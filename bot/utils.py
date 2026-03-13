"""
utils.py – Shared utility functions.

KEY FIX (v2)
------------
Switched from Markdown to HTML parse mode everywhere.
Job messages frequently contain *, _, [ characters (prices, usernames, links)
that broke Markdown parsing silently — safe_send_message caught the exception,
logged it, but the message was already marked as processed and lost forever.
HTML mode uses explicit <b> tags on controlled text and html.escape() on all
user-supplied content, so it never fails due to special characters.
"""

import asyncio
import html
import logging
import sys
from datetime import datetime, timezone, timedelta

# Korea Standard Time = UTC+9 (no daylight saving)
KST = timezone(timedelta(hours=9))
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import User, Chat, Channel, Message

from bot.config import LOG_LEVEL

logger = logging.getLogger(__name__)


# ── Logging ───────────────────────────────────────────────────────

def setup_logging(name: str = "job_bot") -> logging.Logger:
    """
    Configure root logger with console + file handlers.
    Call once at the start of each entry-point script.
    """
    log = logging.getLogger()
    # Don't add duplicate handlers if called multiple times
    if log.handlers:
        return logging.getLogger(name)

    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    # File
    fh = logging.FileHandler("logs/job_bot.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)

    # Silence noisy third-party loggers
    for noisy in ("telethon", "aiogram", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(name)


# ── Message formatting (HTML) ─────────────────────────────────────

def build_job_post(
    *,
    group_title: str,
    group_link: Optional[str],
    author_name: str,
    author_link: Optional[str],
    message_time: datetime,
    message_text: str,
    matched_keywords: Optional[list] = None,
) -> str:
    """
    Build the formatted job-post HTML string for the target group.

    Template (Uzbek):
        ⚠️ Yangi xabar ma'lumotlari:

        Guruh: <group name>
        Xabar egasi: <sender>
        Xabar vaqti: <KST timestamp>

        Xabar matni:
        <message text>

    ALL user-supplied strings are passed through html.escape() so that
    special characters in group names, usernames, or message bodies
    can never break the HTML parser or cause send failures.
    """
    # Convert to Korea Standard Time (UTC+9) before formatting
    if message_time.tzinfo is None:
        message_time = message_time.replace(tzinfo=timezone.utc)
    kst_time = message_time.astimezone(KST)
    time_str = kst_time.strftime("%Y-%m-%d %H:%M:%S")

    # Escape all user-supplied content
    safe_group_title  = html.escape(group_title)
    safe_author_name  = html.escape(author_name)
    safe_message_text = html.escape(message_text)

    # Group name — clickable link if a public username is available
    if group_link:
        group_part = f'<a href="{html.escape(group_link)}">{safe_group_title}</a>'
    else:
        group_part = safe_group_title

    # Sender — clickable link if a profile URL is available
    if author_link:
        owner_part = f'<a href="{html.escape(author_link)}">{safe_author_name}</a>'
    else:
        owner_part = safe_author_name

    return (
        f"<b>⚠️ Yangi ish e’loni:</b>\n\n"
        f"<b>Guruh:</b> {group_part}\n"
        f"<b>Muallif:</b> {owner_part}\n"
        f"<b>Vaqt:</b> {time_str}\n\n"
        f"<b>Xabar matni:</b>\n"
        f"{safe_message_text}"
    )


# ── Telegram entity helpers ───────────────────────────────────────

def get_chat_link(entity) -> Optional[str]:
    """Return a public t.me link if the entity has a username."""
    username = getattr(entity, "username", None)
    return f"https://t.me/{username}" if username else None


def get_user_link(user: User) -> Optional[str]:
    """Return a t.me link for a user (username preferred, else tg deep-link)."""
    if getattr(user, "username", None):
        return f"https://t.me/{user.username}"
    return f"tg://user?id={user.id}"


def get_sender_display_name(sender) -> str:
    """Return a human-readable display name for any Telegram entity."""
    if sender is None:
        return "Unknown"
    if isinstance(sender, User):
        parts = [sender.first_name or "", sender.last_name or ""]
        return " ".join(p for p in parts if p).strip() or f"User#{sender.id}"
    return getattr(sender, "title", None) or str(sender.id)


def get_chat_display_name(chat) -> str:
    """Return a human-readable name for a chat/channel."""
    if chat is None:
        return "Unknown Group"
    return (
        getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or str(chat.id)
    )


# ── Safe send (HTML mode) ─────────────────────────────────────────

# ── Safe send (HTML mode) ─────────────────────────────────────────

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds between non-FloodWait retries


async def safe_send_message(
    client: TelegramClient,
    target,
    text: str,
) -> bool:
    """
    Send a message in HTML parse mode with intelligent retry logic.

    Handles two distinct failure modes differently:

    FloodWaitError — Telegram imposes a mandatory wait and tells us exactly
        how many seconds to wait. We must respect that number exactly.
        Retrying before the wait expires always fails again and wastes the
        retry budget. FloodWait does NOT consume a retry attempt — after
        sleeping the required time we try again on top of the normal budget.
        (The old loop-based approach counted FloodWait as a failed attempt,
        so if it hit on attempt 3 we would sleep correctly but then exit the
        loop and return False without actually retrying.)

    All other errors — network hiccups, temporary server errors, etc.
        Use exponential back-off (5s, 10s) before retrying.

    Returns True on success, False after all retries are exhausted.
    """
    from telethon.errors import FloodWaitError

    attempt = 0
    while attempt < MAX_RETRIES:
        attempt += 1
        try:
            await client.send_message(
                target,
                text,
                parse_mode="html",
                link_preview=False,
            )
            return True

        except FloodWaitError as e:
            # Telegram gave us the exact number of seconds we must wait.
            # Add a 5-second buffer so we don't immediately hit the limit again.
            # Crucially: do NOT increment attempt — the wait was forced by
            # Telegram, not a failure on our part. We will retry after sleeping.
            wait = e.seconds + 5
            logger.warning(
                "[utils] ⏳ FloodWait %ds (attempt %d/%d) — sleeping %ds then retrying",
                e.seconds, attempt, MAX_RETRIES, wait,
            )
            await asyncio.sleep(wait)
            attempt -= 1  # undo the increment — FloodWait does not count

        except Exception as exc:
            logger.warning(
                "[utils] Send attempt %d/%d to %s failed: %s",
                attempt, MAX_RETRIES, target, exc,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    logger.error(
        "[utils] All %d send attempts to %s failed — message dropped.",
        MAX_RETRIES, target,
    )
    return False


# ── Misc ──────────────────────────────────────────────────────────

def utcnow() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def truncate(text: str, max_len: int = 3500) -> str:
    """
    Truncate text to fit inside Telegram's 4096-char message limit.
    Leaves generous room for the surrounding HTML template (~500 chars).
    """
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"