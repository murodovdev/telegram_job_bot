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
# On Railway the container filesystem is ephemeral — any file written inside
# the container is lost on the next deployment. To persist the database across
# deployments you must attach a Railway Volume at /data and set:
#   DATABASE_PATH = /data/job_bot.db
#
# Auto-detection: if RAILWAY_ENVIRONMENT is set (Railway injects it automatically)
# AND DATABASE_PATH is not explicitly overridden, we default to /data/job_bot.db
# so that attaching a volume at /data is enough — no extra env var needed.
_is_railway   = bool(os.getenv("RAILWAY_ENVIRONMENT"))
_db_default   = "/data/job_bot.db" if _is_railway else "data/job_bot.db"
_db_raw       = os.getenv("DATABASE_PATH", _db_default)

# Absolute paths (e.g. /data/job_bot.db) are used as-is.
# Relative paths are resolved against the project root.
DATABASE_PATH : Path = (
    Path(_db_raw) if Path(_db_raw).is_absolute()
    else _BASE_DIR / _db_raw
)

# ── Content deduplication ─────────────────────────────────────────
# How many hours to remember forwarded job posts for duplicate detection.
# A post identical or very similar to one forwarded within this window is skipped.
# Default: 24 hours (job listings are typically re-posted daily).
# Set to 0 to disable content deduplication entirely.
DEDUP_WINDOW_HOURS: int = int(os.getenv("DEDUP_WINDOW_HOURS", "24"))

# Similarity threshold for near-duplicate detection (0.0–1.0).
# 1.0 = exact match only.  0.85 = allows ~15% difference (extra lines, edits).
# Recommended range: 0.80–0.90. Below 0.75 produces too many false positives.
DEDUP_SIMILARITY_THRESHOLD: float = float(
    os.getenv("DEDUP_SIMILARITY_THRESHOLD", "0.85")
)

# ── Logging ───────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Create local directories ──────────────────────────────────────
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
if not SESSION_STRING:
    Path(SESSION_NAME).parent.mkdir(parents=True, exist_ok=True)
if ACCOUNT_2_ENABLED and not SESSION_STRING_2:
    Path(SESSION_NAME_2).parent.mkdir(parents=True, exist_ok=True)