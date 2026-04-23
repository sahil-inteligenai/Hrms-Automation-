"""One-time interactive script — run on a machine with a display.

Opens a visible browser, lets you complete Microsoft OAuth + MFA manually,
then saves the browser session (cookies + localStorage) to auth_state.json
so the daily auto_checkout.py can reuse it without going through MFA again.

Re-run this whenever the daily script reports "Session expired" (typically
every 30-90 days, depending on Azure AD policy).
"""
from __future__ import annotations

import logging

from playwright.sync_api import sync_playwright

from config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def main() -> None:
    cfg = load_config()

    print("=" * 60)
    print("HRMS Session Setup")
    print("=" * 60)
    print(f"Target URL : {cfg.hrms_url}")
    print(f"Will save to: {cfg.auth_state_path.resolve()}")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(cfg.hrms_url)

        print("A browser window has opened. Steps:")
        print('  1. Click "Sign in with Microsoft"')
        print("  2. Enter your work email + password")
        print("  3. Complete MFA (Authenticator approval / SMS code / etc.)")
        print("  4. Wait until you can see the HRMS dashboard")
        print("  5. Return to THIS terminal and press ENTER")
        print()
        input("Press ENTER once you are on the HRMS dashboard... ")

        cfg.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(cfg.auth_state_path))
        log.info("Session saved to %s", cfg.auth_state_path.resolve())

        browser.close()

    print()
    print("Done. You can now run:  python auto_checkout.py --once")


if __name__ == "__main__":
    main()
