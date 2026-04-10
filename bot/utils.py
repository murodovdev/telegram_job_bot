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

KEY FIX (v3)
------------
safe_send_message uchun uchta muhim tuzatish:

1. Har client uchun alohida lock (_client_states dict).
   Ilgari: bitta global lock — client_1 FloodWait olganda client_2 ham
   10+ daqiqa bloklanardi.
   Endi: har client mustaqil ishlaydi.

2. FloodWait paytida lock bo'shatiladi.
   Ilgari: asyncio.sleep(600) davomida lock ushlab turilardi —
   navbatdagi BARCHA xabarlar 10 daqiqa qotib qolardi.
   Endi: lock bo'shatiladi → sleep → lock qayta olinadi.

3. Lock lazy init race condition tuzatildi.
   Ilgari: ikkita coroutine bir vaqtda _send_lock is None ko'rib,
   ikki xil Lock yaratar edi — rate limiter ishlamas edi.
   Endi: har client uchun dict da bir marta yaratiladi.
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
        f"<b>⚠️ Yangi ish e'loni:</b>\n\n"
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

MAX_RETRIES    = 3
RETRY_DELAY    = 5      # seconds between non-FloodWait retries
_SEND_MIN_GAP  = 3.1    # minimum seconds between sends per client

# ── Per-client rate limiter ───────────────────────────────────────
#
# MUAMMO (v2 da):
#   Bitta global _send_lock va _last_send_at bor edi.
#   client_1 FloodWait (600s) olganda — lock ushlab turib uxlardi.
#   client_2 ham shu lockni kutib, 10+ daqiqa bloklanardi.
#   Natija: barcha xabarlar 10-15 daqiqa kechikardi.
#
# YECHIM (v3):
#   Har bir client uchun mustaqil lock va last_send vaqti.
#   _client_states[id(client)] = {"lock": Lock, "last_send": float}
#
#   Foyda 1: client_1 bloklanса — client_2 mustaqil ishlayveradi.
#   Foyda 2: FloodWait paytida lock bo'shatiladi (quyida tushuntirish).
#   Foyda 3: Race condition yo'q — dict yozuvi birinchi chaqiruvda,
#            event loop single-threaded bo'lgani uchun xavfsiz.

_client_states: dict[int, dict] = {}


def _get_client_state(client: TelegramClient) -> dict:
    """
    Berilgan client uchun state dict qaytaradi.
    Yo'q bo'lsa yaratadi — dict lookup O(1), xavfsiz.
    """
    cid = id(client)
    if cid not in _client_states:
        _client_states[cid] = {
            "lock":      asyncio.Lock(),
            "last_send": 0.0,
        }
    return _client_states[cid]


async def safe_send_message(
    client: TelegramClient,
    target,
    text: str,
) -> Optional[Any]:
    """
    HTML formatda xabar yuboradi: per-client rate limiting + retry.

    v3 tuzatishlari:
      • Har client uchun alohida lock — biri bloklanса ikkinchisi ishlaydi.
      • FloodWait paytida lock bo'shatiladi — navbat qotib qolmaydi.
      • Rate-limit sleep ham lock ichida, lekin FloodWait emas.

    Muvaffaqiyatda: Message ob'ektini qaytaradi (.id atributi bor).
    Xatoda (3 urinishdan keyin): None.
    """
    from telethon.errors import FloodWaitError

    state = _get_client_state(client)
    lock  = state["lock"]

    async with lock:
        # ── Rate limiting ─────────────────────────────────────────
        # Oxirgi yuborishdan beri _SEND_MIN_GAP o'tganmi?
        # Bu sleep lock ICHIDA — boshqa coroutine bu clientga
        # yuborishga urinmaydi (to'g'ri xatti-harakat).
        elapsed = time.monotonic() - state["last_send"]
        if state["last_send"] > 0 and elapsed < _SEND_MIN_GAP:
            gap = _SEND_MIN_GAP - elapsed
            logger.debug("[utils] rate-limit gap: %.2fs | client_id=%s", gap, id(client))
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
                state["last_send"] = time.monotonic()
                return sent

            except FloodWaitError as e:
                flood_wait = e.seconds + 5
                logger.warning(
                    "[utils] ⚠️ FloodWait %ds | client_id=%s | "
                    "lock bo'shatildi — navbat kutmaydi",
                    e.seconds, id(client),
                )
                # ── ASOSIY TUZATISH ───────────────────────────────
                # v2 da: await asyncio.sleep(600) lock ICHIDA edi.
                # Natija: navbatdagi barcha xabarlar 10 daqiqa qotardi.
                #
                # v3 da: lock bo'shatiladi → uxlaymiz → lock qayta olinadi.
                # Navbatdagi xabarlar bu vaqtda ham kutadi, chunki
                # Telegram ularni ham bloklar — lekin lock bo'sh bo'lgani
                # uchun boshqa client (client_2) to'siqsiz ishlayveradi.
                lock.release()
                try:
                    await asyncio.sleep(flood_wait)
                finally:
                    # finally: exception bo'lsa ham lock qayta olinadi
                    await lock.acquire()

                # FloodWait dan keyin attempt hisoblanmaydi — qayta urinish
                attempt -= 1

            except Exception as exc:
                logger.warning(
                    "[utils] Yuborish %d/%d muvaffaqiyatsiz | target=%s | xato: %s",
                    attempt, MAX_RETRIES, target, exc,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)

        logger.error(
            "[utils] ❌ %d urinishdan keyin yuborilmadi — xabar tushirildi. "
            "client_id=%s | target=%s",
            MAX_RETRIES, id(client), target,
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