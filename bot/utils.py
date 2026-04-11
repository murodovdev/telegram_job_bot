"""
utils.py – Shared utility functions.

KEY FIX (v3) — Rate limiter to eliminate 12-minute FloodWait delays
--------------------------------------------------------------------
Root cause: multiple groups post at once → bot sends them all rapidly
→ Telegram returns FloodWait 600s → all subsequent posts wait 10+ min.

Fix: 3.1-second minimum gap between sends via asyncio.Lock.
Result: 1 post=instant, 5 posts=0s,3s,6s,9s,12s. Never 600s.
"""

import asyncio
import html
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

KST = timezone(timedelta(hours=9))

from telethon import TelegramClient
from telethon.tl.types import User, Chat, Channel, Message

from bot.config import LOG_LEVEL

logger = logging.getLogger(__name__)


def setup_logging(name: str = "job_bot") -> logging.Logger:
    log = logging.getLogger()
    if log.handlers:
        return logging.getLogger(name)
    log.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    fh = logging.FileHandler("logs/job_bot.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    for noisy in ("telethon", "aiogram", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)


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
    if message_time.tzinfo is None:
        message_time = message_time.replace(tzinfo=timezone.utc)
    kst_time = message_time.astimezone(KST)
    time_str = kst_time.strftime("%Y-%m-%d %H:%M:%S")
    safe_group_title  = html.escape(group_title)
    safe_author_name  = html.escape(author_name)
    safe_message_text = html.escape(message_text)
    group_part = (
        f'<a href="{html.escape(group_link)}">{safe_group_title}</a>'
        if group_link else safe_group_title
    )
    owner_part = (
        f'<a href="{html.escape(author_link)}">{safe_author_name}</a>'
        if author_link else safe_author_name
    )
    return (
        f"<b>⚠️ Yangi ish e'loni:</b>\n\n"
        f"<b>Guruh:</b> {group_part}\n"
        f"<b>Muallif:</b> {owner_part}\n"
        f"<b>Vaqt:</b> {time_str}\n\n"
        f"<b>Xabar matni:</b>\n"
        f"{safe_message_text}"
    )


def get_chat_link(entity) -> Optional[str]:
    username = getattr(entity, "username", None)
    return f"https://t.me/{username}" if username else None


def get_user_link(user: User) -> Optional[str]:
    if getattr(user, "username", None):
        return f"https://t.me/{user.username}"
    return f"tg://user?id={user.id}"


def get_sender_display_name(sender) -> str:
    if sender is None:
        return "Unknown"
    if isinstance(sender, User):
        parts = [sender.first_name or "", sender.last_name or ""]
        return " ".join(p for p in parts if p).strip() or f"User#{sender.id}"
    return getattr(sender, "title", None) or str(sender.id)


def get_chat_display_name(chat) -> str:
    if chat is None:
        return "Unknown Group"
    return (
        getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or str(chat.id)
    )


# ── Safe send with rate limiting ──────────────────────────────────
#
# THE FIX FOR 12-MINUTE DELAYS
# ─────────────────────────────
# Problem: multiple simultaneous posts → bot fires them all at once
#          → Telegram: FloodWait 600s → 10-12 minute delay
#
# Fix: asyncio.Lock + 3.1s minimum gap between sends
#      → Telegram never issues FloodWait
#      → 1 post = instant, 5 posts = 0s/3s/6s/9s/12s

MAX_RETRIES   = 3
RETRY_DELAY   = 5
_send_lock    : Any   = None   # created lazily inside event loop
_last_send_at : float = 0.0
_SEND_MIN_GAP : float = 3.1    # seconds — keeps us under Telegram's limit


async def safe_send_message(
    client: TelegramClient,
    target,
    text: str,
) -> Optional[Any]:
    """
    Send in HTML mode with rate limiting and retry.

    Rate limiting: enforces 3.1s minimum between sends (prevents FloodWait).
    A single isolated post has zero extra wait.
    Returns sent Message object on success, None on failure.
    """
    from telethon.errors import FloodWaitError
    global _send_lock, _last_send_at

    if _send_lock is None:
        _send_lock = asyncio.Lock()

    async with _send_lock:
        elapsed = time.monotonic() - _last_send_at
        if _last_send_at > 0 and elapsed < _SEND_MIN_GAP:
            gap = _SEND_MIN_GAP - elapsed
            logger.debug("[utils] rate-limit pause %.2fs", gap)
            await asyncio.sleep(gap)

        attempt = 0
        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                sent = await client.send_message(
                    target, text, parse_mode="html", link_preview=False,
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
                attempt -= 1

            except Exception as exc:
                logger.warning(
                    "[utils] Send attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)

        logger.error("[utils] All %d attempts failed — message dropped.", MAX_RETRIES)
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def truncate(text: str, max_len: int = 3500) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"