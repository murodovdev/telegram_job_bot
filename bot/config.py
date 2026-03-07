"""
config.py – Centralised configuration loader.

Session handling
----------------
Two modes per account:

1. STRING SESSION (recommended for Railway / cloud)
   Set SESSION_STRING (account 1) and optionally SESSION_STRING_2 (account 2).

2. FILE SESSION (local development)
   Leave SESSION_STRING empty; set SESSION_NAME file path instead.

Account 2 is fully optional. If SESSION_STRING_2 and PHONE_NUMBER_2 are both
empty, the bot runs with account 1 only — fully backward compatible.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"[config] Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in all values."
        )
    return value


# ── Telegram API ──────────────────────────────────────────────────
API_ID       : int = int(_require("API_ID"))
API_HASH     : str = _require("API_HASH")

# ── Account 1 (primary, required) ────────────────────────────────
PHONE_NUMBER  : str = _require("PHONE_NUMBER")
SESSION_STRING: str = os.getenv("SESSION_STRING", "")
SESSION_NAME  : str = str(_BASE_DIR / os.getenv("SESSION_NAME", "sessions/userbot"))

# ── Account 2 (fallback, optional) ───────────────────────────────
PHONE_NUMBER_2 : str = os.getenv("PHONE_NUMBER_2", "")
SESSION_STRING_2: str = os.getenv("SESSION_STRING_2", "")
SESSION_NAME_2  : str = str(_BASE_DIR / os.getenv("SESSION_NAME_2", "sessions/userbot2"))

# True if account 2 credentials are present
ACCOUNT_2_ENABLED: bool = bool(SESSION_STRING_2 or PHONE_NUMBER_2)

# ── Admin bot ─────────────────────────────────────────────────────
BOT_TOKEN    : str = _require("BOT_TOKEN")
ADMIN_USER_ID: int = int(_require("ADMIN_USER_ID"))

# ── Target group ──────────────────────────────────────────────────
TARGET_GROUP : str = os.getenv("TARGET_GROUP", "")

# ── Database ──────────────────────────────────────────────────────
DATABASE_PATH : Path = _BASE_DIR / os.getenv("DATABASE_PATH", "data/job_bot.db")

# ── Logging ───────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Create local directories ──────────────────────────────────────
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
if not SESSION_STRING:
    Path(SESSION_NAME).parent.mkdir(parents=True, exist_ok=True)
if ACCOUNT_2_ENABLED and not SESSION_STRING_2:
    Path(SESSION_NAME_2).parent.mkdir(parents=True, exist_ok=True)