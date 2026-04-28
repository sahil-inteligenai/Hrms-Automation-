"""Configuration loader.

Reads environment variables (via .env) into a frozen dataclass and validates
that all required credentials are present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REQUIRED_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
)


@dataclass(frozen=True)
class Config:
    hrms_url: str
    telegram_bot_token: str
    telegram_chat_id: int
    gmail_address: str
    gmail_app_password: str
    email_to: str
    auth_state_path: Path
    screenshot_dir: Path
    reply_timeout_seconds: int
    run_time: str
    headless: bool
    ms_email: str | None
    ms_password: str | None
    keep_alive_interval_hours: int

    @property
    def reauth_enabled(self) -> bool:
        return bool(self.ms_email and self.ms_password)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    missing = [k for k in REQUIRED_ENV_VARS if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            "Missing required env vars: "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values."
        )

    return Config(
        hrms_url=os.getenv("HRMS_URL", "https://hrms.inteligenai.com/").rstrip("/") + "/",
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=int(os.environ["TELEGRAM_CHAT_ID"]),
        gmail_address=os.environ["GMAIL_ADDRESS"],
        gmail_app_password=os.environ["GMAIL_APP_PASSWORD"],
        email_to=os.getenv("EMAIL_TO") or os.environ["GMAIL_ADDRESS"],
        auth_state_path=Path(os.getenv("AUTH_STATE_PATH", "./auth_state.json")),
        screenshot_dir=Path(os.getenv("SCREENSHOT_DIR", "./screenshots")),
        reply_timeout_seconds=int(os.getenv("REPLY_TIMEOUT_SECONDS", "600")),
        run_time=os.getenv("RUN_TIME", "22:00"),
        headless=_bool(os.getenv("HEADLESS"), True),
        ms_email=(os.getenv("MS_EMAIL") or None),
        ms_password=(os.getenv("MS_PASSWORD") or None),
        keep_alive_interval_hours=int(os.getenv("KEEP_ALIVE_INTERVAL_HOURS", "6")),
    )
