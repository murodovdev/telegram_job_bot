"""
config.py – Centralised configuration loader.

Session handling
----------------
Two modes are supported:

1. STRING SESSION (recommended for Railway / cloud deployment)
   Set SESSION_STRING in your environment variables.
   The session lives entirely in memory — no file needed.

2. FILE SESSION (local development)
   Leave SESSION_STRING empty and set SESSION_NAME (file path).
   Telethon writes a .session file to disk.
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
PHONE_NUMBER : str = _require("PHONE_NUMBER")

# ── Admin bot ─────────────────────────────────────────────────────
BOT_TOKEN    : str = _require("BOT_TOKEN")
ADMIN_USER_ID: int = int(_require("ADMIN_USER_ID"))

# ── Target group ──────────────────────────────────────────────────
TARGET_GROUP : str = os.getenv("TARGET_GROUP", "")

# ── Session (string takes priority over file) ─────────────────────
SESSION_STRING: str = os.getenv("SESSION_STRING", "")   # Railway / cloud
SESSION_NAME  : str = str(_BASE_DIR / os.getenv("SESSION_NAME", "sessions/userbot"))

# ── Database ──────────────────────────────────────────────────────
DATABASE_PATH : Path = _BASE_DIR / os.getenv("DATABASE_PATH", "data/job_bot.db")

# ── Logging ───────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Create local directories (skipped on Railway — ephemeral FS) ──
if not SESSION_STRING:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    Path(SESSION_NAME).parent.mkdir(parents=True, exist_ok=True)
else:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)