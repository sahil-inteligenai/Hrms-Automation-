"""Headless re-auth driver.

Logs into Microsoft on behalf of the user when the saved Playwright session
has expired. The user only has to tap Approve in the Authenticator app (or
type a 2-digit number-match code) — everything else is automated.

Triggered by the keep-alive probe in `auto_checkout.py`. If MS_EMAIL /
MS_PASSWORD are not configured, this module is not called and behavior
falls back to the manual `setup_session.py` flow.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from config import Config
from notifier import send_telegram

log = logging.getLogger(__name__)

# How long to give the user to approve the push / type the number.
MFA_WAIT_TIMEOUT_MS = 90_000

# Microsoft's MFA screen has used several locations for number-match digits
# across UI revisions. Try them in order.
NUMBER_MATCH_SELECTORS = (
    "#idRichContext_DisplaySign",
    "#displaySign",
    '[data-testid="displaySign"]',
    'div[role="heading"]:has-text("Enter the number")',
)

# "Sign in with Microsoft" button on the HRMS login page.
HRMS_MS_BUTTON_SELECTORS = (
    'button:has-text("Sign in with Microsoft")',
    'a:has-text("Sign in with Microsoft")',
    'text=/sign\\s*in\\s*with\\s*microsoft/i',
)


def _screenshot(page: Page, screenshot_dir: Path, label: str) -> Path | None:
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = screenshot_dir / f"reauth_{label}_{ts}.png"
        page.screenshot(path=str(path), full_page=True)
        log.info("Screenshot saved: %s", path)
        return path
    except Exception as e:
        log.warning("Screenshot failed: %s", e)
        return None


def _click_first(page: Page, selectors, timeout: int = 8_000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            loc.click()
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def _scrape_number_match(page: Page) -> str | None:
    # Wait briefly for the MFA screen to settle so Microsoft has time to
    # render the displayed number into the DOM.
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeoutError:
        pass

    # 1) Try the explicit element selectors first.
    for sel in NUMBER_MATCH_SELECTORS:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=2_500)
            text = (loc.text_content() or "").strip()
            m = re.search(r"\b(\d{2,3})\b", text)
            if m:
                return m.group(1)
            if text:
                return text
        except PlaywrightTimeoutError:
            continue

    # 2) Fallback: scan the visible body text for a 2-digit number that
    #    appears near Authenticator-related copy. Try the page and any
    #    iframes (Microsoft's MFA UI sometimes renders inside one).
    candidates = [page]
    candidates.extend(page.frames)
    pat = re.compile(
        r"(?:enter\s+the\s+number|authenticator).*?(\d{2,3})",
        re.IGNORECASE | re.DOTALL,
    )
    for frame in candidates:
        try:
            body = frame.locator("body").inner_text(timeout=2_000)
        except Exception:
            continue
        m = pat.search(body)
        if m:
            return m.group(1)
        if "authenticator" in body.lower():
            m2 = re.search(r"\b(\d{2,3})\b", body)
            if m2:
                return m2.group(1)
    return None


def refresh_session(cfg: Config) -> tuple[bool, str]:
    """Drive a fresh Microsoft login and save the resulting session.

    Returns (ok, message). Sends Telegram updates at key transitions so the
    user knows when to look at their Authenticator app.
    """
    if not cfg.reauth_enabled:
        return (False, "Re-auth disabled (MS_EMAIL / MS_PASSWORD not set in .env).")

    log.info("reauth: starting headless re-auth flow")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg.headless)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(cfg.hrms_url, wait_until="domcontentloaded", timeout=45_000)

            if not _click_first(page, HRMS_MS_BUTTON_SELECTORS, timeout=10_000):
                _screenshot(page, cfg.screenshot_dir, "no_ms_button")
                return (False, "Re-auth failed: 'Sign in with Microsoft' button not found.")

            # --- Email step ---
            try:
                email_input = page.locator('input[type="email"], input[name="loginfmt"]').first
                email_input.wait_for(state="visible", timeout=20_000)
                email_input.fill(cfg.ms_email or "")
                page.locator('input[type="submit"], button[type="submit"]').first.click()
            except PlaywrightTimeoutError:
                _screenshot(page, cfg.screenshot_dir, "no_email_field")
                return (False, "Re-auth failed: Microsoft email field did not appear.")

            # --- Password step ---
            try:
                pw_input = page.locator('input[type="password"], input[name="passwd"]').first
                pw_input.wait_for(state="visible", timeout=20_000)
                pw_input.fill(cfg.ms_password or "")
                page.locator('input[type="submit"], button[type="submit"]').first.click()
            except PlaywrightTimeoutError:
                _screenshot(page, cfg.screenshot_dir, "no_password_field")
                return (False, "Re-auth failed: password field did not appear.")

            # Detect immediate credential rejection.
            try:
                err = page.locator('#passwordError, [role="alert"]').first
                err.wait_for(state="visible", timeout=2_500)
                msg = (err.text_content() or "").strip()
                _screenshot(page, cfg.screenshot_dir, "credential_error")
                return (False, f"Re-auth failed: credential rejected ({msg[:120]}).")
            except PlaywrightTimeoutError:
                pass

            # --- MFA step ---
            number = _scrape_number_match(page)
            # Always dump a screenshot so if the number ever comes back empty
            # we can see what Microsoft rendered.
            _screenshot(page, cfg.screenshot_dir, "mfa_screen")
            if number:
                send_telegram(
                    cfg,
                    f"HRMS re-auth: open Microsoft Authenticator and enter "
                    f"`{number}` within {MFA_WAIT_TIMEOUT_MS // 1000}s.",
                )
                log.info("reauth: number-match prompt %s sent to user", number)
            else:
                send_telegram(
                    cfg,
                    "HRMS re-auth: open Authenticator. If it asks for a number, "
                    "check screenshots/reauth_mfa_screen_*.png on the host - "
                    f"I couldn't read it. {MFA_WAIT_TIMEOUT_MS // 1000}s window.",
                )
                log.warning("reauth: number-match could not be scraped; pushed fallback message")

            # Wait for navigation off login.microsoftonline.com.
            try:
                page.wait_for_url(
                    re.compile(r"^(?!https://login\.microsoftonline\.com).*"),
                    timeout=MFA_WAIT_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                _screenshot(page, cfg.screenshot_dir, "mfa_timeout")
                return (
                    False,
                    f"Re-auth failed: no MFA approval received in "
                    f"{MFA_WAIT_TIMEOUT_MS // 1000}s.",
                )

            # --- "Stay signed in?" KMSI prompt (sometimes shown) ---
            try:
                if "login.microsoftonline.com" in page.url or "login.live.com" in page.url:
                    yes_btn = page.locator(
                        'input[type="submit"][value="Yes"], button:has-text("Yes")'
                    ).first
                    yes_btn.wait_for(state="visible", timeout=5_000)
                    yes_btn.click()
            except PlaywrightTimeoutError:
                pass

            # --- Land on HRMS dashboard ---
            try:
                page.wait_for_url(re.compile(r"^https://hrms\.inteligenai\.com/.*"),
                                  timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PlaywrightTimeoutError:
                _screenshot(page, cfg.screenshot_dir, "post_mfa_unknown")
                return (False, f"Re-auth failed: ended on unexpected URL {page.url}.")

            if "/login" in page.url:
                _screenshot(page, cfg.screenshot_dir, "post_mfa_back_to_login")
                return (False, "Re-auth failed: HRMS still showing /login after MFA.")

            cfg.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(cfg.auth_state_path))
            log.info("reauth: session saved to %s", cfg.auth_state_path)
            return (True, "HRMS session refreshed.")

        except Exception as e:
            log.exception("reauth: flow crashed")
            try:
                _screenshot(page, cfg.screenshot_dir, "crash")
            except Exception:
                pass
            return (False, f"Re-auth failed: {type(e).__name__}: {e}")
        finally:
            context.close()
            browser.close()
