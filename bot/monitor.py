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
        if chat_id not in db.get_source_group_ids(account=account_num):
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
        # message.sender_id is a plain integer available with zero cost —
        # no Telegram API call needed. Check before repost/dedup lookups.
        sender_id = message.sender_id
        if sender_id and db.is_blocked(sender_id):
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
            orig_msg  = (
                getattr(fwd, "channel_post", None)
                or getattr(fwd, "saved_from_msg_id", None)
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

        # ── 5.5 Content duplicate gate ────────────────────────────
        # Catches copy-paste spam: the same (or nearly identical) job listing
        # posted manually in multiple groups — different chat/msg IDs, no
        # fwd_from header, so Gates 3 and 4 cannot catch it.
        #
        # Two-tier check (see database.py for full design rationale):
        #   Tier 1 — exact SHA-256 hash match (O(1))
        #   Tier 2 — SequenceMatcher similarity against recent texts (O(n))
        #
        # Only runs on confirmed job posts to keep the dedup table small.
        # The hash is registered only after a successful send (below).
        if DEDUP_WINDOW_HOURS > 0:
            is_dup, dup_reason = db.is_content_duplicate(
                text,
                window_hours=DEDUP_WINDOW_HOURS,
                similarity_threshold=DEDUP_SIMILARITY_THRESHOLD,
            )
            if is_dup:
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
        chat = sender = None
        try:
            chat   = await event.get_chat()
            sender = await event.get_sender()
        except Exception as exc:
            logger.warning(
                "[monitor/acct%s] Could not resolve entities: %s", account_num, exc
            )

        group_title = get_chat_display_name(chat)
        group_link  = get_chat_link(chat)
        author_name = get_sender_display_name(sender)
        author_link: Optional[str] = (
            get_user_link(sender) if isinstance(sender, User) else None
        )

        # ── 7. Timestamp ──────────────────────────────────────────
        msg_time = message.date
        if msg_time and msg_time.tzinfo is None:
            msg_time = msg_time.replace(tzinfo=timezone.utc)
        if not msg_time:
            msg_time = utcnow()

        # ── 8. Resolve target ─────────────────────────────────────
        target = _get_target_id()
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

        success = await safe_send_message(assigned_client, target, post_text)

        if success:
            db.mark_processed(chat_id, msg_id)
            # Register content fingerprint AFTER successful send so that
            # a failed send never permanently blacklists content.
            if DEDUP_WINDOW_HOURS > 0:
                db.record_content_hash(text, source_chat=chat_id, source_msg=msg_id)
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


# ── Public API ────────────────────────────────────────────────────

async def setup_monitor() -> None:
    """Start all clients and register event handlers."""
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