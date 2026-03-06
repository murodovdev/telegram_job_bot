"""
monitor.py – Telethon userbot.

Changes in v4
-------------
• Feature 3 — Repost dedup: checks fwd_from on incoming messages.
  If the original (chat_id, msg_id) was already forwarded from another
  group, it is silently skipped.
• Feature 7 — Stats: calls db.record_stat() after every successful forward.
• Feature 10 — Error notifications: sends admin DM on send failures and
  Telethon disconnection via notifier.notify_admin().
• Auto-delete join/leave service messages in target group (from v3).
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
from bot.client import telethon_client as client
from bot.config import PHONE_NUMBER, TARGET_GROUP
from bot.filters import is_job_message
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


def _get_target_id() -> Optional[int]:
    cfg = db.get_target_group()
    if cfg:
        return cfg["chat_id"]
    if TARGET_GROUP and str(TARGET_GROUP).lstrip("-").isdigit():
        return int(TARGET_GROUP)
    return None


# ── Handler 1: job message forwarding ────────────────────────────

async def _on_new_message(event: events.NewMessage.Event) -> None:
    message: Message = event.message
    chat_id = event.chat_id
    msg_id  = message.id

    logger.debug(
        "[monitor] MSG RECEIVED | chat_id=%-20s | msg_id=%-8s | preview=%r",
        chat_id, msg_id, (message.text or "")[:60],
    )

    # ── 1. Source-group gate ──────────────────────────────────────
    if chat_id not in db.get_source_group_ids():
        return

    logger.info("[monitor] SOURCE GROUP HIT | chat_id=%s | msg_id=%s", chat_id, msg_id)

    # ── 2. Skip non-text messages ────────────────────────────────
    text = message.text or message.raw_text or ""
    if not text.strip():
        return

    # ── 3. Repost / forward duplicate check ──────────────────────
    # If this message was forwarded from another chat, check the ORIGINAL
    # (chat_id, msg_id) — if we've already seen it from another group, skip.
    fwd = getattr(message, "fwd_from", None)
    if fwd is not None:
        orig_chat = getattr(fwd, "from_id", None)
        orig_msg  = getattr(fwd, "channel_post", None) or getattr(fwd, "saved_from_msg_id", None)

        # Extract numeric chat id from PeerChannel / PeerUser / PeerChat
        orig_chat_id = None
        if orig_chat is not None:
            orig_chat_id = (
                getattr(orig_chat, "channel_id", None)
                or getattr(orig_chat, "user_id", None)
                or getattr(orig_chat, "chat_id", None)
            )
            if orig_chat_id:
                # Normalise to the -100 supergroup format
                orig_chat_id = int(f"-100{orig_chat_id}") if orig_chat_id > 0 else orig_chat_id

        if orig_chat_id and orig_msg:
            if db.is_repost(orig_chat_id, orig_msg):
                logger.info(
                    "[monitor] SKIPPED (repost duplicate) | orig_chat=%s | orig_msg=%s",
                    orig_chat_id, orig_msg,
                )
                return
            # Not seen before — record it so future reposts are caught
            db.mark_original(orig_chat_id, orig_msg)

    # ── 4. Processed-message dedup ───────────────────────────────
    if db.is_processed(chat_id, msg_id):
        logger.debug("[monitor] SKIPPED (already processed) | msg_id=%s", msg_id)
        return

    # ── 5. Keyword filter ────────────────────────────────────────
    result = is_job_message(text)
    if not result.is_job:
        logger.debug(
            "[monitor] SKIPPED (no keywords) | tier info | preview=%r", text[:80]
        )
        return

    logger.info(
        "[monitor] ✅ JOB DETECTED | chat=%s | msg=%s | lang=%s | tier=%s | kw=%s",
        chat_id, msg_id, result.matched_lang, result.match_tier, result.matched_keywords,
    )

    # ── 6. Resolve entities ──────────────────────────────────────
    chat = sender = None
    try:
        chat   = await event.get_chat()
        sender = await event.get_sender()
    except Exception as exc:
        logger.warning("[monitor] Could not resolve entities: %s", exc)

    group_title = get_chat_display_name(chat)
    group_link  = get_chat_link(chat)
    author_name = get_sender_display_name(sender)
    author_link: Optional[str] = (
        get_user_link(sender) if isinstance(sender, User) else None
    )

    # ── 7. Timestamp ─────────────────────────────────────────────
    msg_time = message.date
    if msg_time and msg_time.tzinfo is None:
        msg_time = msg_time.replace(tzinfo=timezone.utc)
    if not msg_time:
        msg_time = utcnow()

    # ── 8. Resolve target ────────────────────────────────────────
    target = _get_target_id()
    if not target:
        await notify_admin(
            "❌ <b>Monitor error:</b> No target group configured.\n"
            "Use /settarget in the admin bot."
        )
        return

    # ── 9. Build and send ────────────────────────────────────────
    post_text = build_job_post(
        group_title=group_title,
        group_link=group_link,
        author_name=author_name,
        author_link=author_link,
        message_time=msg_time,
        message_text=truncate(text),
        matched_keywords=result.matched_keywords,
    )

    success = await safe_send_message(client, target, post_text)
    db.mark_processed(chat_id, msg_id)

    if success:
        logger.info(
            "[monitor] ✅ FORWARDED | chat=%s | msg=%s → target=%s",
            chat_id, msg_id, target,
        )
        # ── Feature 7: record analytics stat ─────────────────────
        db.record_stat(
            source_chat_id=chat_id,
            source_title=group_title,
            matched_lang=result.matched_lang,
            matched_kw=", ".join(result.matched_keywords),
            match_tier=result.match_tier,
        )
    else:
        # ── Feature 10: notify admin on send failure ──────────────
        logger.error(
            "[monitor] ❌ SEND FAILED | chat=%s | msg=%s | target=%s",
            chat_id, msg_id, target,
        )
        await notify_admin(
            f"❌ <b>Send failed</b> after 3 retries.\n\n"
            f"Source: {group_title} (<code>{chat_id}</code>)\n"
            f"Message ID: <code>{msg_id}</code>\n"
            f"Target: <code>{target}</code>\n\n"
            f"Check that the monitor account is still a member of the target group."
        )


# ── Handler 2: auto-delete join / leave service messages ─────────

async def _on_chat_action(event: events.ChatAction.Event) -> None:
    """Delete join/leave service messages from the target group."""
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
        await client.delete_messages(target_id, [action.id])
    except Exception as exc:
        logger.warning(
            "[monitor] Could not delete service message %s: %s\n"
            "  → Make sure the account is admin with 'Delete messages' permission.",
            action.id, exc,
        )


# ── Public API ────────────────────────────────────────────────────

async def setup_monitor() -> None:
    """Connect Telethon and register event handlers."""
    logger.info("[monitor] Connecting to Telegram …")
    await client.start(phone=PHONE_NUMBER)
    me = await client.get_me()
    logger.info("[monitor] Logged in as: %s (id=%s)", me.first_name, me.id)

    client.add_event_handler(_on_new_message, events.NewMessage())
    client.add_event_handler(_on_chat_action, events.ChatAction())
    logger.info("[monitor] Event handlers registered.")

    source_groups = db.list_source_groups()
    if source_groups:
        logger.info(
            "[monitor] Monitoring %d group(s): %s",
            len(source_groups),
            [f"{g.title} ({g.chat_id})" for g in source_groups],
        )
    else:
        logger.warning("[monitor] ⚠️  No source groups. Use /addgroup.")

    target = db.get_target_group()
    if target:
        logger.info("[monitor] Target: %s (%s)", target["title"], target["chat_id"])
    else:
        logger.warning("[monitor] ⚠️  No target group. Use /settarget.")

    logger.info("[monitor] 👂 Listening …")


async def start_monitor() -> None:
    db.init_db()
    await setup_monitor()
    await client.run_until_disconnected()