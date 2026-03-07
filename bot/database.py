"""
database.py – SQLite persistence layer.

Changes in v4 (dual-account)
-----------------------------
• source_groups gains assigned_account INTEGER NOT NULL DEFAULT 1
  so each group can be assigned to account 1 or 2.
• init_db() runs a safe ALTER TABLE migration so existing databases
  gain the column without losing any data.
• add_source_group() accepts assigned_account param (default 1).
• list_source_groups() accepts optional account filter.
• get_source_group_ids() accepts optional account filter.
• SourceGroup dataclass gains assigned_account field.
"""

import sqlite3
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
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
    assigned_account: int = 1


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
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id          INTEGER NOT NULL UNIQUE,
                title            TEXT    NOT NULL,
                username         TEXT,
                added_at         TEXT    NOT NULL DEFAULT (datetime('now')),
                assigned_account INTEGER NOT NULL DEFAULT 1
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

            CREATE TABLE IF NOT EXISTS original_msg_index (
                fwd_chat_id INTEGER NOT NULL,
                fwd_msg_id  INTEGER NOT NULL,
                seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (fwd_chat_id, fwd_msg_id)
            );
        """)

        # ── Safe migration: add assigned_account to existing tables ──
        # SQLite does not support ALTER TABLE ADD COLUMN IF NOT EXISTS,
        # so we attempt it and ignore the error if the column already exists.
        try:
            conn.execute(
                "ALTER TABLE source_groups "
                "ADD COLUMN assigned_account INTEGER NOT NULL DEFAULT 1"
            )
            logger.info("[DB] Migrated: added assigned_account column")
        except sqlite3.OperationalError:
            pass  # column already exists — normal on subsequent startups

    logger.info("[DB] Schema initialised at %s", DATABASE_PATH)


# ── Source-group CRUD ────────────────────────────────────────────

def add_source_group(
    chat_id: int,
    title: str,
    username: Optional[str] = None,
    assigned_account: int = 1,
) -> bool:
    """Insert a new source group. Returns True on success, False if duplicate."""
    try:
        with _connection() as conn:
            conn.execute(
                "INSERT INTO source_groups "
                "(chat_id, title, username, assigned_account) VALUES (?, ?, ?, ?)",
                (chat_id, title, username, assigned_account),
            )
        logger.info(
            "[DB] Source group added: %s (%s) → account %s",
            title, chat_id, assigned_account,
        )
        return True
    except sqlite3.IntegrityError:
        logger.warning("[DB] Source group already exists: %s", chat_id)
        return False


def remove_source_group(chat_id: int) -> bool:
    """Delete a source group. Returns True if a row was actually deleted."""
    removed = False
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM source_groups WHERE chat_id = ?", (chat_id,)
        )
        removed = cursor.rowcount > 0
    if removed:
        logger.info("[DB] Source group removed: %s", chat_id)
    return removed


def update_source_group_title(
    chat_id: int, title: str, username: Optional[str] = None
) -> None:
    with _connection() as conn:
        conn.execute(
            "UPDATE source_groups SET title=?, username=? WHERE chat_id=?",
            (title, username, chat_id),
        )


def reassign_source_group(
    chat_id: int,
    assigned_account: int,
    correct_chat_id: Optional[int] = None,
    title: Optional[str] = None,
    username: Optional[str] = None,
) -> bool:
    """
    Move a group to a different account.
    Optionally corrects the stored chat_id, title, and username at the same time
    (used to fix legacy rows stored with a positive/un-prefixed chat_id).
    Returns True if a row was updated.
    """
    new_chat_id = correct_chat_id if correct_chat_id is not None else chat_id

    # Build update dynamically depending on which fields are provided
    sets = ["assigned_account = ?"]
    params: list = [assigned_account]

    if correct_chat_id is not None and correct_chat_id != chat_id:
        sets.append("chat_id = ?")
        params.append(correct_chat_id)
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if username is not None:
        sets.append("username = ?")
        params.append(username)

    params.append(chat_id)  # WHERE clause

    with _connection() as conn:
        cursor = conn.execute(
            f"UPDATE source_groups SET {', '.join(sets)} WHERE chat_id = ?",
            params,
        )
        updated = cursor.rowcount > 0
    if updated:
        logger.info(
            "[DB] Group %s reassigned to account %s (stored as %s)",
            chat_id, assigned_account, new_chat_id,
        )
    return updated


def list_source_groups(account: Optional[int] = None) -> List[SourceGroup]:
    """
    Return monitored source groups ordered by most recently added.
    If account is given (1 or 2), return only groups for that account.
    """
    with _connection() as conn:
        if account is not None:
            rows = conn.execute(
                "SELECT * FROM source_groups WHERE assigned_account=? ORDER BY added_at DESC",
                (account,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM source_groups ORDER BY added_at DESC"
            ).fetchall()
    return [SourceGroup(**dict(row)) for row in rows]


def get_source_group_ids(account: Optional[int] = None) -> List[int]:
    """
    Return chat_id list — used by the monitor hot-path.
    If account is given, return only IDs for that account.
    """
    with _connection() as conn:
        if account is not None:
            rows = conn.execute(
                "SELECT chat_id FROM source_groups WHERE assigned_account=?",
                (account,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT chat_id FROM source_groups").fetchall()
    return [r["chat_id"] for r in rows]


def get_source_group_by_id(chat_id: int) -> Optional[SourceGroup]:
    """
    Find a source group by chat_id.
    Searches both the given ID and its normalised -100 counterpart so that
    legacy rows stored without the -100 prefix are still found correctly.
    """
    # Build both candidate IDs
    if chat_id > 0:
        candidates = (chat_id, int(f"-100{chat_id}"))
    else:
        # e.g. -1002000748619  →  also try the bare positive 2000748619
        bare = int(str(chat_id).lstrip("-").lstrip("100") or "0") if str(abs(chat_id)).startswith("100") else abs(chat_id)
        candidates = (chat_id, bare)

    with _connection() as conn:
        placeholders = ",".join("?" * len(candidates))
        row = conn.execute(
            f"SELECT * FROM source_groups WHERE chat_id IN ({placeholders})",
            candidates,
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
    with _connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM original_msg_index WHERE fwd_chat_id=? AND fwd_msg_id=?",
            (fwd_chat_id, fwd_msg_id),
        ).fetchone()
    return row is not None


def mark_original(fwd_chat_id: int, fwd_msg_id: int) -> None:
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
    with _connection() as conn:
        conn.execute(
            """INSERT INTO forwarded_stats
               (source_chat_id, source_title, matched_lang, matched_kw, match_tier)
               VALUES (?, ?, ?, ?, ?)""",
            (source_chat_id, source_title, matched_lang, matched_kw, match_tier),
        )


def get_stats_summary(days: int = 7) -> dict:
    with _connection() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM forwarded_stats "
            "WHERE forwarded_at >= datetime('now', ?)",
            (f"-{days} days",),
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        today_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM forwarded_stats "
            "WHERE date(forwarded_at) = date('now')"
        ).fetchone()
        today = today_row["cnt"] if today_row else 0

        group_rows = conn.execute(
            "SELECT source_title, COUNT(*) AS cnt FROM forwarded_stats "
            "WHERE forwarded_at >= datetime('now', ?) "
            "GROUP BY source_chat_id ORDER BY cnt DESC LIMIT 10",
            (f"-{days} days",),
        ).fetchall()
        by_group = [(r["source_title"], r["cnt"]) for r in group_rows]

        lang_rows = conn.execute(
            "SELECT matched_lang, COUNT(*) AS cnt FROM forwarded_stats "
            "WHERE forwarded_at >= datetime('now', ?) GROUP BY matched_lang",
            (f"-{days} days",),
        ).fetchall()
        by_lang = {r["matched_lang"] or "unknown": r["cnt"] for r in lang_rows}

        tier_rows = conn.execute(
            "SELECT match_tier, COUNT(*) AS cnt FROM forwarded_stats "
            "WHERE forwarded_at >= datetime('now', ?) GROUP BY match_tier",
            (f"-{days} days",),
        ).fetchall()
        by_tier = {r["match_tier"]: r["cnt"] for r in tier_rows}

    return {
        "total": total, "today": today,
        "by_group": by_group, "by_lang": by_lang,
        "by_tier": by_tier, "days": days,
    }


def get_daily_summary() -> dict:
    with _connection() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM forwarded_stats "
            "WHERE date(forwarded_at) = date('now')"
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        group_rows = conn.execute(
            "SELECT source_title, COUNT(*) AS cnt FROM forwarded_stats "
            "WHERE date(forwarded_at) = date('now') "
            "GROUP BY source_chat_id ORDER BY cnt DESC LIMIT 5"
        ).fetchall()
        by_group = [(r["source_title"], r["cnt"]) for r in group_rows]

        lang_rows = conn.execute(
            "SELECT matched_lang, COUNT(*) AS cnt FROM forwarded_stats "
            "WHERE date(forwarded_at) = date('now') GROUP BY matched_lang"
        ).fetchall()
        by_lang = {r["matched_lang"] or "unknown": r["cnt"] for r in lang_rows}

    return {"total": total, "by_group": by_group, "by_lang": by_lang}