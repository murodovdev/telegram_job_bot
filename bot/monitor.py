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
    "odam olindi", "olindi", "band bo'ldi", "band boldi", "to'ldi", "toldi",
    "tugadi", "yopildi", "ishchi topildi", "aktual emas", "kerak emas",
    "olingan", "band", "topildi", "yopilyapti", "bitdi",
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
        f"⚠️ <b>Harom deb topilgan ish e'loni</b>\n\n"
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

# Fix 5: Groq API rate limit himoyasi.
# Bir vaqtda maksimal 3 ta parallel Groq so'rov — free tier limit 30/min.
_groq_semaphore = asyncio.Semaphore(3)


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
    Return a NewMessage event handler bound to a specific client and account.

    The handler only processes messages from source groups that are assigned
    to account_num in the database — the two accounts never step on each other.
    """
    async def _on_new_message(event: events.NewMessage.Event) -> None:
        message: Message = event.message
        chat_id = event.chat_id
        msg_id  = message.id

        logger.debug(
            "[monitor/acct%s] MSG RECEIVED | chat_id=%-20s | msg_id=%-8s | preview=%r",
            account_num, chat_id, msg_id, (message.text or "")[:60],
        )

        # ── 1. Source-group gate (account-specific) ───────────────
        # Uses the in-memory cache — O(1) frozenset lookup, no DB I/O,
        # no event-loop blocking. Called on every incoming message so
        # this is the single most performance-critical line in the bot.
        if chat_id not in db.get_source_group_ids_cached(account=account_num):
            logger.debug(
                "[monitor/acct%s] IGNORED (not a source group) | chat_id=%s | msg_id=%s",
                account_num, chat_id, msg_id,
            )
            return

        logger.info(
            "[monitor/acct%s] SOURCE GROUP HIT | chat_id=%s | msg_id=%s",
            account_num, chat_id, msg_id,
        )

        # ── 2. Extract text (plain text or media caption) ─────────
        text = message.text or message.caption or message.raw_text or ""
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

        # ── 5. Keyword filter ─────────────────────────────────────
        result = is_job_message(text)
        if not result.is_job:
            logger.info(
                "[monitor/acct%s] SKIPPED (no keywords) | chat_id=%s | msg_id=%s | preview=%r",
                account_num, chat_id, msg_id, text[:80],
            )
            return

        # ── 5.1 Pre-resolve entities (lightweight) ────────────────
        # Guruh nomi DB dan bepul olinadi (O(1), API chaqiruvi yo'q).
        # Sender keyinchalik to'liq resolve qilinadi, bu yerda try/except
        # bilan oldindan olamiz — review queue uchun kerak.
        _stored_group = db.get_source_group_by_id(chat_id)
        _pre_group_title = _stored_group.title if _stored_group else "Unknown Group"
        _pre_group_link  = (
            f"https://t.me/{_stored_group.username}"
            if _stored_group and _stored_group.username else ""
        )
        _pre_sender = None
        try:
            _pre_sender = await event.get_sender()
        except Exception:
            pass
        _pre_author_name = get_sender_display_name(_pre_sender)
        _pre_author_link = (
            get_user_link(_pre_sender) if isinstance(_pre_sender, User) else ""
        ) or ""
        _pre_msg_time = message.date
        if _pre_msg_time and _pre_msg_time.tzinfo is None:
            _pre_msg_time = _pre_msg_time.replace(tzinfo=timezone.utc)
        _pre_msg_time_str = _pre_msg_time.isoformat() if _pre_msg_time else ""

        # ── 5.2 Keyword-based halal pre-filter ───────────────────
        haram_kw = is_haram_job(text)
        if haram_kw.is_haram:
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
                author_name=_pre_author_name,
                author_link=_pre_author_link,
                msg_time=_pre_msg_time_str,
            )
            if sent:
                db.mark_processed(chat_id, msg_id)
            return

        # ── 5.3 Groq AI halal check ───────────────────────────────
        # Semaphore bilan rate limit nazorat qilinadi — bir vaqtda max 3 ta so'rov.
        async with _groq_semaphore:
            groq_result = await check_halal_with_groq(text)
        if not groq_result.api_error and groq_result.verdict in ("haram", "unclear"):
            verdict_label = "harom" if groq_result.verdict == "haram" else "noaniq"
            logger.info(
                "[monitor/acct%s] GROQ=%s | reason=%s | chat_id=%s | msg_id=%s",
                account_num, groq_result.verdict, groq_result.reason, chat_id, msg_id,
            )
            # Ikkalasi ham adminga yuboriladi — admin qaror qiladi.
            # haram_source field uchun verdict ni saqlaymiz (groq_haram / groq_unclear)
            sent = await _send_to_review(
                text=text,
                source_chat=chat_id,
                source_msg=msg_id,
                haram_reason=groq_result.reason or f"AI natijasi: {verdict_label}",
                haram_source=f"groq_{groq_result.verdict}",
                group_title=_pre_group_title,
                group_link=_pre_group_link,
                author_name=_pre_author_name,
                author_link=_pre_author_link,
                msg_time=_pre_msg_time_str,
            )
            if sent:
                db.mark_processed(chat_id, msg_id)
            return

        # ── 5.5 Content duplicate gate ────────────────────────────
        # Catches copy-paste spam: the same (or nearly identical) job listing
        # posted manually in multiple groups — different chat/msg IDs, no
        # fwd_from header, so Gates 3 and 4 cannot catch it.
        #
        # Two-tier check (see database.py for full design rationale):
        #   Tier 1 — exact SHA-256 hash match (O(1))
        #   Tier 2 — SequenceMatcher similarity against recent texts (O(n))
        #
        # _pending_hashes guards against the race condition where two accounts
        # receive the same message within milliseconds — both would pass the
        # DB duplicate check before either records the hash.  The second
        # coroutine to arrive sees the hash in _pending_hashes and exits early.
        content_hash: Optional[str] = None
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

            is_dup, dup_reason = db.is_content_duplicate(
                text,
                window_hours=DEDUP_WINDOW_HOURS,
                similarity_threshold=DEDUP_SIMILARITY_THRESHOLD,
            )
            if is_dup:
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
        author_name = _pre_author_name
        author_link: Optional[str] = _pre_author_link or None

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
            success = await safe_send_message(assigned_client, target, post_text)

            if success:
                db.mark_processed(chat_id, msg_id)
                # Register content fingerprint AFTER successful send so that
                # a failed send never permanently blacklists content.
                if DEDUP_WINDOW_HOURS > 0:
                    db.record_content_hash(text, source_chat=chat_id, source_msg=msg_id)
                # "Odam olindi" feature: target_msg_id ni DB ga saqlaymiz.
                # Keyinroq reply/edit kelganda shu ID orqali postni topamiz.
                try:
                    sent_msgs = await assigned_client.get_messages(target, limit=1)
                    if sent_msgs:
                        db.save_forwarded_msg(
                            source_chat=chat_id,
                            source_msg=msg_id,
                            target_chat=target,
                            target_msg_id=sent_msgs[0].id,
                            post_text=post_text,
                        )
                except Exception as _exc:
                    logger.warning("[monitor] Could not save forwarded_msg id: %s", _exc)
                logger.info(
                    "[monitor/acct%s] ✅ FORWARDED | chat=%s | msg=%s → target=%s",
                    account_num, chat_id, msg_id, target,
                )
                db.record_stat(
                    source_chat_id=chat_id,
                    source_title=group_title,
                    matched_lang=result.matched_lang,
                    matched_kw=", ".join(result.matched_keywords),
                    match_tier=result.match_tier,
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

    return _on_new_message


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

        text = message.text or message.caption or ""
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

        text = message.text or message.caption or ""
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

    return _on_reply, _on_edited


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

        # Register the account-specific message handler
        handler = _make_message_handler(client, account_num)
        client.add_event_handler(handler, events.NewMessage())

        # "Odam olindi" handlerlarini ro'yxatdan o'tkazish
        on_reply, on_edited = _make_filled_handlers(client, account_num)
        client.add_event_handler(on_reply,  events.NewMessage())
        client.add_event_handler(on_edited, events.MessageEdited())

        groups = db.list_source_groups(account=account_num)
        logger.info(
            "[monitor] Account %s monitoring %d group(s)",
            account_num, len(groups),
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