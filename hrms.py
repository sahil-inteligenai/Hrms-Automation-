"""Playwright-driven HRMS checkout.

Reuses a saved browser session (`auth_state.json`) so the daily run does not
have to navigate Microsoft OAuth + MFA. If the session has expired, returns
a clear error instructing the user to re-run setup_session.py.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from config import Config

log = logging.getLogger(__name__)

# Tried in order; first visible match wins.
CLOCK_OUT_SELECTORS = (
    'button:has-text("Clock Out")',
    'text=/clock\\s*out/i',
)

# After Clock Out opens the inline time editor, click Save to commit.
SAVE_SELECTORS = (
    'button:has-text("Save")',
    '[role="button"]:has-text("Save")',
)

# The HH:MM input that appears next to Save when Clock Out is clicked.
TIME_INPUT_SELECTORS = (
    'input[type="time"]',
    'input[aria-label*="time" i]',
)

# Toast like "Clocked out at 12:26" — its text becomes the success message.
SUCCESS_INDICATORS = (
    'text=/clocked\\s*out\\s*at/i',
    'text=/clocked.?out/i',
)


def _screenshot(page: Page, screenshot_dir: Path, label: str) -> None:
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = screenshot_dir / f"{label}_{ts}.png"
        page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot saved: %s", path)
    except Exception as e:
        log.warning("Screenshot failed: %s", e)


def perform_checkout(cfg: Config, out_time: str | None = None) -> tuple[bool, str]:
    if not cfg.auth_state_path.exists():
        return (
            False,
            f"Auth state file not found at {cfg.auth_state_path}. "
            "Run setup_session.py first to create it.",
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg.headless)
        context = browser.new_context(storage_state=str(cfg.auth_state_path))
        page = context.new_page()

        try:
            log.info("Navigating to %s", cfg.hrms_url)
            page.goto(cfg.hrms_url, wait_until="networkidle", timeout=45_000)

            current_url = page.url
            log.info("Landed on: %s", current_url)
            if "/login" in current_url or "login.microsoftonline.com" in current_url:
                _screenshot(page, cfg.screenshot_dir, "session_expired")
                return (
                    False,
                    "Session expired. Re-run setup_session.py on a machine with a "
                    "display, then copy the new auth_state.json to this host.",
                )

            clicked_selector: str | None = None
            for sel in CLOCK_OUT_SELECTORS:
                try:
                    locator = page.locator(sel).first
                    locator.wait_for(state="visible", timeout=4_000)
                    locator.click()
                    clicked_selector = sel
                    log.info("Clicked Clock Out via selector: %s", sel)
                    break
                except PlaywrightTimeoutError:
                    continue

            if not clicked_selector:
                _screenshot(page, cfg.screenshot_dir, "clock_out_button_not_found")
                return (
                    False,
                    "Could not find a Clock Out button on the dashboard "
                    "(already clocked out today?). Debug screenshot saved.",
                )

            if out_time:
                filled = False
                for sel in TIME_INPUT_SELECTORS:
                    try:
                        field = page.locator(sel).first
                        field.wait_for(state="visible", timeout=4_000)
                        field.fill(out_time)
                        filled = True
                        log.info("Set clock-out time to %s via selector: %s", out_time, sel)
                        break
                    except PlaywrightTimeoutError:
                        continue
                if not filled:
                    _screenshot(page, cfg.screenshot_dir, "time_input_not_found")
                    return (
                        False,
                        "Could not find the clock-out time input. Debug screenshot saved.",
                    )

            saved_selector: str | None = None
            for sel in SAVE_SELECTORS:
                try:
                    locator = page.locator(sel).first
                    locator.wait_for(state="visible", timeout=5_000)
                    locator.click()
                    saved_selector = sel
                    log.info("Clicked Save via selector: %s", sel)
                    break
                except PlaywrightTimeoutError:
                    continue

            if not saved_selector:
                _screenshot(page, cfg.screenshot_dir, "save_button_not_found")
                return (
                    False,
                    "Clock Out opened the time editor but no Save button was found. "
                    "Debug screenshot saved.",
                )

            for indicator in SUCCESS_INDICATORS:
                try:
                    toast = page.locator(indicator).first
                    toast.wait_for(state="visible", timeout=5_000)
                    _screenshot(page, cfg.screenshot_dir, "clock_out_ok")
                    try:
                        text = (toast.text_content() or "").strip()
                    except Exception:
                        text = ""
                    return (True, text or "Clocked out successfully.")
                except PlaywrightTimeoutError:
                    continue

            time.sleep(2)
            _screenshot(page, cfg.screenshot_dir, "clock_out_ambiguous")
            return (
                True,
                "Save clicked, but no success toast detected. "
                "Verify on HRMS manually.",
            )

        except Exception as e:
            log.exception("Checkout flow crashed")
            try:
                _screenshot(page, cfg.screenshot_dir, "checkout_crash")
            except Exception:
                pass
            return (False, f"Checkout failed: {type(e).__name__}: {e}")
        finally:
            context.close()
            browser.close()
