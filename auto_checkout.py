"""InteligenAI HRMS auto-checkout daemon.

Runs continuously; at the configured time on weekdays it:
  1. Sends a Telegram + email prompt asking whether you've already checked out.
  2. Waits up to REPLY_TIMEOUT_SECONDS for a YES/NO reply on Telegram.
  3. If YES         -> sends "got it" and exits.
     If NO/TIMEOUT  -> drives Playwright to click Check Out on the HRMS dashboard.
  4. Reports the outcome back via Telegram.

Use --once for an immediate test run that bypasses the scheduler and the
weekday gate.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time
from datetime import datetime

import schedule

from config import Config, load_config
from hrms import keep_session_alive, perform_checkout
from notifier import (
    get_latest_update_id,
    send_email,
    send_telegram,
    wait_for_telegram_reply,
)
from reauth import refresh_session

log = logging.getLogger("auto_checkout")

PROMPT_TEXT = (
    "InteligenAI HRMS - did you check out today?\n\n"
    "Reply YES if you already did.\n"
    "Reply NO (or do not reply at all) and I will check you out automatically "
    "in 10 minutes.\n"
    "Reply HH:MM (e.g. 18:30) and I will clock you out at that time."
)


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        "auto_checkout.log", maxBytes=2_000_000, backupCount=3
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def run_keep_alive(cfg: Config) -> None:
    """Probe HRMS to refresh cookies; if expired, attempt automated re-auth."""
    health = keep_session_alive(cfg)
    if health == "healthy":
        return
    if health == "error":
        log.warning("keep-alive: probe errored, will retry next cycle")
        return

    # health == "expired"
    if not cfg.reauth_enabled:
        try:
            send_telegram(
                cfg,
                "HRMS session expired. Re-run setup_session.py to refresh "
                "(or set MS_EMAIL/MS_PASSWORD in .env to enable auto re-auth).",
            )
        except Exception as e:
            log.warning("Could not send expiry alert: %s", e)
        return

    try:
        send_telegram(cfg, "HRMS session expired - attempting automatic re-auth.")
    except Exception as e:
        log.warning("Could not send pre-reauth alert: %s", e)

    ok, message = refresh_session(cfg)
    log.info("reauth result: ok=%s msg=%s", ok, message)
    try:
        send_telegram(cfg, ("[OK] " if ok else "[FAIL] ") + message)
    except Exception as e:
        log.warning("Could not send reauth result: %s", e)


def run_workflow(cfg: Config, *, force: bool = False) -> None:
    weekday = datetime.now().weekday()  # Mon=0 ... Sun=6
    if not force and weekday >= 5:
        log.info("Weekend (weekday=%d) - skipping today's check.", weekday)
        return

    log.info("Starting checkout workflow")

    try:
        baseline = get_latest_update_id(cfg)
    except Exception as e:
        log.error("Could not contact Telegram for baseline (%s). Aborting run.", e)
        return

    try:
        send_telegram(cfg, PROMPT_TEXT)
    except Exception as e:
        log.error("Telegram prompt failed: %s. Will still attempt checkout after timeout.", e)

    try:
        send_email(cfg, "HRMS Checkout Reminder", PROMPT_TEXT)
    except Exception as e:
        log.warning("Email send failed (non-fatal): %s", e)

    log.info("Waiting up to %ds for Telegram reply", cfg.reply_timeout_seconds)
    reply = wait_for_telegram_reply(cfg, cfg.reply_timeout_seconds, baseline)
    log.info("Reply outcome: %s", reply)

    if reply.kind == "YES":
        try:
            send_telegram(cfg, "Got it - no action taken.")
        except Exception as e:
            log.warning("Could not send acknowledgement: %s", e)
        return

    out_time = reply.time if reply.kind == "OUT" else None
    log.info("Reply was %s - performing auto-checkout (out_time=%s)", reply.kind, out_time)
    success, message = perform_checkout(cfg, out_time=out_time)
    summary = ("[OK] " if success else "[FAIL] ") + message
    log.info("Checkout result: %s", summary)

    try:
        send_telegram(cfg, summary)
    except Exception as e:
        log.error("Failed to report result on Telegram: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the workflow immediately (skip scheduler and weekday gate) and exit.",
    )
    args = parser.parse_args()

    _setup_logging()
    cfg = load_config()

    if args.once:
        log.info("--once mode: running workflow now")
        run_workflow(cfg, force=True)
        return

    def _job() -> None:
        try:
            run_workflow(cfg)
        except Exception:
            log.exception("Workflow crashed (scheduler stays alive)")

    def _keep_alive_job() -> None:
        try:
            run_keep_alive(cfg)
        except Exception:
            log.exception("Keep-alive crashed (scheduler stays alive)")

    schedule.every().day.at(cfg.run_time).do(_job)
    schedule.every(cfg.keep_alive_interval_hours).hours.do(_keep_alive_job)

    # Run a probe immediately on startup so a stale session isn't carried
    # all the way to checkout time.
    _keep_alive_job()

    log.info(
        "Scheduler armed: checkout %s daily (weekdays); keep-alive every %dh. "
        "Press Ctrl-C to stop.",
        cfg.run_time,
        cfg.keep_alive_interval_hours,
    )

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
