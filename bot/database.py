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

Changes in v5 (user blocking)
------------------------------
• New table: blocked_users — stores permanently blocked Telegram user IDs.
• New functions: block_user(), unblock_user(), is_blocked(), list_blocked_users().
• is_blocked() is called on the hot-path in monitor.py (Gate 2.5) using only
  message.sender_id — no Telegram API call needed, zero latency overhead.
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
class BlockedUser:
    user_id:    int
    full_name:  str
    username:   Optional[str]
    reason:     Optional[str]
    blocked_at: str


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

            -- Blocked users: messages from these senders are never forwarded
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id    INTEGER PRIMARY KEY,
                full_name  TEXT    NOT NULL DEFAULT '',
                username   TEXT,
                reason     TEXT,
                blocked_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Content fingerprints for cross-group duplicate detection.
            -- Stores a SHA-256 hash of each forwarded message's normalised text
            -- plus the first 500 chars of normalised text for fuzzy comparison.
            -- Rows older than DEDUP_WINDOW_HOURS are pruned on startup.
            CREATE TABLE IF NOT EXISTS content_hashes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                text_hash   TEXT    NOT NULL,
                norm_text   TEXT    NOT NULL DEFAULT '',
                source_chat INTEGER NOT NULL,
                source_msg  INTEGER NOT NULL,
                seen_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_ch_hash ON content_hashes (text_hash);
            CREATE INDEX IF NOT EXISTS idx_ch_time ON content_hashes (seen_at);

            -- forwarded_msgs: har forward qilingan post uchun
            -- (source_chat, source_msg) → target_msg_id mapping.
            -- "Odam olindi" feature: manba guruhda reply/edit kelganda
            -- target guruhda tegishli postni topib edit qilish uchun ishlatiladi.
            -- 48 soatdan eski yozuvlar tozalanadi (Telegram edit limiti).
            CREATE TABLE IF NOT EXISTS forwarded_msgs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat    INTEGER NOT NULL,
                source_msg     INTEGER NOT NULL,
                sender_id      INTEGER,
                target_chat    INTEGER NOT NULL,
                target_msg_id  INTEGER NOT NULL,
                post_text      TEXT    NOT NULL DEFAULT '',
                forwarded_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                UNIQUE (source_chat, source_msg)
            );
            CREATE INDEX IF NOT EXISTS idx_fwd_source
                ON forwarded_msgs (source_chat, source_msg);
            CREATE INDEX IF NOT EXISTS idx_fwd_sender
                ON forwarded_msgs (source_chat, sender_id);
            CREATE INDEX IF NOT EXISTS idx_fwd_time
                ON forwarded_msgs (forwarded_at);

            -- Halal review queue: harom deb topilgan ish e'lonlari admin
            -- tasdiqlashini kutadi. Admin "Tasdiqlash" bosganda guruhga yuboriladi,
            -- "Rad etish" bosganda o'chiriladi.
            CREATE TABLE IF NOT EXISTS halal_review_queue (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source_chat  INTEGER NOT NULL,
                source_msg   INTEGER NOT NULL,
                post_text    TEXT    NOT NULL,
                haram_reason TEXT    NOT NULL DEFAULT '',
                haram_source TEXT    NOT NULL DEFAULT '',
                group_title  TEXT    NOT NULL DEFAULT '',
                group_link   TEXT    NOT NULL DEFAULT '',
                author_name  TEXT    NOT NULL DEFAULT '',
                author_link  TEXT    NOT NULL DEFAULT '',
                msg_time     TEXT    NOT NULL DEFAULT '',
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            -- bot_settings: generic key-value store for runtime configuration.
            -- Used for features like the AI filter toggle that need to survive restarts.
            CREATE TABLE IF NOT EXISTS bot_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)

        # ── Safe migrations ───────────────────────────────────────
        # SQLite does not support ALTER TABLE ADD COLUMN IF NOT EXISTS,
        # so we attempt each migration and silently ignore already-exists errors.
        try:
            conn.execute(
                "ALTER TABLE source_groups "
                "ADD COLUMN assigned_account INTEGER NOT NULL DEFAULT 1"
            )
            logger.info("[DB] Migrated: added assigned_account column")
        except sqlite3.OperationalError:
            pass  # column already exists — normal on subsequent startups

        # Migrate forwarded_msgs — add sender_id if missing
        try:
            conn.execute(
                "ALTER TABLE forwarded_msgs ADD COLUMN sender_id INTEGER"
            )
            logger.info("[DB] Migrated: added sender_id to forwarded_msgs")
        except sqlite3.OperationalError:
            pass

        # Migrate halal_review_queue — add metadata columns if missing
        for col, definition in [
            ("group_title", "TEXT NOT NULL DEFAULT ''"),
            ("group_link",  "TEXT NOT NULL DEFAULT ''"),
            ("author_name", "TEXT NOT NULL DEFAULT ''"),
            ("author_link", "TEXT NOT NULL DEFAULT ''"),
            ("msg_time",    "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE halal_review_queue ADD COLUMN {col} {definition}"
                )
                logger.info("[DB] Migrated: added %s to halal_review_queue", col)
            except sqlite3.OperationalError:
                pass  # already exists

    logger.info("[DB] Schema initialised at %s", DATABASE_PATH)

    # Startup maintenance: prune all tables that would grow forever.
    from bot.config import DEDUP_WINDOW_HOURS
    cleanup_content_hashes(DEDUP_WINDOW_HOURS)
    cleanup_processed_msgs(keep_days=30)
    cleanup_original_msg_index(keep_days=7)

    # Warm the in-memory caches immediately so the first message after
    # startup does not pay the cold-cache DB round-trip penalty.
    _cache_refresh_source_groups()
    _cache_refresh_blocked_users()
    _cache_refresh_target()


# ── In-memory hot-path cache ─────────────────────────────────────
#
# Gate 1  (source-group check)  and Gate 2.5 (blocked-user check)  are
# called on EVERY incoming message. Opening a fresh SQLite connection per
# call blocks the asyncio event loop for ~3–5 ms each time. With 42 groups
# active and many non-job messages arriving, this adds up to tens of
# milliseconds of stall per second — enough to cause the multi-second
# delays observed in production.
#
# Solution: maintain three in-process sets/values that mirror the DB.
# They are refreshed after every write that modifies the relevant table,
# so they are always consistent with the DB without any polling or TTL.
# Read operations (the hot path) become pure Python set lookups — O(1),
# no I/O, no event-loop blocking.

_source_group_ids_by_account: dict = {}  # {account_num: frozenset(chat_ids)}
_blocked_user_ids: frozenset = frozenset()
_cached_target_id: Optional[int] = None
# Separate flag so is_blocked_cached() does not re-query the DB on every
# message when the blocked list is legitimately empty (frozenset() is falsy,
# so "if not _blocked_user_ids" would always be True in that case).
_blocked_cache_ready: bool = False


def _cache_refresh_source_groups() -> None:
    """Rebuild the source-group ID cache from the database."""
    global _source_group_ids_by_account
    with _connection() as conn:
        rows = conn.execute(
            "SELECT chat_id, assigned_account FROM source_groups"
        ).fetchall()
    by_account: dict = {}
    for row in rows:
        acct = row["assigned_account"]
        by_account.setdefault(acct, set()).add(row["chat_id"])
    _source_group_ids_by_account = {k: frozenset(v) for k, v in by_account.items()}
    logger.debug("[DB] source-group cache refreshed: %s",
                 {k: len(v) for k, v in _source_group_ids_by_account.items()})


def _cache_refresh_blocked_users() -> None:
    """Rebuild the blocked-user ID cache from the database."""
    global _blocked_user_ids, _blocked_cache_ready
    with _connection() as conn:
        rows = conn.execute("SELECT user_id FROM blocked_users").fetchall()
    _blocked_user_ids = frozenset(r["user_id"] for r in rows)
    _blocked_cache_ready = True
    logger.debug("[DB] blocked-user cache refreshed: %d entries", len(_blocked_user_ids))


def _cache_refresh_target() -> None:
    """Refresh the cached target group ID."""
    global _cached_target_id
    with _connection() as conn:
        row = conn.execute("SELECT chat_id FROM target_group WHERE id = 1").fetchone()
    _cached_target_id = row["chat_id"] if row else None
    logger.debug("[DB] target cache refreshed: %s", _cached_target_id)


def get_source_group_ids_cached(account: Optional[int] = None) -> frozenset:
    """
    O(1) hot-path read — returns a frozenset from the in-memory cache.
    Falls back to a live DB query only if the cache is empty (first call
    before init_db has run, which should never happen in normal operation).
    """
    if not _source_group_ids_by_account:
        _cache_refresh_source_groups()
    if account is None:
        # Union of all accounts
        result: set = set()
        for ids in _source_group_ids_by_account.values():
            result |= ids
        return frozenset(result)
    return _source_group_ids_by_account.get(account, frozenset())


def is_blocked_cached(user_id: int) -> bool:
    """O(1) hot-path read from the in-memory blocked-user set.
    Uses _blocked_cache_ready flag instead of truthiness so an empty
    blocked list does not cause a DB round-trip on every message."""
    if not _blocked_cache_ready:
        _cache_refresh_blocked_users()
    return user_id in _blocked_user_ids


def get_cached_target_id() -> Optional[int]:
    """Return the cached target group chat_id. Refreshes once if not set."""
    if _cached_target_id is None:
        _cache_refresh_target()
    return _cached_target_id


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
        _cache_refresh_source_groups()
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
        _cache_refresh_source_groups()
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
        _cache_refresh_source_groups()
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
    _cache_refresh_target()


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


# ── Content deduplication ─────────────────────────────────────────
#
# Design rationale
# ────────────────
# Gate 3 (repost check) catches Telegram-native forwards where fwd_from is
# present. It does NOT catch the most common real-world spam pattern: a user
# (or multiple different users) manually copying and pasting the same job
# listing text into many groups. Each copy has a different chat_id and
# message_id, so Gate 3 and Gate 4 both pass silently.
#
# This section adds Gate 5.5 — a two-tier content fingerprinting check that
# runs only on confirmed job posts (after the keyword filter), keeping the
# dedup table small and the check fast:
#
#   Tier 1 — Exact hash:  SHA-256 of normalised text.  O(1) DB lookup.
#             Catches perfect copy-paste with zero false positives.
#
#   Tier 2 — Fuzzy match: SequenceMatcher (Python stdlib) similarity ratio
#             against all normalised texts seen within the dedup window.
#             Catches minor edits, appended phone numbers, extra blank lines.
#             Only runs when the exact hash misses (typically < 200 rows to
#             compare against — well within SQLite performance limits).
#
# Hash registration happens AFTER a successful send — same discipline as
# mark_processed() — so a failed send never permanently blacklists content.

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher


def _normalise_text(text: str) -> str:
    r"""
    Produce a canonical form of text for duplicate comparison.

    Steps:
    1. Unicode NFC normalisation (same character, different byte sequences)
    2. Lowercase
    3. Strip URLs (they vary but content is the same)
    4. Strip phone number patterns — ONLY clear phone formats:
         +82-010-1234-5678, 010 1234 5678, (010)12345678 etc.
       The old regex [\d\s\-\+\(\)]{7,} was too broad — it also ate
       sentences like "5 nafar. Ish vaqti 08:00-17:00", turning different
       job posts into the same normalised string (false positives).
    5. Collapse all whitespace to single spaces
    6. Strip leading/trailing whitespace
    """
    # Unicode normalise
    text = unicodedata.normalize("NFC", text)
    # Lowercase (safe for Korean — it has no case)
    text = text.lower()
    # Remove URLs
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    # Remove phone numbers: optional + or country code, then groups of digits
    # separated by spaces/dashes/dots, total digit count >= 7.
    # Uses a tighter pattern that requires an optional leading + or digits-only
    # prefix so it does not eat arbitrary mixed digit-space text.
    text = re.sub(r"\+?[\d]{1,3}[\s\-\.]?[\(\d][\d\s\-\.\(\)]{5,}\d", " ", text)
    # Collapse all whitespace (spaces, newlines, tabs) to a single space
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _content_hash(text: str) -> str:
    """SHA-256 hex digest of the normalised text. Public so monitor.py can
    compute the hash cheaply before taking the _pending_hashes lock."""
    return hashlib.sha256(_normalise_text(text).encode("utf-8")).hexdigest()


def is_content_duplicate(
    text: str,
    window_hours: int = 24,
    similarity_threshold: float = 0.85,
) -> tuple:
    """
    Two-tier duplicate check for a candidate job-post text.

    Returns (is_dup: bool, reason: str).

    Tier 1 — Exact:  O(1) indexed hash lookup.
    Tier 2 — Fuzzy:  O(n) SequenceMatcher against recent norm_text rows,
                     where n = number of posts forwarded in the last window_hours
                     (typically < 200 — well within performance limits).

    The window_hours=0 escape hatch disables the check entirely.
    """
    if window_hours <= 0:
        return False, ""

    norm  = _normalise_text(text)
    h     = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    since = f"-{window_hours} hours"

    with _connection() as conn:
        # Tier 1: exact hash
        exact = conn.execute(
            "SELECT source_chat, source_msg FROM content_hashes "
            "WHERE text_hash = ? AND seen_at >= datetime('now', ?)",
            (h, since),
        ).fetchone()
        if exact:
            return True, (
                f"exact duplicate of msg {exact['source_msg']} "
                f"in chat {exact['source_chat']}"
            )

        # Tier 2: fuzzy — only if there are recent rows to compare against
        # Truncate norm to 500 chars; similarity on longer texts is expensive
        # and job listings rarely exceed that after normalisation.
        norm_sample = norm[:500]
        if not norm_sample:
            return False, ""

        recent_rows = conn.execute(
            "SELECT id, norm_text, source_chat, source_msg FROM content_hashes "
            "WHERE seen_at >= datetime('now', ?)",
            (since,),
        ).fetchall()

    for row in recent_rows:
        stored = row["norm_text"][:500]
        if not stored:
            continue
        ratio = SequenceMatcher(None, norm_sample, stored, autojunk=False).ratio()
        if ratio >= similarity_threshold:
            return True, (
                f"near-duplicate (similarity={ratio:.2f}) of msg "
                f"{row['source_msg']} in chat {row['source_chat']}"
            )

    return False, ""


def record_content_hash(
    text: str,
    source_chat: int,
    source_msg: int,
) -> None:
    """
    Store the content fingerprint of a successfully forwarded job post.
    Called AFTER send succeeds — never before — so failed sends don't
    permanently blacklist content.
    """
    norm = _normalise_text(text)
    h    = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    with _connection() as conn:
        conn.execute(
            "INSERT INTO content_hashes (text_hash, norm_text, source_chat, source_msg) "
            "VALUES (?, ?, ?, ?)",
            (h, norm[:500], source_chat, source_msg),
        )


def cleanup_content_hashes(window_hours: int = 24) -> int:
    """
    Delete content_hashes rows older than window_hours.
    Called on startup and by the daily scheduler to keep the table lean.
    Returns the number of rows deleted.
    """
    if window_hours <= 0:
        return 0
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM content_hashes WHERE seen_at < datetime('now', ?)",
            (f"-{window_hours} hours",),
        )
        deleted = cursor.rowcount
    if deleted:
        logger.info("[DB] Pruned %d expired content_hashes rows", deleted)
    return deleted


def cleanup_processed_msgs(keep_days: int = 30) -> int:
    """
    Delete processed_msgs rows older than keep_days.

    Without cleanup this table grows forever — 42 monitored groups each
    receiving ~100 messages/day = 4200 rows/day = 1.5M rows/year.
    30 days is enough to prevent any realistic duplicate from slipping through.
    Returns the number of rows deleted.
    """
    if keep_days <= 0:
        return 0
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM processed_msgs WHERE processed_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        )
        deleted = cursor.rowcount
    if deleted:
        logger.info("[DB] Pruned %d old processed_msgs rows (keep_days=%d)", deleted, keep_days)
    return deleted


def cleanup_original_msg_index(keep_days: int = 7) -> int:
    """
    Delete original_msg_index rows older than keep_days.

    This table records forwarded-message origins to catch Telegram-native
    reposts.  A 7-day window is more than enough — nobody reposts a job
    listing a week later expecting it to match.
    Returns the number of rows deleted.
    """
    if keep_days <= 0:
        return 0
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM original_msg_index WHERE seen_at < datetime('now', ?)",
            (f"-{keep_days} days",),
        )
        deleted = cursor.rowcount
    if deleted:
        logger.info("[DB] Pruned %d old original_msg_index rows (keep_days=%d)", deleted, keep_days)
    return deleted


# ── Blocked users ─────────────────────────────────────────────────

def block_user(
    user_id: int,
    full_name: str = "",
    username: Optional[str] = None,
    reason: Optional[str] = None,
) -> bool:
    """
    Add a user to the blocked list.
    Returns True if newly blocked, False if already blocked.
    """
    try:
        with _connection() as conn:
            conn.execute(
                "INSERT INTO blocked_users (user_id, full_name, username, reason) "
                "VALUES (?, ?, ?, ?)",
                (user_id, full_name, username, reason),
            )
        logger.info("[DB] User blocked: %s (%s)", full_name or user_id, user_id)
        _cache_refresh_blocked_users()
        return True
    except sqlite3.IntegrityError:
        logger.warning("[DB] User already blocked: %s", user_id)
        return False


def unblock_user(user_id: int) -> bool:
    """
    Remove a user from the blocked list.
    Returns True if the user was found and removed.
    """
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM blocked_users WHERE user_id = ?", (user_id,)
        )
        removed = cursor.rowcount > 0
    if removed:
        logger.info("[DB] User unblocked: %s", user_id)
        _cache_refresh_blocked_users()
    return removed


def is_blocked(user_id: int) -> bool:
    """
    Hot-path check — called on every incoming message.
    Uses only the integer user_id, no Telegram API call needed.
    """
    if not user_id:
        return False
    with _connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def list_blocked_users() -> List[BlockedUser]:
    """Return all blocked users ordered by most recently blocked."""
    with _connection() as conn:
        rows = conn.execute(
            "SELECT * FROM blocked_users ORDER BY blocked_at DESC"
        ).fetchall()
    return [BlockedUser(**dict(row)) for row in rows]


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


def clear_all_stats() -> int:
    """
    Barcha forwarded_stats yozuvlarini o'chiradi.
    /clearstats admin buyrug'i uchun ishlatiladi.
    Qaytaradi: o'chirilgan qatorlar soni.
    """
    with _connection() as conn:
        cursor = conn.execute("DELETE FROM forwarded_stats")
        deleted = cursor.rowcount
    logger.info("[DB] Cleared %d forwarded_stats rows.", deleted)
    return deleted


# ── Halal review queue ────────────────────────────────────────────

# Forwarded messages index
# Maps (source_chat, source_msg) → target_msg_id for the "position filled" feature.

def save_forwarded_msg(
    source_chat: int,
    source_msg: int,
    target_chat: int,
    target_msg_id: int,
    post_text: str = "",
    sender_id: Optional[int] = None,
) -> None:
    """
    Forward qilingan xabarni DB ga saqlaydi.
    sender_id: ish beruvchining Telegram user ID si — reply bo'lmagan
               "odam olindi" holatida so'nggi postini topish uchun ishlatiladi.
    UNIQUE constraint: bir xil (source_chat, source_msg) uchun
    ikkinchi marta kelsa yangilanadi.
    """
    with _connection() as conn:
        conn.execute(
            """INSERT INTO forwarded_msgs
               (source_chat, source_msg, sender_id, target_chat, target_msg_id, post_text)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_chat, source_msg) DO UPDATE SET
                   sender_id     = excluded.sender_id,
                   target_msg_id = excluded.target_msg_id,
                   post_text     = excluded.post_text,
                   forwarded_at  = datetime('now')""",
            (source_chat, source_msg, sender_id, target_chat, target_msg_id, post_text),
        )


def get_last_forwarded_by_sender(source_chat: int, sender_id: int) -> Optional[dict]:
    """
    Shu guruhda o'sha sender_id dan eng oxirgi forward qilingan postni qaytaradi.
    Reply bo'lmagan 'odam olindi' holatida ishlatiladi:
    xabar yozgan odam kim? → uning so'nggi ish e'loni qaysi?
    Topilmasa None.
    """
    with _connection() as conn:
        row = conn.execute(
            """SELECT * FROM forwarded_msgs
               WHERE source_chat = ? AND sender_id = ?
               ORDER BY forwarded_at DESC LIMIT 1""",
            (source_chat, sender_id),
        ).fetchone()
    return dict(row) if row else None


def get_forwarded_msg(source_chat: int, source_msg: int) -> Optional[dict]:
    """
    Manba (chat_id, msg_id) bo'yicha target msg_id ni qaytaradi.
    Topilmasa None.
    """
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM forwarded_msgs WHERE source_chat=? AND source_msg=?",
            (source_chat, source_msg),
        ).fetchone()
    return dict(row) if row else None


def cleanup_forwarded_msgs(keep_hours: int = 48) -> int:
    """
    48 soatdan eski forwarded_msgs yozuvlarini o'chiradi.
    Telegram 48 soatdan keyin edit qilishga ruxsat bermaydi —
    eski yozuvlarni saqlashning ma'nosi yo'q.
    """
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM forwarded_msgs WHERE forwarded_at < datetime('now', ?)",
            (f"-{keep_hours} hours",),
        )
        deleted = cursor.rowcount
    if deleted:
        logger.info("[DB] Pruned %d old forwarded_msgs rows", deleted)
    return deleted


def add_to_review_queue(
    source_chat: int,
    source_msg: int,
    post_text: str,
    haram_reason: str = "",
    haram_source: str = "",
    group_title: str = "",
    group_link: str = "",
    author_name: str = "",
    author_link: str = "",
    msg_time: str = "",
) -> int:
    """
    Harom deb topilgan ish e'lonini review queue ga qo'shadi.
    Guruh va yuboruvchi ma'lumotlari ham saqlanadi — admin
    tasdiqlasa build_job_post() bilan to'liq format yuboriladi.
    Qaytaradi: yangi qator ID si (callback_data uchun ishlatiladi).
    """
    with _connection() as conn:
        cursor = conn.execute(
            "INSERT INTO halal_review_queue "
            "(source_chat, source_msg, post_text, haram_reason, haram_source, "
            " group_title, group_link, author_name, author_link, msg_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_chat, source_msg, post_text, haram_reason, haram_source,
             group_title, group_link, author_name, author_link, msg_time),
        )
        return cursor.lastrowid


def get_review_item(review_id: int) -> Optional[dict]:
    """Review queue dan bitta yozuvni oladi."""
    with _connection() as conn:
        row = conn.execute(
            "SELECT * FROM halal_review_queue WHERE id = ?", (review_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_review_item(review_id: int) -> None:
    """Review queue dan yozuvni o'chiradi (tasdiqlangan yoki rad etilgan)."""
    with _connection() as conn:
        conn.execute(
            "DELETE FROM halal_review_queue WHERE id = ?", (review_id,)
        )


def cleanup_review_queue(keep_hours: int = 48) -> int:
    """
    48 soatdan eski review queue yozuvlarini o'chiradi.
    Admin javob bermagan holatlar uchun.
    """
    with _connection() as conn:
        cursor = conn.execute(
            "DELETE FROM halal_review_queue WHERE created_at < datetime('now', ?)",
            (f"-{keep_hours} hours",),
        )
        deleted = cursor.rowcount
    if deleted:
        logger.info("[DB] Pruned %d old review_queue rows", deleted)
    return deleted

# ── Bot settings (runtime configuration) ─────────────────────────
#
# A simple key-value store that persists across restarts.
# Values are stored as strings; callers handle type conversion.
#
# AI filter toggle key: "ai_filter_enabled"
#   "1" = enabled (default), "0" = disabled

_AI_FILTER_KEY = "ai_filter_enabled"

# In-memory cache for the AI filter flag — avoids a DB read on every
# message. Invalidated immediately when set_setting() is called.
_ai_filter_cache: Optional[bool] = None


def get_setting(key: str, default: str = "") -> str:
    """Return the stored value for *key*, or *default* if not found."""
    with _connection() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """Upsert a key-value pair into bot_settings."""
    global _ai_filter_cache
    with _connection() as conn:
        conn.execute(
            """
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE
              SET value = excluded.value,
                  updated_at = excluded.updated_at
            """,
            (key, value),
        )
    # Invalidate the AI-filter in-memory cache if this key changed it.
    if key == _AI_FILTER_KEY:
        _ai_filter_cache = None
    logger.info("[DB] Setting updated: %s = %s", key, value)


def is_ai_filter_enabled() -> bool:
    """
    Return True if the Groq AI halal filter is currently enabled.

    Defaults to True (enabled) if the setting has never been written.
    Uses a module-level cache so monitor.py never hits SQLite on the
    hot path — the cache is only invalidated when set_setting() is called.
    """
    global _ai_filter_cache
    if _ai_filter_cache is None:
        raw = get_setting(_AI_FILTER_KEY, default="1")
        _ai_filter_cache = raw != "0"
    return _ai_filter_cache


def toggle_ai_filter() -> bool:
    """
    Flip the AI filter state.  Returns the NEW state (True = enabled).
    """
    current = is_ai_filter_enabled()
    new_state = not current
    set_setting(_AI_FILTER_KEY, "1" if new_state else "0")
    logger.info(
        "[DB] AI filter toggled: %s → %s",
        "ON" if current else "OFF",
        "ON" if new_state else "OFF",
    )
    return new_state