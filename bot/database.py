"""
database.py – SQLite persistence layer.

Changes in v3
-------------
• New table: forwarded_stats — records every forwarded message with source
  group and timestamp for analytics (/stats command).
• New table: original_msg_index — stores (fwd_from_chat_id, fwd_from_msg_id)
  to detect reposted/forwarded duplicates across groups.
• New functions: record_stat(), get_stats_summary(), is_repost().
"""

import sqlite3
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, List, Optional

from bot.config import DATABASE_PATH

logger = logging.getLogger(__name__)


# ── Data-Transfer Objects ────────────────────────────────────────

@dataclass
class SourceGroup:
    id: int
    chat_id: int
    title: str
    username: Optional[str]
    added_at: str


# ── Connection helper ────────────────────────────────────────────

@contextmanager
def _connection() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(DATABASE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────

def init_db() -> None:
    """Create / migrate all tables. Safe to call on every startup."""
    with _connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS source_groups (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id   INTEGER NOT NULL UNIQUE,
                title     TEXT    NOT NULL,
                username  TEXT,
                added_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS target_group (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                chat_id    INTEGER NOT NULL,
                title      TEXT    NOT NULL,
                username   TEXT,
                updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS processed_msgs (
                chat_id      INTEGER NOT NULL,
                message_id   INTEGER NOT NULL,
                processed_at TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_pm_chat ON processed_msgs (chat_id);

            -- Analytics: one row per forwarded message
            CREATE TABLE IF NOT EXISTS forwarded_stats (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat_id INTEGER NOT NULL,
                source_title   TEXT,
                matched_lang   TEXT,
                matched_kw     TEXT,
                match_tier     INTEGER DEFAULT 1,
                forwarded_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_fs_chat
                ON forwarded_stats (source_chat_id);
            CREATE INDEX IF NOT EXISTS idx_fs_time
                ON forwarded_stats (forwarded_at);

            -- Repost dedup: original message origin index
            CREATE TABLE IF NOT EXISTS original_msg_index (
                fwd_chat_id INTEGER NOT NULL,
                fwd_msg_id  INTEGER NOT NULL,
                seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (fwd_chat_id, fwd_msg_id)
            );
        """)
    logger.info("[DB] Schema initialised at %s", DATABASE_PATH)


# ── Source-group CRUD ────────────────────────────────────────────

def add_source_group(chat_id: int, title: str, username: Optional[str] = None) -> bool:
    try:
        with _connection() as conn:
            conn.execute(
                "INSERT INTO source_groups (chat_id, title, username) VALUES (?, ?, ?)",
                (chat_id, title, username),
            )
        logger.info("[DB] Source group added: %s (%s)", title, chat_id)
        return True
    except sqlite3.IntegrityError:
        logger.warning("[DB] Source group already exists: %s", chat_id)
        return False


def remove_source_group(chat_id: int) -> bool:
    removed = False
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM source_groups WHERE chat_id = ?", (chat_id,)
        )
        removed = cursor.rowcount > 0
    if removed:
        logger.info("[DB] Source group removed: %s", chat_id)
    return removed


def update_source_group_title(chat_id: int, title: str, username: Optional[str] = None) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE source_groups SET title=?, username=? WHERE chat_id=?",
            (title, username, chat_id),
        )


def list_source_groups() -> List[SourceGroup]:
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM source_groups ORDER BY added_at DESC"
        ).fetchall()
    return [SourceGroup(**dict(row)) for row in rows]


def get_source_group_ids() -> List[int]:
    with _connection() as conn:
        rows = conn.execute("SELECT chat_id FROM source_groups").fetchall()
    return [r["chat_id"] for r in rows]


def get_source_group_by_id(chat_id: int) -> Optional[SourceGroup]:
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM source_groups WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return SourceGroup(**dict(row)) if row else None


# ── Target-group ─────────────────────────────────────────────────

def set_target_group(chat_id: int, title: str, username: Optional[str] = None) -> None:
    with _connection() as conn:
        conn.execute("""
            INSERT INTO target_group (id, chat_id, title, username, updated_at)
            VALUES (1, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                chat_id    = excluded.chat_id,
                title      = excluded.title,
                username   = excluded.username,
                updated_at = excluded.updated_at
        """, (chat_id, title, username))
    logger.info("[DB] Target group set: %s (%s)", title, chat_id)


def get_target_group() -> Optional[dict]:
    with _connection() as conn:
        row = conn.execute("SELECT * FROM target_group WHERE id = 1").fetchone()
    return dict(row) if row else None


# ── Deduplication ────────────────────────────────────────────────

def mark_processed(chat_id: int, message_id: int) -> None:
    with _connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_msgs (chat_id, message_id) VALUES (?, ?)",
            (chat_id, message_id),
        )


def is_processed(chat_id: int, message_id: int) -> bool:
    with _connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_msgs WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()
    return row is not None


def get_processed_count() -> int:
    with _connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM processed_msgs").fetchone()
    return row["cnt"]


# ── Repost / forward dedup ───────────────────────────────────────

def is_repost(fwd_chat_id: int, fwd_msg_id: int) -> bool:
    """
    Return True if we have already seen a message that was originally
    posted in fwd_chat_id with fwd_msg_id.

    This catches the case where the same job post is forwarded from
    group A into groups B and C — without this check it would be
    forwarded to the target twice.
    """
    with _connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM original_msg_index WHERE fwd_chat_id=? AND fwd_msg_id=?",
            (fwd_chat_id, fwd_msg_id),
        ).fetchone()
    return row is not None


def mark_original(fwd_chat_id: int, fwd_msg_id: int) -> None:
    """Record the original (chat_id, msg_id) of a forwarded message."""
    with _connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO original_msg_index (fwd_chat_id, fwd_msg_id) VALUES (?, ?)",
            (fwd_chat_id, fwd_msg_id),
        )


# ── Analytics ────────────────────────────────────────────────────

def record_stat(
    source_chat_id: int,
    source_title: str,
    matched_lang: Optional[str],
    matched_kw: Optional[str],
    match_tier: int = 1,
) -> None:
    """Insert one analytics row every time a job post is forwarded."""
    with _connection() as conn:
        conn.execute(
            """INSERT INTO forwarded_stats
               (source_chat_id, source_title, matched_lang, matched_kw, match_tier)
               VALUES (?, ?, ?, ?, ?)""",
            (source_chat_id, source_title, matched_lang, matched_kw, match_tier),
        )


def get_stats_summary(days: int = 7) -> dict:
    """
    Return an analytics summary for the last *days* days.

    Returns a dict with:
      total         – total forwarded messages
      by_group      – list of (title, count) sorted by count desc
      by_lang       – dict {lang: count}
      by_tier       – dict {1: count, 2: count}
      today         – count for today (KST approximated as UTC+9)
    """
    with _connection() as conn:
        # Total in window
        total_row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM forwarded_stats
               WHERE forwarded_at >= datetime('now', ?)""",
            (f"-{days} days",),
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        # Today count (UTC, close enough for reporting)
        today_row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM forwarded_stats
               WHERE date(forwarded_at) = date('now')"""
        ).fetchone()
        today = today_row["cnt"] if today_row else 0

        # Top groups
        group_rows = conn.execute(
            """SELECT source_title, COUNT(*) AS cnt
               FROM forwarded_stats
               WHERE forwarded_at >= datetime('now', ?)
               GROUP BY source_chat_id
               ORDER BY cnt DESC
               LIMIT 10""",
            (f"-{days} days",),
        ).fetchall()
        by_group = [(r["source_title"], r["cnt"]) for r in group_rows]

        # By language
        lang_rows = conn.execute(
            """SELECT matched_lang, COUNT(*) AS cnt
               FROM forwarded_stats
               WHERE forwarded_at >= datetime('now', ?)
               GROUP BY matched_lang""",
            (f"-{days} days",),
        ).fetchall()
        by_lang = {r["matched_lang"] or "unknown": r["cnt"] for r in lang_rows}

        # By tier
        tier_rows = conn.execute(
            """SELECT match_tier, COUNT(*) AS cnt
               FROM forwarded_stats
               WHERE forwarded_at >= datetime('now', ?)
               GROUP BY match_tier""",
            (f"-{days} days",),
        ).fetchall()
        by_tier = {r["match_tier"]: r["cnt"] for r in tier_rows}

    return {
        "total": total,
        "today": today,
        "by_group": by_group,
        "by_lang": by_lang,
        "by_tier": by_tier,
        "days": days,
    }


def get_daily_summary() -> dict:
    """Stats for today only — used by the scheduled evening summary."""
    with _connection() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM forwarded_stats WHERE date(forwarded_at) = date('now')"
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        group_rows = conn.execute(
            """SELECT source_title, COUNT(*) AS cnt
               FROM forwarded_stats
               WHERE date(forwarded_at) = date('now')
               GROUP BY source_chat_id
               ORDER BY cnt DESC
               LIMIT 5"""
        ).fetchall()
        by_group = [(r["source_title"], r["cnt"]) for r in group_rows]

        lang_rows = conn.execute(
            """SELECT matched_lang, COUNT(*) AS cnt
               FROM forwarded_stats
               WHERE date(forwarded_at) = date('now')
               GROUP BY matched_lang"""
        ).fetchall()
        by_lang = {r["matched_lang"] or "unknown": r["cnt"] for r in lang_rows}

    return {"total": total, "by_group": by_group, "by_lang": by_lang}