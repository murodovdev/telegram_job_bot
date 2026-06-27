"""
Shared pytest fixtures.

Required environment variables (API_ID, BOT_TOKEN, …) are stubbed with dummy
values BEFORE any bot module is imported, so bot.config (which hard-requires
them at import time) loads cleanly in CI where no .env exists.

DATABASE_PATH is pointed at a throwaway temp file so DB tests never touch the
real job_bot.db.
"""

import os
import pathlib
import tempfile

# ── Stub required config env vars before importing anything from bot.* ──
os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "test_api_hash")
os.environ.setdefault("PHONE_NUMBER", "+10000000000")
os.environ.setdefault("BOT_TOKEN", "123456:test-bot-token")
os.environ.setdefault("ADMIN_USER_ID", "1")

_TEST_DB = pathlib.Path(tempfile.gettempdir()) / "jobbot_pytest.db"
os.environ["DATABASE_PATH"] = str(_TEST_DB)
if _TEST_DB.exists():
    _TEST_DB.unlink()

import pytest  # noqa: E402

import bot.database as db  # noqa: E402

_ALL_TABLES = (
    "source_groups", "blocked_users", "content_hashes", "processed_msgs",
    "forwarded_msgs", "forwarded_stats", "original_msg_index",
    "halal_review_queue", "bot_settings", "target_group",
)


@pytest.fixture
def fresh_db():
    """Initialise the schema once, then wipe every table for test isolation."""
    db.init_db()
    with db._connection() as conn:
        for table in _ALL_TABLES:
            conn.execute(f"DELETE FROM {table}")
    # Rebuild caches so they reflect the now-empty tables.
    db._cache_refresh_source_groups()
    db._cache_refresh_blocked_users()
    db._cache_refresh_target()
    # Reset settings caches that the raw DELETE above bypassed.
    db._ai_filter_cache = None
    db._review_queue_cache = None
    db._priority_group_target_cache = None
    db._priority_group_cache_loaded = False
    return db
