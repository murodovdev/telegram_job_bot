"""
monitor.py – Telethon userbot (dual-account).

Changes in v5
-------------
• Two Telethon clients (client_1, client_2) run concurrently.
• _make_message_handler(client, account_num) is a factory that returns a
  closure — each client gets its own handler that only processes messages
  from groups assigned to that account number in the DB.
• _on_chat_action runs on client_1 only (target group management).
• setup_monitor() starts and registers handlers for all active clients.
• main.py awaits asyncio.gather(*[c.run_until_disconnected() for c in all_clients])
  to keep both connections alive.
"""

import asyncio
import re
import logging
from datetime import timezone
from typing import Optional

from telethon import events
from telethon.tl.types import (
    User,
    Message,
    MessageActionChatAddUser,
    MessageActionChatJoinedByLink,
    MessageActionChatJoinedByRequest,
    MessageActionChatDeleteUser,
)

import bot.database as db
from bot.client import client_1, all_clients
from bot.config import PHONE_NUMBER, PHONE_NUMBER_2, TARGET_GROUP
from bot.config import DEDUP_WINDOW_HOURS, DEDUP_SIMILARITY_THRESHOLD
from bot.config import GROQ_API_KEY
from bot.filters import is_job_message
from bot.halal_filter import is_haram_job
from bot.groq_halal import check_halal_with_groq
from bot.notifier import notify_admin
from bot.utils import (
    build_job_post,
    get_chat_display_name,
    get_chat_link,
    get_sender_display_name,
    get_user_link,
    safe_send_message,
    truncate,
    utcnow,
)

logger = logging.getLogger(__name__)

# ── "Odam olindi" kalit so'zlar ──────────────────────────────────
# Manba guruhda ish e'loniga reply yoki edit orqali kelganda
# target guruhda post "ODAM OLINDI" deb edit qilinadi.
_FILLED_KEYWORDS: list = [
    # O'zbekcha
    "odam olindi", "olindi", "olind", "band bo'ldi", "band boldi", "to'ldi", "toldi",
    "tugadi", "yopildi", "ishchi topildi", "aktual emas", "kerak emas",
    "olingan", "band", "topildi", "yopilyapti", "bitdi", "Ishga olindi",
    # Ruscha
    "взяли", "занято", "нашли", "закрыто", "не актуально", "нашли человека",
    "место занято", "уже нашли", "закрыта", "набрали",
    # Koreys
    "채용완료", "마감", "충원완료", "구했어요", "채용됐어요", "마감됐어요",
    "뽑았어요", "완료", "마감입니다",
]

def _is_filled_signal(text: str) -> bool:
    """Matn 'odam olindi' signalimi tekshiradi."""
    if not text:
        return False
    low = text.lower()
    return any(kw in low for kw in _FILLED_KEYWORDS)


# ── Review queue sender ───────────────────────────────────────────
# Harom deb topilgan ish e'lonini admin botga yuboradi.
# Yuborish uchun aiogram Bot instance lazim — admin_bot.py dan olinadi.
_aiogram_bot = None

def set_aiogram_bot(bot) -> None:
    """admin_bot.py dan aiogram Bot instansini uzatish uchun."""
    global _aiogram_bot
    _aiogram_bot = bot


async def _send_to_review(
    text: str,
    source_chat: int,
    source_msg: int,
    haram_reason: str,
    haram_source: str,
    group_title: str = "",
    group_link: str = "",
    author_name: str = "",
    author_link: str = "",
    msg_time: str = "",
) -> bool:
    """
    Harom deb topilgan postni DB ga saqlaydi va admin botga
    'Tasdiqlash / Rad etish' tugmalar bilan yuboradi.
    Guruh va yuboruvchi metadata ham saqlanadi — approve da
    to'liq format bilan guruhga yuboriladi.
    Qaytaradi: True = muvaffaqiyatli, False = xato.
    """
    from bot.config import ADMIN_USER_ID
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    review_id = db.add_to_review_queue(
        source_chat=source_chat,
        source_msg=source_msg,
        post_text=text,
        haram_reason=haram_reason,
        haram_source=haram_source,
        group_title=group_title,
        group_link=group_link,
        author_name=author_name,
        author_link=author_link,
        msg_time=msg_time,
    )

    if haram_source == "keyword":
        source_label = "🔑 Kalit so'z"
    elif haram_source == "groq_unclear":
        source_label = "🤔 Groq AI (noaniq)"
    else:
        source_label = "🤖 Groq AI"
    preview = text[:400] + ("…" if len(text) > 400 else "")

    import html as _html
    from datetime import datetime, timedelta

    _KST = timezone(timedelta(hours=9))
    time_display = "—"
    if msg_time:
        try:
            _dt = datetime.fromisoformat(msg_time)
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
            time_display = _dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            time_display = msg_time

    _safe_group  = _html.escape(group_title or "—")
    _safe_author = _html.escape(author_name or "—")
    _group_part  = (
        f'<a href="{_html.escape(group_link)}">{_safe_group}</a>'
        if group_link else _safe_group
    )
    _author_part = (
        f'<a href="{_html.escape(author_link)}">{_safe_author}</a>'
        if author_link else _safe_author
    )

    notify_text = (
        f"⚠️ <b>Tekshirish kerak bo'lgan ish e'loni</b>\n\n"
        f"<b>Guruh:</b> {_group_part}\n"
        f"<b>Muallif:</b> {_author_part}\n"
        f"<b>Vaqt:</b> {time_display}\n\n"
        f"{source_label}: <i>{_html.escape(haram_reason)}</i>\n\n"
        f"<b>Xabar matni:</b>\n{_html.escape(preview)}"
    )

    # Fix 8: callback_data Telegram 64 bayt limitini tekshirish
    approve_cb = f"review:approve:{review_id}"
    reject_cb  = f"review:reject:{review_id}"
    assert len(approve_cb.encode()) <= 64, f"callback_data too long: {approve_cb}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Tasdiqlash — guruhga yuborish",
            callback_data=approve_cb,
        ),
        InlineKeyboardButton(
            text="❌ Rad etish",
            callback_data=reject_cb,
        ),
    ]])

    if _aiogram_bot:
        try:
            await _aiogram_bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=notify_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            logger.info("[monitor] Review request sent to admin | review_id=%d", review_id)
            return True
        except Exception as exc:
            logger.warning("[monitor] Failed to send review to admin: %s", exc)
            return False
    else:
        logger.warning("[monitor] _aiogram_bot not set — review not sent to admin")
        return False

# ── Race-condition guard for Gate 5.5 ────────────────────────────
#
# Problem: two accounts can receive the same copy-paste message within
# milliseconds of each other.  Both call is_content_duplicate() before
# either has finished sending and recording the hash, so both see "no
# duplicate" and both forward the post.
#
# Fix: maintain a module-level set of SHA-256 hashes that are currently
# in-flight (between duplicate-check and hash-registration).  The second
# coroutine to arrive sees the hash already in the set and drops the message.
# The set entry is always removed in a finally block so a failed send never
# permanently blocks the content.
_pending_hashes: set = set()

# ── Polling loop (backup — guarantees no missed messages) ────────
#
# ROOT CAUSE of 6-minute delays, finally confirmed:
#
# Telethon receives ALL updates from Telegram via MTProto push.
# Even with a dedicated account + chats= filter, the issue persists because:
#
#   1. The chats= filter is CLIENT-SIDE. Telegram still pushes every update
#      from every group the account is in. The filter just discards them
#      faster in Python — they still go through Telethon's _updates_queue.
#
#   2. Telethon's internal _updates_queue processes updates sequentially
#      before dispatching to handlers. During Telegram reconnections or
#      server-side update batching, the queue fills up. Source-group
#      messages wait behind irrelevant updates.
#
#   3. Telegram can delay MTProto push delivery for userbot accounts by
#      several minutes during server load peaks.
#
# Evidence: April 15 log shows 9 messages with 1-3s delay (queue empty)
# and many messages with 5-7 minute delay (queue backed up). Same code,
# same account, different queue state.
#
# SOLUTION: Two independent detection channels:
#   A) Event handler (existing) — real-time, ~1s when MTProto push works
#   B) Polling loop (new) — scans source groups every POLL_INTERVAL seconds
#      using get_messages() API. Catches EVERYTHING the event system missed.
#
# The polling loop is the standard approach used by all production
# Telegram monitoring bots (TGStat, Combot, etc.).
#
# Together: even if event delivery fails entirely, every job post is
# forwarded within POLL_INTERVAL seconds of being sent.

POLL_INTERVAL      = 15   # seconds between polling cycles per account
POLL_MESSAGES      = 15   # how many recent messages to check per group per cycle
POLL_MAX_AGE_HOURS =  2   # only forward messages younger than this
POLL_STARTUP_DELAY = 90   # seconds to wait before FIRST poll after bot restart
                          # This lets the event handler drain Telegram's pending
                          # update queue and write is_processed records to DB,
                          # so the first poll doesn't re-send already-forwarded posts.

# In-memory guard: (chat_id, msg_id) pairs currently being processed
# by either the event handler OR the polling loop.
# Prevents the race condition where both channels pick up the same message
# simultaneously (before either writes is_processed to DB) and both forward it.
_processing_msgs: set = set()

# Groq API rate limit: max 3 parallel requests at once
_groq_semaphore = asyncio.Semaphore(3)


async def _polling_loop(client, account_num: int) -> None:
    """
    Backup polling loop — barcha source guruhlarni PARALLEL tekshiradi.

    Har POLL_INTERVAL soniyada, barcha source guruhlar uchun
    asyncio.gather() bilan bir vaqtda get_messages() chaqiriladi.
    Bu 50 ta guruh uchun ketma-ket ~15s o'rniga ~1-2s oladi.

    Har bir xabar uchun:
      1. Yosh tekshiruvi (POLL_MAX_AGE_HOURS dan eski = o'tkazib yuboriladi)
      2. is_processed tekshiruvi (event handler allaqachon ko'rgan = o'tkazib)
      3. Keyword filter
      4. Halal filter
      5. Dedup
      6. Forward
    """
    logger.info(
        "[polling/acct%s] Backup polling loop started "
        "(startup_delay=%ds, interval=%ds, depth=%d msgs/group, parallel)",
        account_num, POLL_STARTUP_DELAY, POLL_INTERVAL, POLL_MESSAGES,
    )

    # Wait on startup so the event handler can drain Telegram's pending
    # update queue and mark messages as processed before first poll.
    # Without this, the polling loop re-sends everything from the past
    # POLL_MAX_AGE_HOURS on every bot restart.
    await asyncio.sleep(POLL_STARTUP_DELAY)

    while True:
        await asyncio.sleep(POLL_INTERVAL)

        groups = db.list_source_groups(account=account_num)
        if not groups:
            continue

        target = db.get_cached_target_id()
        if not target:
            continue

        # ── Barcha guruhlardan PARALLEL get_messages() ────────────────────
        # Avval: 50 guruh × ~300ms = ~15s ketma-ket
        # Endi:  asyncio.gather() = ~300ms (parallel)
        async def _fetch_group(group):
            try:
                msgs = await client.get_messages(group.chat_id, limit=POLL_MESSAGES)
                return group, msgs
            except Exception as exc:
                logger.debug(
                    "[polling/acct%s] get_messages failed for %s: %s",
                    account_num, group.title, exc,
                )
                return group, []

        results = await asyncio.gather(*[_fetch_group(g) for g in groups])

        # ── Har bir xabarni qayta ishlash ──────────────────────────────
        checked   = 0
        forwarded = 0

        for group, messages in results:
            for message in messages:
                checked += 1
                msg_id  = message.id
                chat_id = group.chat_id

                # 1. Yosh tekshiruvi — eski xabarlarni o'tkazib yuborish
                msg_time = message.date
                if msg_time:
                    if msg_time.tzinfo is None:
                        msg_time = msg_time.replace(tzinfo=timezone.utc)
                    age_hours = (utcnow() - msg_time).total_seconds() / 3600
                    if age_hours > POLL_MAX_AGE_HOURS:
                        continue

                # 2. is_processed — event handler allaqachon ko'rganmi?
                # Bu eng muhim check: ikki marta forward qilinishini oldini oladi
                if db.is_processed(chat_id, msg_id):
                    continue

                # 2a. In-memory guard — event handler hozir bu xabarni
                # qayta ishlayaptimi? (DB ga hali yozilmagan bo'lishi mumkin)
                _key = (chat_id, msg_id)
                if _key in _processing_msgs:
                    continue
                _processing_msgs.add(_key)

                # 3. Matn ajratish
                text = (
                    getattr(message, "text", None)
                    or getattr(message, "caption", None)
                    or getattr(message, "raw_text", None)
                    or ""
                )
                if not text.strip():
                    continue

                # 4. Keyword filter
                result = is_job_message(text)
                if not result.is_job:
                    continue

                # 5. Halal keyword filter
                haram_kw = is_haram_job(text)
                if haram_kw.is_haram:
                    db.mark_processed(chat_id, msg_id)
                    continue

                # 6. Kontent dedup
                if DEDUP_WINDOW_HOURS > 0:
                    is_dup, _ = db.is_content_duplicate(
                        text,
                        window_hours=DEDUP_WINDOW_HOURS,
                        similarity_threshold=DEDUP_SIMILARITY_THRESHOLD,
                    )
                    if is_dup:
                        db.mark_processed(chat_id, msg_id)
                        continue

                # 7. Post yasash va yuborish
                if msg_time and msg_time.tzinfo is None:
                    msg_time = msg_time.replace(tzinfo=timezone.utc)

                group_link = (
                    f"https://t.me/{group.username}" if group.username else None
                )

                sender = None
                try:
                    sender = await message.get_sender()
                except Exception:
                    pass

                post_text = build_job_post(
                    group_title=group.title,
                    group_link=group_link,
                    author_name=get_sender_display_name(sender),
                    author_link=(
                        get_user_link(sender) if isinstance(sender, User) else None
                    ),
                    message_time=msg_time or utcnow(),
                    message_text=truncate(text),
                    matched_keywords=result.matched_keywords,
                )

                sent_msg = await safe_send_message(client, target, post_text)
                if sent_msg:
                    db.mark_processed(chat_id, msg_id)
                    if DEDUP_WINDOW_HOURS > 0:
                        db.record_content_hash(
                            text, source_chat=chat_id, source_msg=msg_id
                        )
                    try:
                        db.save_forwarded_msg(
                            source_chat=chat_id,
                            source_msg=msg_id,
                            target_chat=target,
                            target_msg_id=sent_msg.id,
                            post_text=post_text,
                            sender_id=getattr(message, "sender_id", None),
                        )
                    except Exception:
                        pass
                    forwarded += 1
                    _poll_lag = 0.0
                    if msg_time:
                        try:
                            _mt = msg_time if msg_time.tzinfo else msg_time.replace(tzinfo=timezone.utc)
                            _poll_lag = (utcnow() - _mt).total_seconds()
                        except Exception:
                            pass
                    logger.info(
                        "[polling/acct%s] ✅ CAUGHT BY POLL | "
                        "chat=%s | msg=%s | lag=%.0fs",
                        account_num, chat_id, msg_id, _poll_lag,
                    )
                _processing_msgs.discard(_key)

        if forwarded:
            logger.info(
                "[polling/acct%s] cycle done | checked=%d | caught=%d",
                account_num, checked, forwarded,
            )


def _get_target_id() -> Optional[int]:
    cfg = db.get_target_group()
    if cfg:
        return cfg["chat_id"]
    if TARGET_GROUP and str(TARGET_GROUP).lstrip("-").isdigit():
        return int(TARGET_GROUP)
    return None


# ── Handler factory ───────────────────────────────────────────────

def _make_message_handler(assigned_client, account_num: int):
    """
    Return a NewMessage event handler for one Telethon client.

    Architecture (two-layer):
    ┌─ _on_new_message ──────────────────────────────────────────┐
    │  Thin wrapper registered with Telethon.                    │
    │  Creates an independent asyncio task and returns           │
    │  IMMEDIATELY to Telethon's update loop.                    │
    │  Zero blocking time — Telethon never waits.                │
    └────────────────────────────────────────────────────────────┘
    ┌─ _process_message ──────────────────────────────────────────┐
    │  Full 9-gate pipeline, runs concurrently as a Task.        │
    │  Multiple messages processed in parallel — no queuing.     │
    └────────────────────────────────────────────────────────────┘

    Additionally, setup_monitor() registers handlers with
    events.NewMessage(chats=source_ids) so Telethon pre-filters
    at the protocol level — personal groups/channels never even
    reach this handler.
    """
    async def _process_message(event: events.NewMessage.Event) -> None:
        """Full pipeline — runs as an independent asyncio.Task."""
        from telethon.tl.types import MessageService

        message = event.message
        chat_id = event.chat_id
        msg_id  = message.id

        # ── 0. Skip non-text service messages (join/leave events) ─
        # MessageService objects have no .text/.caption — they crash Gate 2.
        if isinstance(message, MessageService):
            return

        logger.debug(
            "[monitor/acct%s] MSG RECEIVED | chat_id=%-20s | msg_id=%-8s | preview=%r",
            account_num, chat_id, msg_id,
            (getattr(message, "text", None) or "")[:60],
        )

        # ── 1. Source-group gate ───────────────────────────────────
        # Safety net — Telethon's chats= filter already pre-filters.
        # This protects against edge cases (cache stale, etc.).
        if chat_id not in db.get_source_group_ids_cached(account=account_num):
            return

        # Measure how long Telegram took to deliver this update.
        # delivery_lag > 5s means Telegram's push was delayed, not our code.
        _lag = 0.0
        if message.date:
            try:
                _md = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
                _lag = (utcnow() - _md).total_seconds()
            except Exception:
                pass
        logger.info(
            "[monitor/acct%s] SOURCE GROUP HIT | chat_id=%s | msg_id=%s | delivery_lag=%.1fs",
            account_num, chat_id, msg_id, _lag,
        )

        # ── 2. Extract text (plain text or media caption) ─────────
        text = getattr(message, "text", None) or getattr(message, "caption", None) or getattr(message, "raw_text", None) or ""
        if not text.strip():
            logger.info(
                "[monitor/acct%s] SKIPPED (no text/caption) | chat_id=%s | msg_id=%s",
                account_num, chat_id, msg_id,
            )
            return

        # ── 2.5 Blocked-user gate ─────────────────────────────────
        # Uses in-memory cache — O(1) frozenset lookup, no DB I/O.
        sender_id = message.sender_id
        if sender_id and db.is_blocked_cached(sender_id):
            logger.info(
                "[monitor/acct%s] SKIPPED (blocked user) | user_id=%s | chat_id=%s | msg_id=%s",
                account_num, sender_id, chat_id, msg_id,
            )
            return

        # ── 3. Repost / forward duplicate check ───────────────────
        _pending_original: Optional[tuple] = None
        fwd = getattr(message, "fwd_from", None)
        if fwd is not None:
            orig_chat = getattr(fwd, "from_id", None)
            # channel_post      — set when forwarded from a channel
            # saved_from_msg_id — set when forwarded from Saved Messages
            # message_id        — set when forwarded from a group (was missing before)
            orig_msg  = (
                getattr(fwd, "channel_post", None)
                or getattr(fwd, "saved_from_msg_id", None)
                or getattr(fwd, "message_id", None)
            )
            orig_chat_id = None
            if orig_chat is not None:
                orig_chat_id = (
                    getattr(orig_chat, "channel_id", None)
                    or getattr(orig_chat, "user_id",    None)
                    or getattr(orig_chat, "chat_id",    None)
                )
                if orig_chat_id:
                    orig_chat_id = (
                        int(f"-100{orig_chat_id}")
                        if orig_chat_id > 0 else orig_chat_id
                    )
            if orig_chat_id and orig_msg:
                if db.is_repost(orig_chat_id, orig_msg):
                    logger.info(
                        "[monitor/acct%s] SKIPPED (repost duplicate) | "
                        "orig_chat=%s | orig_msg=%s",
                        account_num, orig_chat_id, orig_msg,
                    )
                    return
                _pending_original = (orig_chat_id, orig_msg)

        # ── 4. Processed-message dedup ────────────────────────────
        if db.is_processed(chat_id, msg_id):
            logger.info(
                "[monitor/acct%s] SKIPPED (already processed) | chat_id=%s | msg_id=%s",
                account_num, chat_id, msg_id,
            )
            return

        # In-memory guard — polling loop hozir bu xabarni qayta ishlayaptimi?
        _pm_key = (chat_id, msg_id)
        if _pm_key in _processing_msgs:
            logger.debug(
                "[monitor/acct%s] SKIPPED (in-flight by polling) | chat_id=%s | msg_id=%s",
                account_num, chat_id, msg_id,
            )
            return
        _processing_msgs.add(_pm_key)

        # ── 5. Keyword filter ─────────────────────────────────────
        result = is_job_message(text)
        if not result.is_job:
            logger.info(
                "[monitor/acct%s] SKIPPED (no keywords) | chat_id=%s | msg_id=%s | preview=%r",
                account_num, chat_id, msg_id, text[:80],
            )
            return

        # ── 5.1 Pre-resolve group metadata (O(1), no API call) ──────────
        # Guruh nomi va vaqt DB dan bepul olinadi. Sender resolve va Groq
        # tekshiruvi kerak bo'lgandagina va parallel amalga oshiriladi.
        _stored_group    = db.get_source_group_by_id(chat_id)
        _pre_group_title = _stored_group.title if _stored_group else "Unknown Group"
        _pre_group_link  = (
            f"https://t.me/{_stored_group.username}"
            if _stored_group and _stored_group.username else ""
        )
        _pre_msg_time = message.date
        if _pre_msg_time and _pre_msg_time.tzinfo is None:
            _pre_msg_time = _pre_msg_time.replace(tzinfo=timezone.utc)
        _pre_msg_time_str = _pre_msg_time.isoformat() if _pre_msg_time else ""

        # ── 5.2 Keyword-based halal pre-filter ───────────────────────────
        # Bu gate API chaqiruvisiz ishlaydi — eng tez bloklash yo'li.
        # Harom topilsa: sender resolve qilinadi (review uchun), keyin qaytiladi.
        haram_kw = is_haram_job(text)
        if haram_kw.is_haram:
            _kw_sender = None
            try:
                _kw_sender = await event.get_sender()
            except Exception:
                pass
            logger.info(
                "[monitor/acct%s] HARAM (keyword) | category=%s | "
                "keywords=%s | chat_id=%s | msg_id=%s",
                account_num, haram_kw.category,
                haram_kw.matched_keywords, chat_id, msg_id,
            )
            sent = await _send_to_review(
                text=text,
                source_chat=chat_id,
                source_msg=msg_id,
                haram_reason=f"Kalit so'z: {', '.join(haram_kw.matched_keywords)}",
                haram_source="keyword",
                group_title=_pre_group_title,
                group_link=_pre_group_link,
                author_name=get_sender_display_name(_kw_sender),
                author_link=(
                    get_user_link(_kw_sender)
                    if isinstance(_kw_sender, User) else ""
                ) or "",
                msg_time=_pre_msg_time_str,
            )
            if sent:
                db.mark_processed(chat_id, msg_id)
            return

        # ── 5.3 + 5.5  Sender resolve  &  content dedup — PARALLEL ─────────
        #
        # Ilgari: get_sender() (~200ms) va dedup (~50ms) ketma-ket = ~250ms.
        # Endi:   asyncio.gather() bilan ikkalasi parallel = ~200ms (max).
        #
        # Exact-hash check (Tier 1, O(1)) avval sinxron tekshiriladi —
        # duplicate bo'lsa API chaqiruvlarga ketmasdan darhol qaytiladi.
        content_hash: Optional[str] = None
        _pre_sender: Optional[object] = None

        # Tier 1 exact-hash sinxron — O(1), DB I/O yo'q (in-memory)
        if DEDUP_WINDOW_HOURS > 0:
            content_hash = db._content_hash(text)
            if content_hash in _pending_hashes:
                logger.info(
                    "[monitor/acct%s] SKIPPED (in-flight duplicate) | "
                    "chat_id=%s | msg_id=%s",
                    account_num, chat_id, msg_id,
                )
                return
            _pending_hashes.add(content_hash)

        # get_sender() va fuzzy dedup birga parallel
        async def _do_get_sender():
            try:
                return await event.get_sender()
            except Exception:
                return None

        async def _do_dedup():
            if DEDUP_WINDOW_HOURS <= 0 or content_hash is None:
                return False, ""
            return db.is_content_duplicate(
                text,
                window_hours=DEDUP_WINDOW_HOURS,
                similarity_threshold=DEDUP_SIMILARITY_THRESHOLD,
            )

        _pre_sender, (is_dup, dup_reason) = await asyncio.gather(
            _do_get_sender(), _do_dedup()
        )

        if is_dup:
            if content_hash:
                _pending_hashes.discard(content_hash)
            logger.info(
                "[monitor/acct%s] SKIPPED (content duplicate) | %s | "
                "chat_id=%s | msg_id=%s",
                account_num, dup_reason, chat_id, msg_id,
            )
            return

        # Confirmed job post — safe to register origin
        if _pending_original:
            db.mark_original(*_pending_original)

        logger.info(
            "[monitor/acct%s] ✅ JOB DETECTED | chat=%s | msg=%s | lang=%s | kw=%s",
            account_num, chat_id, msg_id,
            result.matched_lang, result.matched_keywords,
        )

        # ── 6. Resolve entities ───────────────────────────────────
        # Fix 2: _pre_sender allaqachon Gate 5.1 da olingan — qayta get_sender()
        # chaqirmaslik kerak. Bu har halol xabar uchun 100-500ms tejaydi.
        # group_title ham DB dan allaqachon olingan (_pre_group_title).
        group_title = _pre_group_title
        group_link  = _pre_group_link or None

        if not group_title or group_title == "Unknown Group":
            # DB da topilmasa — Telegram API dan olish (kam uchraydi)
            try:
                chat        = await event.get_chat()
                group_title = get_chat_display_name(chat)
                group_link  = get_chat_link(chat)
            except Exception as exc:
                logger.warning(
                    "[monitor/acct%s] Could not resolve chat: %s", account_num, exc
                )

        group_title = group_title or "Unknown Group"
        author_name = get_sender_display_name(_pre_sender)
        author_link: Optional[str] = (
            get_user_link(_pre_sender) if isinstance(_pre_sender, User) else None
        )

        # ── 7. Timestamp ──────────────────────────────────────────
        msg_time = message.date
        if msg_time and msg_time.tzinfo is None:
            msg_time = msg_time.replace(tzinfo=timezone.utc)
        if not msg_time:
            msg_time = utcnow()

        # ── 8. Resolve target ─────────────────────────────────────
        # Uses in-memory cache — no DB I/O on the hot path.
        target = db.get_cached_target_id()
        if not target:
            await notify_admin(
                "❌ <b>Monitor error:</b> No target group configured.\n"
                "Use /settarget in the admin bot."
            )
            return

        # ── 9. Build and send ─────────────────────────────────────
        post_text = build_job_post(
            group_title=group_title,
            group_link=group_link,
            author_name=author_name,
            author_link=author_link,
            message_time=msg_time,
            message_text=truncate(text),
            matched_keywords=result.matched_keywords,
        )

        try:
            sent_msg = await safe_send_message(assigned_client, target, post_text)

            if sent_msg:
                db.mark_processed(chat_id, msg_id)
                # Register content fingerprint AFTER successful send so that
                # a failed send never permanently blacklists content.
                if DEDUP_WINDOW_HOURS > 0:
                    db.record_content_hash(text, source_chat=chat_id, source_msg=msg_id)
                # "Odam olindi" feature: target_msg_id ni DB ga saqlaymiz.
                # safe_send_message now returns the sent Message object directly,
                # so we read sent_msg.id here — no extra get_messages() API call
                # needed. The old approach (get_messages after send) was both slow
                # (~300 ms per post) and racey: if another message landed in the
                # target group between send and fetch, the wrong ID was stored,
                # silently breaking the "ODAM OLINDI" edit feature.
                try:
                    db.save_forwarded_msg(
                        source_chat=chat_id,
                        source_msg=msg_id,
                        target_chat=target,
                        target_msg_id=sent_msg.id,
                        post_text=post_text,
                        sender_id=message.sender_id,
                    )
                except Exception as _exc:
                    logger.warning("[monitor] Could not save forwarded_msg id: %s", _exc)
                logger.info(
                    "[monitor/acct%s] ✅ FORWARDED | chat=%s | msg=%s → target=%s | target_msg_id=%s",
                    account_num, chat_id, msg_id, target, sent_msg.id,
                )
                db.record_stat(
                    source_chat_id=chat_id,
                    source_title=group_title,
                    matched_lang=result.matched_lang,
                    matched_kw=", ".join(result.matched_keywords),
                    match_tier=result.match_tier,
                )
                # ── Groq background check ───────────────────────────────────────
                # Post allaqachon guruhga yuborildi. Groq fon rejimida
                # tekshiradi — haram bo'lsa postni o'chirib adminga xabar beradi.
                if db.is_ai_filter_enabled() and bool(GROQ_API_KEY):
                    asyncio.create_task(
                        _background_groq_check(
                            client=assigned_client,
                            target=target,
                            target_msg_id=sent_msg.id,
                            source_chat=chat_id,
                            source_msg=msg_id,
                            text=text,
                            group_title=group_title,
                            group_link=group_link or "",
                            author_name=author_name,
                            author_link=author_link or "",
                            msg_time=_pre_msg_time_str,
                            account_num=account_num,
                        )
                    )
            else:
                logger.error(
                    "[monitor/acct%s] ❌ SEND FAILED | chat=%s | msg=%s | target=%s",
                    account_num, chat_id, msg_id, target,
                )
                await notify_admin(
                    f"❌ <b>Send failed</b> (account {account_num}) after 3 retries.\n\n"
                    f"Source: {group_title} (<code>{chat_id}</code>)\n"
                    f"Message ID: <code>{msg_id}</code>\n"
                    f"Target: <code>{target}</code>"
                )
        finally:
            # Always release the pending-hash lock regardless of outcome.
            # On success: hash is now in the DB so future duplicates are caught there.
            # On failure: release so a manual retry or the next occurrence can go through.
            if content_hash:
                _pending_hashes.discard(content_hash)
            # Release in-memory processing guard
            _processing_msgs.discard(_pm_key)

    async def _on_new_message(event: events.NewMessage.Event) -> None:
        """Thin wrapper — spawns task, returns to Telethon immediately."""
        asyncio.create_task(_process_message(event))

    return _on_new_message


# ── Background Groq halal check ───────────────────────────────────────────────
# Post allaqachon target guruhga yuborilgan. Bu funksiya fon rejimida ishlaydi.
# Haram yoki noaniq: (1) postni o'chiradi, (2) adminga review yuboradi.
# Halol: hech narsa qilmaydi.

# ── Group info sync loop ───────────────────────────────────────────────────────
# Har 4 soatda source guruhlar ma'lumotlarini tekshiradi:
#   - Guruh nomi (title) o'zgardimi?
#   - Username o'zgardimi yoki guruh private bo'lib qoldimi?
#   - Private guruh bo'lsa description dan invite link qidiradi
# O'zgarish bo'lsa DB yangilanadi va adminga xabar ketadi.

GROUP_SYNC_INTERVAL = 4 * 3600   # 4 soat (soniyada)
_INVITE_LINK_RE = re.compile(r'https://t\.me/\+[A-Za-z0-9_-]+')


async def _group_sync_loop(client, account_num: int) -> None:
    """
    Background task: har GROUP_SYNC_INTERVAL soniyada source guruhlar
    ma'lumotlarini Telegram API dan oladi va DB ni yangilaydi.
    """
    # Botni ishga tushirishdan keyin 2 daqiqa kutamiz
    await asyncio.sleep(120)
    logger.info(
        "[group_sync/acct%s] Group sync loop started (interval=%dh)",
        account_num, GROUP_SYNC_INTERVAL // 3600,
    )

    while True:
        groups = db.list_source_groups(account=account_num)

        for group in groups:
            try:
                entity = await client.get_entity(group.chat_id)
            except Exception as exc:
                logger.debug(
                    "[group_sync/acct%s] Cannot fetch chat_id=%s: %s",
                    account_num, group.chat_id, exc,
                )
                continue

            new_title    = getattr(entity, "title", None) or group.title
            new_username = getattr(entity, "username", None)  # None = private

            # Private guruh — description dan invite link qidirish
            invite_link: Optional[str] = None
            if not new_username:
                about = getattr(entity, "about", None) or ""
                m = _INVITE_LINK_RE.search(about)
                if m:
                    invite_link = m.group(0)

            title_changed    = new_title    != group.title
            username_changed = new_username != group.username

            if not title_changed and not username_changed:
                continue   # Hech narsa o'zgarmagan — o'tkazib yuborish

            # DB yangilash
            db.update_source_group_title(
                chat_id=group.chat_id,
                title=new_title,
                username=new_username or invite_link,
            )

            # Admin uchun xabar yaratish
            parts = [
                f"🔄 <b>Source group updated</b> (Account {account_num})",
                f"ID: <code>{group.chat_id}</code>",
            ]
            if title_changed:
                parts.append(
                    f"Nomi: <s>{group.title}</s> → <b>{new_title}</b>"
                )
            if username_changed:
                old_link = f"@{group.username}" if group.username else "private"
                if new_username:
                    new_link = f"@{new_username}"
                elif invite_link:
                    new_link = f"private (invite: {invite_link})"
                else:
                    new_link = "private (invite link topilmadi)"
                parts.append(f"Link: {old_link} → {new_link}")

            await notify_admin("\n".join(parts))
            logger.info(
                "[group_sync/acct%s] Updated chat_id=%s | title=%r | username=%r",
                account_num, group.chat_id, new_title, new_username,
            )

            # Har guruh orasida biroz kutish (API rate limit uchun)
            await asyncio.sleep(1)

        await asyncio.sleep(GROUP_SYNC_INTERVAL)


async def _background_groq_check(
    client,
    target: int,
    target_msg_id: int,
    source_chat: int,
    source_msg: int,
    text: str,
    group_title: str,
    group_link: str,
    author_name: str,
    author_link: str,
    msg_time: str,
    account_num: int,
) -> None:
    """Fon rejimida Groq tekshiruvi. Haram bo'lsa postni o'chirib adminga yuboradi."""
    try:
        async with _groq_semaphore:
            groq_result = await check_halal_with_groq(text)

        if groq_result.api_error:
            logger.debug(
                "[monitor/acct%s] bg-Groq: API error — post kept | target_msg=%s",
                account_num, target_msg_id,
            )
            return

        if groq_result.verdict not in ("haram", "unclear"):
            logger.debug(
                "[monitor/acct%s] bg-Groq: halol | target_msg=%s",
                account_num, target_msg_id,
            )
            return

        verdict_label = "harom" if groq_result.verdict == "haram" else "noaniq"
        logger.info(
            "[monitor/acct%s] bg-Groq: %s | reason=%s | "
            "source=%s/%s → deleting target_msg=%s",
            account_num, groq_result.verdict, groq_result.reason,
            source_chat, source_msg, target_msg_id,
        )

        # Target guruhdan o'chirish
        try:
            await client.delete_messages(target, [target_msg_id])
            logger.info(
                "[monitor/acct%s] bg-Groq: deleted target_msg=%s",
                account_num, target_msg_id,
            )
        except Exception as del_exc:
            logger.warning(
                "[monitor/acct%s] bg-Groq: delete failed target_msg=%s: %s",
                account_num, target_msg_id, del_exc,
            )

        # Adminga review queue
        await _send_to_review(
            text=text,
            source_chat=source_chat,
            source_msg=source_msg,
            haram_reason=groq_result.reason or f"AI natijasi: {verdict_label}",
            haram_source=f"groq_{groq_result.verdict}",
            group_title=group_title,
            group_link=group_link,
            author_name=author_name,
            author_link=author_link,
            msg_time=msg_time,
        )

    except Exception as exc:
        logger.error(
            "[monitor/acct%s] bg-Groq check crashed: %s",
            account_num, exc,
        )


# ── Handler 2: auto-delete join/leave service messages ────────────
# Only needs to run on client_1 — whichever account is in the target group.

async def _on_chat_action(event: events.ChatAction.Event) -> None:
    target_id = _get_target_id()
    if not target_id or event.chat_id != target_id:
        return

    action = event.action_message
    if action is None:
        return

    action_type = type(action.action)
    is_join_or_leave = action_type in (
        MessageActionChatAddUser,
        MessageActionChatJoinedByLink,
        MessageActionChatJoinedByRequest,
        MessageActionChatDeleteUser,
    )
    if not is_join_or_leave:
        return

    try:
        user = await event.get_user()
        display = (
            f"{user.first_name or ''} {user.last_name or ''}".strip()
            if user else "unknown"
        )
    except Exception:
        display = "unknown"

    logger.info(
        "[monitor] 🗑️  Deleting service message | user=%s | msg_id=%s",
        display, action.id,
    )
    try:
        await client_1.delete_messages(target_id, [action.id])
    except Exception as exc:
        logger.warning(
            "[monitor] Could not delete service message %s: %s",
            action.id, exc,
        )


# ── Handler 3: "Odam olindi" — reply yoki edit orqali ────────────
#
# Ikkita holat kuzatiladi:
#   A) Manba guruhda ish e'loniga kimdir reply qilib "odam olindi" desa
#   B) Ish beruvchi o'z xabarini edit qilib matniga "odam olindi" qo'shsa
#
# Ikkalasida ham target guruhda yuborilgan forward postga
# "🔴 ODAM OLINDI" qo'shimcha qo'yiladi (edit orqali).

async def _mark_filled_in_target(
    assigned_client,
    source_chat: int,
    source_msg: int,
) -> None:
    """
    DB dan target_msg_id ni topib, target guruhda o'sha postni edit qiladi.
    Telegram 48 soatdan eski xabarlarni edit qilmaydi — bu holda log yozadi.
    """
    record = db.get_forwarded_msg(source_chat, source_msg)
    if not record:
        logger.debug(
            "[monitor] filled signal — source msg not in forwarded_msgs "
            "source_chat=%s source_msg=%s", source_chat, source_msg
        )
        return

    target_chat   = record["target_chat"]
    target_msg_id = record["target_msg_id"]
    old_text      = record["post_text"] or ""

    # Eski matnning oxiriga "ODAM OLINDI" separator qo'shamiz
    separator = "\n\n─────────────────\n🔴 <b>ODAM OLINDI</b>"
    if "ODAM OLINDI" in old_text:
        logger.debug("[monitor] Already marked filled: target_msg_id=%s", target_msg_id)
        return

    new_text = old_text + separator

    try:
        await assigned_client.edit_message(
            entity=target_chat,
            message=target_msg_id,
            text=new_text,
            parse_mode="html",
            link_preview=False,
        )
        logger.info(
            "[monitor] 🔴 ODAM OLINDI marked | source_chat=%s source_msg=%s → target_msg=%s",
            source_chat, source_msg, target_msg_id,
        )
    except Exception as exc:
        err_str = str(exc)
        if "MESSAGE_EDIT_TIME_EXPIRED" in err_str or "edit" in err_str.lower():
            logger.info(
                "[monitor] Cannot edit — message too old (>48h) | target_msg_id=%s",
                target_msg_id,
            )
        else:
            logger.warning(
                "[monitor] Failed to edit filled message %s: %s",
                target_msg_id, exc,
            )


def _make_filled_handlers(assigned_client, account_num: int):
    """
    Ikkita event handler qaytaradi:
      1. on_reply  — ish e'loniga "odam olindi" deb reply qilinsa
      2. on_edited — ish e'loni tahrirlansa va "odam olindi" qo'shilsa
    """
    async def _on_reply(event: events.NewMessage.Event) -> None:
        """Kimdir source guruhda ish e'loniga reply qildi."""
        message = event.message
        chat_id = event.chat_id

        # Faqat kuzatilayotgan manba guruhlardan
        if chat_id not in db.get_source_group_ids_cached(account=account_num):
            return

        # Faqat reply bo'lsa
        if not message.reply_to_msg_id:
            return

        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if not _is_filled_signal(text):
            return

        logger.info(
            "[monitor/acct%s] FILLED signal (reply) | chat=%s replied_to=%s",
            account_num, chat_id, message.reply_to_msg_id,
        )
        await _mark_filled_in_target(
            assigned_client=assigned_client,
            source_chat=chat_id,
            source_msg=message.reply_to_msg_id,
        )

    async def _on_edited(event: events.MessageEdited.Event) -> None:
        """Ish beruvchi o'z xabarini edit qildi."""
        message = event.message
        chat_id = event.chat_id

        if chat_id not in db.get_source_group_ids_cached(account=account_num):
            return

        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if not _is_filled_signal(text):
            return

        logger.info(
            "[monitor/acct%s] FILLED signal (edit) | chat=%s msg=%s",
            account_num, chat_id, message.id,
        )
        await _mark_filled_in_target(
            assigned_client=assigned_client,
            source_chat=chat_id,
            source_msg=message.id,
        )

    async def _on_no_reply(event: events.NewMessage.Event) -> None:
        """
        Reply bo'lmagan holat: ish beruvchining o'zi guruhda shunchaki
        'odam olindi' deb yozadi. Sender ID orqali uning guruhda
        eng so'nggi forward qilingan postini topib edit qilamiz.
        """
        message = event.message
        chat_id = event.chat_id

        # Faqat kuzatilayotgan manba guruhlardan
        if chat_id not in db.get_source_group_ids_cached(account=account_num):
            return

        # Reply bo'lmagan holat — reply bo'lsa _on_reply handler oladi
        if message.reply_to_msg_id:
            return

        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if not _is_filled_signal(text):
            return

        sender_id = message.sender_id
        if not sender_id:
            return

        # O'sha sender_id dan shu guruhda eng oxirgi forward qilingan post
        record = db.get_last_forwarded_by_sender(
            source_chat=chat_id,
            sender_id=sender_id,
        )
        if not record:
            logger.debug(
                "[monitor/acct%s] FILLED no-reply — no recent post found for sender=%s chat=%s",
                account_num, sender_id, chat_id,
            )
            return

        logger.info(
            "[monitor/acct%s] FILLED signal (no-reply) | sender=%s → last post source_msg=%s",
            account_num, sender_id, record["source_msg"],
        )
        await _mark_filled_in_target(
            assigned_client=assigned_client,
            source_chat=chat_id,
            source_msg=record["source_msg"],
        )

    return _on_reply, _on_edited, _on_no_reply


# ── Public API ────────────────────────────────────────────────────

async def setup_monitor() -> None:
    """Start all clients and register event handlers."""
    # Fix 7: Startupda _pending_hashes tozalanadi — avvalgi crash dan qolgan
    # yozuvlar yangi sessiyada xabarlarni noto'g'ri bloklashini oldini oladi.
    _pending_hashes.clear()
    phone_numbers = {1: PHONE_NUMBER, 2: PHONE_NUMBER_2 or PHONE_NUMBER}

    for account_num, client in enumerate(all_clients, start=1):
        logger.info("[monitor] Connecting account %s …", account_num)
        await client.start(phone=phone_numbers[account_num])
        me = await client.get_me()
        logger.info(
            "[monitor] Account %s logged in as: %s (id=%s)",
            account_num, me.first_name, me.id,
        )

        # ── Register handlers with source-group chat filter ─────────
        # THE KEY FIX for 12-minute delays:
        #
        # events.NewMessage() with NO filter = Telethon delivers EVERY message
        # from EVERY group/channel/chat the account is in (500+ personal groups).
        # Each message goes through the handler even if it's from a personal chat.
        # When hundreds of irrelevant messages queue up, source-group messages
        # wait behind them — causing 10-12 minute delays.
        #
        # events.NewMessage(chats=source_ids) = Telegram filters at PROTOCOL level.
        # Only messages from the 40 source groups ever reach the handler.
        # Personal groups, channels, bots — completely invisible to the handler.
        # Result: zero queue, instant detection.
        groups = db.list_source_groups(account=account_num)
        source_ids = [g.chat_id for g in groups]

        handler = _make_message_handler(client, account_num)
        client.add_event_handler(handler, events.NewMessage(chats=source_ids))

        # "Odam olindi" handlers — same chats= filter
        on_reply, on_edited, on_no_reply = _make_filled_handlers(client, account_num)
        client.add_event_handler(on_reply,    events.NewMessage(chats=source_ids))
        client.add_event_handler(on_no_reply, events.NewMessage(chats=source_ids))
        client.add_event_handler(on_edited,   events.MessageEdited(chats=source_ids))

        logger.info(
            "[monitor] Account %s: chats= filter active for %d source groups",
            account_num, len(source_ids),
        )

        # Start backup polling loop — catches messages missed by event system
        asyncio.create_task(
            _polling_loop(client, account_num),
            name=f"polling_acct{account_num}",
        )
        asyncio.create_task(
            _group_sync_loop(client, account_num),
            name=f"group_sync_acct{account_num}",
        )
        logger.info(
            "[monitor] Account %s: backup polling started (every %ds)",
            account_num, POLL_INTERVAL,
        )

        # ── Startup validation: catch bad IDs before they silently fail ──
        # Any group with a positive chat_id is missing the -100 supergroup
        # prefix. Telegram delivers events as -100XXXXXXXXX, so a positive
        # stored ID never matches and monitoring silently does nothing.
        bad_ids = [g for g in groups if g.chat_id > 0]
        if bad_ids:
            logger.error(
                "[monitor] ⚠️  Account %s has %d group(s) with INVALID positive "
                "chat_id — these will never match incoming events! "
                "Run fix_chat_ids.py to repair them. Affected: %s",
                account_num,
                len(bad_ids),
                [(g.chat_id, g.title) for g in bad_ids],
            )

    # Chat-action handler only on client_1
    client_1.add_event_handler(_on_chat_action, events.ChatAction())

    target = db.get_target_group()
    if target:
        logger.info("[monitor] Target: %s (%s)", target["title"], target["chat_id"])
    else:
        logger.warning("[monitor] ⚠️  No target group. Use /settarget.")

    logger.info("[monitor] 👂 Listening on %d account(s) …", len(all_clients))