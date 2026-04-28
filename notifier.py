"""Telegram + email helpers.

Telegram is used for both outgoing prompts and inbound YES/NO replies
(via long-poll getUpdates). Email is fire-and-forget notification only.
"""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal

from telegram import Bot
from telegram.error import TelegramError

from config import Config

log = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine even when called from inside another running
    event loop (e.g. Playwright's sync API). Always uses a fresh loop in a
    fresh thread so we never collide with the caller's loop."""
    result: dict = {}

    def _worker():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result["value"] = loop.run_until_complete(coro)
        except BaseException as e:
            result["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")

YES_RE = re.compile(r"^\s*y(es)?\s*$", re.IGNORECASE)
NO_RE = re.compile(r"^\s*n(o)?\s*$", re.IGNORECASE)
TIME_RE = re.compile(r"^\s*(?:out\s+|update\s+)?(\d{1,2}):(\d{2})\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Reply:
    kind: Literal["YES", "NO", "TIMEOUT", "OUT"]
    time: str | None = None


def send_telegram(cfg: Config, text: str) -> None:
    async def _send():
        bot = Bot(token=cfg.telegram_bot_token)
        async with bot:
            await bot.send_message(chat_id=cfg.telegram_chat_id, text=text)

    try:
        _run_async(_send())
        log.info("Telegram sent: %s", text[:80].replace("\n", " "))
    except TelegramError as e:
        log.error("Telegram send failed: %s", e)
        raise


def send_email(cfg: Config, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = cfg.gmail_address
    msg["To"] = cfg.email_to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(cfg.gmail_address, cfg.gmail_app_password)
        smtp.send_message(msg)
    log.info("Email sent to %s", cfg.email_to)


def get_latest_update_id(cfg: Config) -> int:
    """Return the most recent update_id so subsequent polling ignores old messages.

    Must be called BEFORE sending the prompt to avoid a race where a fast reply
    arrives between send and poll-start.
    """
    async def _get():
        bot = Bot(token=cfg.telegram_bot_token)
        async with bot:
            updates = await bot.get_updates(timeout=0)
            return updates[-1].update_id if updates else 0

    return _run_async(_get())


def wait_for_telegram_reply(
    cfg: Config, timeout_seconds: int, baseline_update_id: int
) -> Reply:
    deadline = time.time() + timeout_seconds
    offset = baseline_update_id + 1

    async def _poll(off: int, long_poll_timeout: int):
        bot = Bot(token=cfg.telegram_bot_token)
        async with bot:
            return await bot.get_updates(offset=off, timeout=long_poll_timeout)

    while True:
        remaining = int(deadline - time.time())
        if remaining <= 0:
            return Reply("TIMEOUT")

        long_poll = max(1, min(25, remaining))
        try:
            updates = _run_async(_poll(offset, long_poll))
        except TelegramError as e:
            log.warning("getUpdates failed (will retry in 5s): %s", e)
            time.sleep(5)
            continue

        for update in updates:
            offset = update.update_id + 1
            message = update.message
            if not message or not message.text:
                continue
            if message.chat_id != cfg.telegram_chat_id:
                continue

            text = message.text
            if YES_RE.match(text):
                log.info("Got YES reply")
                return Reply("YES")
            if NO_RE.match(text):
                log.info("Got NO reply")
                return Reply("NO")
            m = TIME_RE.match(text)
            if m:
                hh, mm = int(m.group(1)), int(m.group(2))
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    normalized = f"{hh:02d}:{mm:02d}"
                    log.info("Got OUT-at-time reply: %s", normalized)
                    return Reply("OUT", normalized)
            log.info("Ignoring unrecognized reply: %r", text[:60])
