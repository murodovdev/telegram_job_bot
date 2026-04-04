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
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

# Korea Standard Time = UTC+9 (no daylight saving)
KST = timezone(timedelta(hours=9))

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
RETRY_DELAY = 5              # seconds between non-FloodWait retries
_send_lock: Any = None       # asyncio.Lock — lazy-initialised in async context
_last_send_at: float = 0.0
_SEND_MIN_INTERVAL: float = 3.0  # minimum seconds between consecutive sends


async def safe_send_message(
    client: TelegramClient,
    target,
    text: str,
) -> Optional[Any]:
    """
    Send a message in HTML parse mode with rate limiting and retry logic.

    Rate limiting (eliminates FloodWait-induced delays)
    ---------------------------------------------------
    All sends are serialised through asyncio.Lock with a 3-second minimum
    gap. This prevents burst forwarding from triggering Telegram FloodWait
    (which caused 10-12 minute delays when multiple groups posted at once).

    Returns the sent Message object on success (truthy, has .id attribute),
    None on failure (falsy). Callers use `if sent_msg:` as before; callers
    needing the message ID use `sent_msg.id` directly.
    """
    from telethon.errors import FloodWaitError
    global _send_lock, _last_send_at

    if _send_lock is None:
        _send_lock = asyncio.Lock()

    async with _send_lock:
        # Enforce minimum interval between sends
        elapsed = time.monotonic() - _last_send_at
        if _last_send_at > 0 and elapsed < _SEND_MIN_INTERVAL:
            gap = _SEND_MIN_INTERVAL - elapsed
            logger.debug("[utils] rate-limit pause %.2fs before send", gap)
            await asyncio.sleep(gap)

        attempt = 0
        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                sent = await client.send_message(
                    target,
                    text,
                    parse_mode="html",
                    link_preview=False,
                )
                _last_send_at = time.monotonic()
                return sent

            except FloodWaitError as e:
                wait = e.seconds + 5
                logger.warning(
                    "[utils] FloodWait %ds (attempt %d/%d) — sleeping %ds",
                    e.seconds, attempt, MAX_RETRIES, wait,
                )
                await asyncio.sleep(wait)
                attempt -= 1  # FloodWait does not count as a retry

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
        return None


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