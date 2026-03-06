"""
config.py – Centralised configuration loader.

Reads every setting from environment variables (populated from .env).
All other modules import from here so that .env is only touched once.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── locate and load .env ─────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")


def _require(key: str) -> str:
    """Return env-var value or raise a descriptive error at startup."""
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"[config] Required environment variable '{key}' is not set. "
            f"Please copy .env.example to .env and fill in all values."
        )
    return value


# ── Telegram API (Telethon userbot) ──────────────────────────────
API_ID: int = int(_require("API_ID"))
API_HASH: str = _require("API_HASH")
PHONE_NUMBER: str = _require("PHONE_NUMBER")

# ── Admin Telegram Bot (aiogram) ─────────────────────────────────
BOT_TOKEN: str = _require("BOT_TOKEN")
ADMIN_USER_ID: int = int(_require("ADMIN_USER_ID"))

# ── Target group where filtered jobs are posted ──────────────────
TARGET_GROUP: str = os.getenv("TARGET_GROUP", "IshElonlari")

# ── Persistence ──────────────────────────────────────────────────
DATABASE_PATH: Path = _BASE_DIR / os.getenv("DATABASE_PATH", "data/job_bot.db")
SESSION_NAME: str = str(_BASE_DIR / os.getenv("SESSION_NAME", "sessions/userbot"))

# ── Make sure required directories exist ────────────────────────
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
Path(SESSION_NAME).parent.mkdir(parents=True, exist_ok=True)

# ── Logging ──────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
