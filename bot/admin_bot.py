"""
admin_bot.py – aiogram v3 Admin Control Panel (dual-account).

Changes in v4
-------------
• set_telethon_clients(c1, c2) replaces set_telethon_client(c).
• /addgroup: if account 2 is configured, asks which account to assign
  the group to via inline keyboard after resolving it.
• /listgroups: shows [A1] / [A2] badge per group.
• /checkgroups: runs membership check against both clients separately.
• /status: shows connection status for both accounts.
• _check_membership(chat_id, client) accepts the client to check against.
• _check_all_memberships_for_account(groups, client) checks one account's groups.
"""

import asyncio
import html as _html
import logging
import pathlib
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.config import BOT_TOKEN, ADMIN_USER_ID, ACCOUNT_2_ENABLED
import bot.database as db
from bot.filters import is_job_message
from bot.halal_filter import is_haram_job
from bot.groq_halal import check_halal_with_groq
from bot.notifier import set_bot as notifier_set_bot
from bot.utils import setup_logging, safe_send_message, truncate, utcnow

logger = logging.getLogger(__name__)

# Both clients — set by main.py after Telethon connects
_client_1 = None
_client_2 = None


def set_telethon_clients(c1, c2=None) -> None:
    global _client_1, _client_2
    _client_1 = c1
    _client_2 = c2


# ── FSM States ────────────────────────────────────────────────────

class AddGroupState(StatesGroup):
    waiting_for_input   = State()
    waiting_for_account = State()   # only used when account 2 is configured


class SetTargetState(StatesGroup):
    waiting_for_input = State()


class TestKeywordState(StatesGroup):
    waiting_for_text = State()


class TestHalalState(StatesGroup):
    waiting_for_text = State()


class BlockUserState(StatesGroup):
    waiting_for_user = State()   # admin forwards a msg or sends a user ID
    waiting_for_reason = State() # optional reason


class UnblockUserState(StatesGroup):
    waiting_for_selection = State()


# ── Auth guard ────────────────────────────────────────────────────

async def _is_admin(message: Message) -> bool:
    if message.from_user and message.from_user.id == ADMIN_USER_ID:
        return True
    await message.answer("🚫 Access denied.")
    return False


# ── Keyboards ─────────────────────────────────────────────────────

def _ai_filter_label() -> str:
    """Return the current AI filter button label reflecting live state."""
    return "🤖 AI Filter: ON ✅" if db.is_ai_filter_enabled() else "🤖 AI Filter: OFF ❌"


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 List Groups"),  KeyboardButton(text="➕ Add Group")],
            [KeyboardButton(text="➖ Remove Group"), KeyboardButton(text="🎯 Set Target")],
            [KeyboardButton(text="📊 Status"),       KeyboardButton(text="🧪 Test Send")],
            [KeyboardButton(text="📈 Stats"),        KeyboardButton(text="🔍 Test Keyword")],
            [KeyboardButton(text="🔎 Check Groups"), KeyboardButton(text="🚫 Blocked Users")],
            [KeyboardButton(text="🕌 Test Halal"),   KeyboardButton(text=_ai_filter_label())],
        ],
        resize_keyboard=True,
    )

def _remove_keyboard(groups: list) -> InlineKeyboardMarkup:
    buttons = []
    for g in groups:
        badge = " [A2]" if g.assigned_account == 2 else ""
        buttons.append([InlineKeyboardButton(
            text=f"❌ {g.title}{badge}",
            callback_data=f"rm:{g.chat_id}",
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Cancel", callback_data="rm:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _unblock_keyboard(blocked: list) -> InlineKeyboardMarkup:
    buttons = []
    for u in blocked:
        label = u.full_name or f"ID {u.user_id}"
        if u.username:
            label += f" (@{u.username})"
        buttons.append([InlineKeyboardButton(
            text=f"✅ Unblock {label}",
            callback_data=f"unblock:{u.user_id}",
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Cancel", callback_data="unblock:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _account_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard for choosing which account monitors a new group."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Account 1 (primary)",  callback_data="acct:1"),
            InlineKeyboardButton(text="👤 Account 2 (fallback)", callback_data="acct:2"),
        ],
        [InlineKeyboardButton(text="🔙 Cancel", callback_data="acct:cancel")],
    ])


# ── Telethon helpers ──────────────────────────────────────────────

def _get_client(account_num: int = 1):
    """Return the Telethon client for the given account number."""
    return _client_1 if account_num == 1 else _client_2


async def _resolve_group(input_str: str) -> tuple:
    """
    Returns (chat_id, title, username) or (None, error_html, None).

    Resolution strategy:
    1. Try client_1 (primary account).
    2. If client_1 fails, try client_2.
    3. If both fail but the input is a numeric ID, accept it as-is.
       Only hard-fail for @username inputs that no client can resolve.

    Key detail: Telethon's get_entity() treats a *string* of digits as a
    username lookup (which always fails for channel IDs). Numeric inputs must
    be passed as an *integer* in the -100XXXXXXXX supergroup format so Telethon
    resolves them against the account's channel peer cache instead.
    """
    from telethon.tl.types import Channel as _Channel

    is_numeric = input_str.lstrip("-").isdigit()

    # Pre-compute the correct integer peer for numeric inputs so both
    # _try_client calls and the fallback all use the same normalised value.
    if is_numeric:
        raw = int(input_str.lstrip("-"))
        _numeric_peer = int(f"-100{raw}") if int(input_str) >= 0 else int(input_str)
    else:
        _numeric_peer = None

    async def _try_client(client) -> tuple:
        if client is None or not client.is_connected():
            return None, None, None
        try:
            # IMPORTANT: pass integer for numeric IDs, not the raw string.
            # Passing a digit string makes Telethon do a username lookup,
            # which fails even when the account is a member of the channel.
            # Passing the -100-prefixed integer hits the channel peer cache.
            lookup = _numeric_peer if is_numeric else input_str
            entity   = await client.get_entity(lookup)
            chat_id  = int(f"-100{entity.id}") if isinstance(entity, _Channel) else entity.id
            title    = getattr(entity, "title", None) or str(chat_id)
            username = getattr(entity, "username", None)
            return chat_id, title, username
        except Exception as exc:
            logger.warning("[admin_bot] Cannot resolve %r via %s: %s",
                           input_str, client, exc)
            return None, None, None

    # Try account 1 first
    chat_id, title, username = await _try_client(_client_1)
    if chat_id is not None:
        return chat_id, title, username

    # Fall back to account 2
    chat_id, title, username = await _try_client(_client_2)
    if chat_id is not None:
        return chat_id, title, username

    # Both clients failed.
    # For numeric IDs we accept the normalised -100 value as-is. This handles
    # the case where neither account has joined the group yet — the admin can
    # add the ID now and join the account later.
    if is_numeric:
        logger.warning(
            "[admin_bot] Neither client resolved %s — storing as %s (unverified). "
            "Make sure the monitor account joins this group.",
            input_str, _numeric_peer,
        )
        return _numeric_peer, f"Group {_numeric_peer}", None

    # @username that no client could resolve — hard fail
    return (
        None,
        f"❌ Could not find group <code>{_html.escape(input_str)}</code>.\n\n"
        "Make sure:\n• The ID/username is correct\n"
        "• At least one monitor account is a member of that group",
        None,
    )


async def _check_membership(chat_id: int, client) -> tuple[bool, str]:
    """Check whether the given client's account is a member of chat_id."""
    if client is None or not client.is_connected():
        return True, ""

    try:
        from telethon.tl.functions.channels import GetParticipantRequest
        await client(GetParticipantRequest(chat_id, "me"))
        return True, ""
    except Exception:
        return False, (
            "⚠️ <b>Warning:</b> The monitor account does not appear to be a "
            "member of this group.\n\n"
            "The group was added to the list, but <b>you must join it manually</b> "
            "with the monitor account, otherwise no messages will be received from it."
        )


async def _check_all_memberships_for_account(
    groups: list, client, account_num: int
) -> tuple[list, list]:
    """
    Check membership of every group in *groups* against *client*.
    Returns (active, inactive) lists of SourceGroup objects.
    """
    if client is None or not client.is_connected():
        return groups, []

    from telethon.tl.functions.channels import GetParticipantRequest
    active, inactive = [], []
    for g in groups:
        try:
            await client(GetParticipantRequest(g.chat_id, "me"))
            active.append(g)
        except Exception:
            inactive.append(g)
    return active, inactive


# ── Router ────────────────────────────────────────────────────────

router = Router()


# /start ──────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()
    acct2_line = "\n• Account 2 is <b>enabled</b> ✅" if ACCOUNT_2_ENABLED else ""
    await message.answer(
        "👋 <b>Korea Ish E'lonlari Admin Bot</b>\n\n"
        "Commands:\n"
        "• /listgroups — monitored source groups\n"
        "• /addgroup — add a source group\n"
        "• /removegroup — remove a group\n"
        "• /settarget — set output group\n"
        "• /testkeyword — test if a message would be detected\n"
        "• /stats — analytics &amp; statistics\n"
        "• /test — send a test message to target\n"
        "• /status — current config\n"
        "• /checkgroups — membership check for all accounts\n"
        "• /aifilter — toggle the Groq AI halal filter on/off"
        + acct2_line,
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


# /listgroups ─────────────────────────────────────────────────────

@router.message(Command("listgroups"))
@router.message(F.text == "📋 List Groups")
async def cmd_list_groups(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()
    groups = db.list_source_groups()
    if not groups:
        await message.answer(
            "ℹ️ No source groups yet. Use ➕ Add Group.",
            reply_markup=_main_keyboard(),
        )
        return

    entries = []
    for i, g in enumerate(groups, 1):
        link  = f"https://t.me/{g.username}" if g.username else "—"
        badge = " <b>[A2]</b>" if g.assigned_account == 2 else " [A1]"
        entries.append(
            f"{i}.{badge} <b>{_html.escape(g.title)}</b>\n"
            f"   ID: <code>{g.chat_id}</code>\n"
            f"   Link: {link}\n"
            f"   Added: {g.added_at}\n"
        )

    MAX_LEN = 3800
    header  = f"📋 <b>Monitored Source Groups ({len(groups)} total)</b>\n\n"
    pages, current = [], header
    for entry in entries:
        if len(current) + len(entry) + 1 > MAX_LEN:
            pages.append(current)
            current = "📋 <b>(continued)</b>\n\n" + entry + "\n"
        else:
            current += entry + "\n"
    if current.strip():
        pages.append(current)

    for idx, page in enumerate(pages):
        is_last = idx == len(pages) - 1
        await message.answer(
            page,
            parse_mode="HTML",
            reply_markup=_main_keyboard() if is_last else None,
        )


# /addgroup ───────────────────────────────────────────────────────

@router.message(Command("addgroup"))
@router.message(F.text == "➕ Add Group")
async def cmd_add_group_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.set_state(AddGroupState.waiting_for_input)
    tip = (
        "💡 Forward any message from the group to @userinfobot to get its numeric ID.\n\n"
        + (
            "You can also send the <b>@username</b> of a public group."
            if (_client_1 and _client_1.is_connected())
            else "Send the <b>numeric ID</b> (e.g. <code>-1001234567890</code>)."
        )
    )
    await message.answer(
        f"➕ <b>Add Source Group</b>\n\n{tip}\n\n/cancel to abort.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(AddGroupState.waiting_for_input)
async def cmd_add_group_receive(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    raw = (message.text or "").strip()

    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.", reply_markup=_main_keyboard())
        return
    if not raw:
        await message.answer("❌ Please send a group ID or @username.")
        return

    chat_id, title, username = await _resolve_group(raw)
    if chat_id is None:
        await message.answer(title, parse_mode="HTML")
        return

    # If account 2 is configured, ask which account to assign this group to
    if ACCOUNT_2_ENABLED:
        await state.set_data({"chat_id": chat_id, "title": title, "username": username})
        await state.set_state(AddGroupState.waiting_for_account)
        await message.answer(
            f"➕ Group resolved: <b>{_html.escape(title)}</b>\n"
            f"🆔 <code>{chat_id}</code>\n\n"
            f"Which account should monitor this group?",
            parse_mode="HTML",
            reply_markup=_account_keyboard(),
        )
    else:
        # Only one account — add directly to account 1
        await state.clear()
        await _do_add_group(message, chat_id, title, username, account=1)


@router.message(AddGroupState.waiting_for_account)
async def cmd_add_group_account_text(message: Message, state: FSMContext):
    """Catch any text sent while the account-selection keyboard is showing."""
    if not await _is_admin(message):
        return
    raw = (message.text or "").strip()
    if raw.lower() in ("/cancel", "cancel"):
        await state.clear()
        await message.answer("Cancelled.", reply_markup=_main_keyboard())
        return
    await message.answer(
        "👆 Please tap <b>Account 1</b> or <b>Account 2</b> using the buttons above.\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("acct:"), AddGroupState.waiting_for_account)
async def cb_select_account(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("Access denied.", show_alert=True)
        return

    payload = callback.data[5:]  # strip "acct:"
    if payload == "cancel":
        await state.clear()
        await callback.message.edit_text("Cancelled.")
        await callback.message.answer("Back to menu.", reply_markup=_main_keyboard())
        await callback.answer()
        return

    account_num = int(payload)
    data        = await state.get_data()
    await state.clear()

    chat_id  = data["chat_id"]
    title    = data["title"]
    username = data.get("username")

    await callback.message.edit_text(
        f"Assigning to account {account_num}…"
    )
    await _do_add_group(callback.message, chat_id, title, username, account=account_num)
    await callback.answer()


async def _do_add_group(
    message: Message,
    chat_id: int,
    title: str,
    username,
    account: int,
) -> None:
    """Shared logic: insert group into DB, report result, check membership."""

    # ── Always normalise to the -100 supergroup format ────────────────
    # Telegram delivers all group events with this prefix. Without it the
    # stored chat_id never matches incoming events and monitoring silently
    # does nothing. We fix it here unconditionally so no bad ID can ever
    # enter the database regardless of how _resolve_group produced it.
    if chat_id > 0:
        chat_id = int(f"-100{chat_id}")

    added = db.add_source_group(
        chat_id=chat_id, title=title,
        username=username, assigned_account=account,
    )

    if added:
        link_line = f"\n🔗 https://t.me/{username}" if username else ""
        acct_label = f"\n👤 Monitored by: <b>Account {account}</b>"
        await message.answer(
            f"✅ <b>Source group added!</b>\n\n"
            f"🏢 <b>{_html.escape(title)}</b>\n"
            f"🆔 <code>{chat_id}</code>{link_line}{acct_label}",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
        client = _get_client(account)
        is_member, warning = await _check_membership(chat_id, client)
        if not is_member:
            await message.answer(warning, parse_mode="HTML")
    else:
        # Group already exists (possibly stored under a wrong/legacy chat_id).
        # get_source_group_by_id searches both the positive and -100 forms.
        existing = db.get_source_group_by_id(chat_id)
        if existing is None:
            # Should never happen, but guard just in case
            await message.answer(
                f"⚠️ Could not look up <code>{chat_id}</code> in the database.",
                parse_mode="HTML", reply_markup=_main_keyboard(),
            )
            return

        needs_id_fix    = existing.chat_id != chat_id          # legacy positive ID
        needs_reassign  = existing.assigned_account != account

        if needs_id_fix or needs_reassign:
            old_account = existing.assigned_account
            # Correct the chat_id and/or account in one UPDATE
            db.reassign_source_group(
                chat_id=existing.chat_id,       # WHERE — the value actually in the DB
                assigned_account=account,
                correct_chat_id=chat_id,        # fixes the stored ID if wrong
                title=title if title != f"Group {chat_id}" else None,
                username=username,
            )
            link_line = f"\n🔗 https://t.me/{username}" if username else ""
            if needs_reassign:
                action_line = f"👤 Moved from Account {old_account} → <b>Account {account}</b>"
            else:
                action_line = f"👤 Still monitored by <b>Account {account}</b>"
            if needs_id_fix:
                action_line += f"\n🔧 ID corrected: <code>{existing.chat_id}</code> → <code>{chat_id}</code>"
            await message.answer(
                f"🔄 <b>Group updated!</b>\n\n"
                f"🏢 <b>{_html.escape(title or existing.title)}</b>\n"
                f"🆔 <code>{chat_id}</code>{link_line}\n"
                f"{action_line}",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )
            client = _get_client(account)
            is_member, warning = await _check_membership(chat_id, client)
            if not is_member:
                await message.answer(warning, parse_mode="HTML")
        else:
            await message.answer(
                f"⚠️ <b>{_html.escape(title)}</b> (<code>{chat_id}</code>) "
                f"is already monitored by Account {account}.",
                parse_mode="HTML",
                reply_markup=_main_keyboard(),
            )


# /removegroup ────────────────────────────────────────────────────

@router.message(Command("removegroup"))
@router.message(F.text == "➖ Remove Group")
async def cmd_remove_group_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()
    groups = db.list_source_groups()
    if not groups:
        await message.answer("ℹ️ No groups to remove.", reply_markup=_main_keyboard())
        return
    await message.answer(
        "➖ <b>Remove Source Group</b>\n\nTap a group to remove it:",
        parse_mode="HTML",
        reply_markup=_remove_keyboard(groups),
    )


@router.callback_query(F.data.startswith("rm:"))
async def cb_remove_group(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("Access denied.", show_alert=True)
        return
    payload = callback.data[3:]
    if payload == "cancel":
        await callback.message.edit_text("Cancelled.")
        await callback.message.answer("Back to menu.", reply_markup=_main_keyboard())
        await callback.answer()
        return
    chat_id = int(payload)
    groups  = db.list_source_groups()
    title   = next((g.title for g in groups if g.chat_id == chat_id), str(chat_id))
    removed = db.remove_source_group(chat_id)
    if removed:
        await callback.message.edit_text(
            f"✅ <b>{_html.escape(title)}</b> removed.", parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            f"⚠️ <code>{chat_id}</code> not found.", parse_mode="HTML"
        )
    await callback.message.answer("Back to menu.", reply_markup=_main_keyboard())
    await callback.answer()


# /settarget ──────────────────────────────────────────────────────

@router.message(Command("settarget"))
@router.message(F.text == "🎯 Set Target")
async def cmd_set_target_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.set_state(SetTargetState.waiting_for_input)
    await message.answer(
        "🎯 <b>Set Target Group</b>\n\n"
        "Send the numeric ID or @username of the destination group.\n\n"
        "⚠️ Account 1 must be a member with send permission.\n\n"
        "/cancel to abort.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(SetTargetState.waiting_for_input)
async def cmd_set_target_receive(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.", reply_markup=_main_keyboard())
        return
    if not raw:
        await message.answer("❌ Please send a group ID or @username.")
        return

    chat_id, title, username = await _resolve_group(raw)
    if chat_id is None:
        await message.answer(title, parse_mode="HTML")
        return

    await state.clear()
    db.set_target_group(chat_id=chat_id, title=title, username=username)
    link_line = f"\n🔗 https://t.me/{username}" if username else ""
    await message.answer(
        f"✅ <b>Target group set!</b>\n\n"
        f"🎯 <b>{_html.escape(title)}</b>\n"
        f"🆔 <code>{chat_id}</code>{link_line}\n\n"
        f"Use 🧪 Test Send to verify.",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


# /testkeyword ────────────────────────────────────────────────────

@router.message(Command("testkeyword"))
@router.message(F.text == "🔍 Test Keyword")
async def cmd_test_keyword_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.set_state(TestKeywordState.waiting_for_text)
    await message.answer(
        "🔍 <b>Test Keyword Filter</b>\n\n"
        "Send any message text and I'll tell you whether the bot would "
        "detect it as a job post, which keywords matched, and with what confidence.\n\n"
        "/cancel to abort.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(TestKeywordState.waiting_for_text)
async def cmd_test_keyword_receive(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.", reply_markup=_main_keyboard())
        return

    await state.clear()
    result = is_job_message(raw)
    lang_flags = {"uzbek": "🇺🇿", "russian": "🇷🇺", "korean": "🇰🇷"}

    if result.is_job:
        flag = lang_flags.get(result.matched_lang or "", "🌐")
        tier_label = (
            "Tier 1 — strong keyword" if result.match_tier == 1
            else "Tier 2 — word combination"
        )
        kw_safe = _html.escape(", ".join(result.matched_keywords))
        bar = "█" * int(result.confidence * 10) + "░" * (10 - int(result.confidence * 10))
        await message.answer(
            f"✅ <b>DETECTED as job post</b>\n\n"
            f"{flag} Language: <b>{(result.matched_lang or 'unknown').capitalize()}</b>\n"
            f"🎯 Match type: <b>{tier_label}</b>\n"
            f"🔑 Keywords: <code>{kw_safe}</code>\n"
            f"📊 Confidence: {bar} {int(result.confidence * 100)}%",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
    else:
        await message.answer(
            "❌ <b>NOT detected as a job post</b>\n\n"
            "None of the job keywords were found in this message.\n\n"
            "💡 If this message <i>should</i> be detected, the keywords it "
            "uses may need to be added to the filter in <code>bot/filters.py</code>.",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )


# ── Halal review queue — Tasdiqlash / Rad etish ──────────────────

@router.callback_query(F.data.startswith("review:"))
async def cb_review(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("🚫 Ruxsat yo'q.")
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Xato callback.")
        return

    action    = parts[1]   # "approve" yoki "reject"
    review_id = int(parts[2])

    item = db.get_review_item(review_id)
    if not item:
        await callback.message.edit_text(
            callback.message.text + "\n\n⚠️ <i>Bu e'lon allaqachon qayta ishlangan yoki muddati o'tgan.</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
        await callback.answer()
        return

    if action == "reject":
        db.delete_review_item(review_id)
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n❌ Rad etildi.",
            parse_mode="HTML",
            reply_markup=None,
        )
        await callback.answer("Rad etildi.")
        logger.info("[admin_bot] Review %d rejected by admin.", review_id)
        return

    if action == "approve":
        target = db.get_cached_target_id()
        if not target:
            await callback.answer("❌ Target guruh sozlanmagan!")
            return

        from datetime import datetime, timezone

        post_text    = item["post_text"]
        group_title  = item.get("group_title") or "Unknown Group"
        group_link   = item.get("group_link") or None
        author_name  = item.get("author_name") or "Unknown"
        author_link  = item.get("author_link") or None
        msg_time_str = item.get("msg_time") or ""

        try:
            msg_time = datetime.fromisoformat(msg_time_str) if msg_time_str else utcnow()
        except ValueError:
            msg_time = utcnow()
        if msg_time.tzinfo is None:
            msg_time = msg_time.replace(tzinfo=timezone.utc)

        import html as _html
        from datetime import timedelta

        KST = timezone(timedelta(hours=9))
        kst_time = msg_time.astimezone(KST)
        time_str = kst_time.strftime("%Y-%m-%d %H:%M:%S")

        safe_group  = _html.escape(group_title)
        safe_author = _html.escape(author_name)
        safe_text   = _html.escape(truncate(post_text))

        group_part  = f'<a href="{_html.escape(group_link)}">{safe_group}</a>' if group_link else safe_group
        author_part = f'<a href="{_html.escape(author_link)}">{safe_author}</a>' if author_link else safe_author

        formatted = (
            f"⚠️ <b>Yangi ish e'loni:</b>\n\n"
            f"<b>Guruh:</b> {group_part}\n"
            f"<b>Muallif:</b> {author_part}\n"
            f"<b>Vaqt:</b> {time_str}\n\n"
            f"<b>Xabar matni:</b>\n"
            f"{safe_text}"
        )

        success = False
        if _client_1 and _client_1.is_connected():
            success = await safe_send_message(_client_1, target, formatted)

        if success:
            db.delete_review_item(review_id)
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n✅ Tasdiqlandi va guruhga yuborildi.",
                parse_mode="HTML",
                reply_markup=None,
            )
            await callback.answer("✅ Guruhga yuborildi!")
            logger.info("[admin_bot] Review %d approved and forwarded.", review_id)
        else:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n⚠️ Yuborishda xato. Qayta urinib ko'ring.",
                parse_mode="HTML",
                reply_markup=None,
            )
            await callback.answer("⚠️ Yuborishda xato!")


# /testhalal ──────────────────────────────────────────────────────

@router.message(Command("testhalal"))
@router.message(F.text == "🕌 Test Halal")
async def cmd_test_halal_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.set_state(TestHalalState.waiting_for_text)
    await message.answer(
        "🕌 <b>Test Halal Filter</b>\n\n"
        "Ish e'loni matnini yuboring — ikki qatlamli halollik tekshiruvi amalga oshiriladi:\n\n"
        "1️⃣ <b>Keyword filtr</b> — aniq harom kalit so'zlar\n"
        "2️⃣ <b>Groq AI</b> — kontekstni tushunib baholash\n\n"
        "/cancel — bekor qilish.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(TestHalalState.waiting_for_text)
async def cmd_test_halal_receive(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Bekor qilindi.", reply_markup=_main_keyboard())
        return

    await state.clear()
    await message.answer("⏳ Tekshirilmoqda...", parse_mode="HTML")

    lines = ["🕌 <b>Halal Filter Test Natijasi</b>\n"]

    # ── 1-qadam: Keyword filtr ────────────────────────────────────
    haram_kw = is_haram_job(raw)
    if haram_kw.is_haram:
        kw_safe = _html.escape(", ".join(haram_kw.matched_keywords))
        cat_safe = _html.escape(haram_kw.category or "")
        lines.append("1️⃣ <b>Keyword filtr:</b> ❌ HAROM")
        lines.append(f"   Toifa: <code>{cat_safe}</code>")
        lines.append(f"   Kalit so'z: <code>{kw_safe}</code>")
        lines.append("\n🚫 <b>Xulosa: BLOKLANGAN</b> (keyword filtrida)")
        lines.append("Groq AI tekshiruvi shart emas — aniq harom kalit so'z topildi.")
        await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=_main_keyboard())
        return

    lines.append("1️⃣ <b>Keyword filtr:</b> ✅ Aniq harom kalit so'z topilmadi")

    # ── 2-qadam: Groq AI ──────────────────────────────────────────
    lines.append("2️⃣ <b>Groq AI:</b> so'rov yuborilmoqda...")
    groq = await check_halal_with_groq(raw)

    if groq.api_error:
        lines[-1] = "2️⃣ <b>Groq AI:</b> ⚠️ API xatosi — tekshirib bo'lmadi"
        lines.append("\n⚠️ <b>Xulosa: O'TKAZIB YUBORILADI</b>")
        lines.append("API ishlamay qolsa bot to'xtab qolmasligi uchun o'tkazib yuboriladi.")
    elif groq.verdict == "halol" or groq.verdict == "unclear":
        reason_safe = _html.escape(groq.reason or "")
        verdict_label = "✅ HALOL" if groq.verdict == "halol" else "🤔 NOANIQ (unclear)"
        lines[-1] = f"2️⃣ <b>Groq AI:</b> {verdict_label}"
        if reason_safe:
            lines.append(f"   Sabab: <i>{reason_safe}</i>")
        if groq.verdict == "unclear":
            lines.append("   <i>Noaniq holatlar o'tkazib yuboriladi (ehtiyotkor yondashuv)</i>")
        lines.append("\n✅ <b>Xulosa: GURUHGA YUBORILADI</b>")
    else:
        reason_safe = _html.escape(groq.reason or "")
        verdict_safe = _html.escape(groq.verdict or "")
        lines[-1] = f"2️⃣ <b>Groq AI:</b> ❌ {verdict_safe.upper()}"
        if reason_safe:
            lines.append(f"   Sabab: <i>{reason_safe}</i>")
        lines.append("\n🚫 <b>Xulosa: BLOKLANGAN</b> (Groq AI)")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=_main_keyboard())


# /stats ──────────────────────────────────────────────────────────

@router.message(Command("stats"))
@router.message(F.text == "📈 Stats")
async def cmd_stats(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    s = db.get_stats_summary(days=7)
    lang_flags = {"uzbek": "🇺🇿", "russian": "🇷🇺", "korean": "🇰🇷"}

    lines = ["📈 <b>Statistics (last 7 days)</b>\n"]
    lines.append(f"📨 Total forwarded: <b>{s['total']}</b>")
    lines.append(f"📅 Forwarded today: <b>{s['today']}</b>\n")

    if s["by_group"]:
        lines.append("🏆 <b>Top source groups:</b>")
        for i, (title, cnt) in enumerate(s["by_group"], 1):
            lines.append(f"  {i}. {_html.escape(title)} — <b>{cnt}</b>")
        lines.append("")

    if s["by_lang"]:
        lines.append("🌐 <b>By language:</b>")
        for lang, cnt in s["by_lang"].items():
            flag = lang_flags.get(lang, "🌐")
            lines.append(f"  {flag} {lang.capitalize()}: <b>{cnt}</b>")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


# /test ───────────────────────────────────────────────────────────

@router.message(Command("test"))
@router.message(F.text == "🧪 Test Send")
async def cmd_test(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()
    target_cfg = db.get_target_group()
    if not target_cfg:
        await message.answer("❌ No target set. Use 🎯 Set Target.", reply_markup=_main_keyboard())
        return
    if _client_1 is None or not _client_1.is_connected():
        await message.answer(
            "⚠️ Account 1 not connected. Run <code>python main.py</code>.",
            parse_mode="HTML", reply_markup=_main_keyboard(),
        )
        return
    target = target_cfg["chat_id"]
    await message.answer(
        f"⏳ Sending test to <b>{_html.escape(target_cfg['title'])}</b> …",
        parse_mode="HTML",
    )
    try:
        await _client_1.send_message(
            target,
            "✅ <b>Test Message</b>\n\nForwarding is working correctly!\n\n"
            "Bu test xabari. Ish e'lonlari shu yerga yuboriladi.",
            parse_mode="html",
            link_preview=False,
        )
        await message.answer(
            f"✅ <b>Test successful!</b> Check {_html.escape(target_cfg['title'])}.",
            parse_mode="HTML", reply_markup=_main_keyboard(),
        )
    except Exception as exc:
        await message.answer(
            f"❌ <b>Test failed!</b>\n\n<code>{_html.escape(str(exc))}</code>",
            parse_mode="HTML", reply_markup=_main_keyboard(),
        )


# /status ─────────────────────────────────────────────────────────

@router.message(Command("status"))
@router.message(F.text == "📊 Status")
async def cmd_status(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    groups_a1 = db.list_source_groups(account=1)
    groups_a2 = db.list_source_groups(account=2)
    target     = db.get_target_group()
    processed  = db.get_processed_count()

    def _conn_status(client, label):
        if client and client.is_connected():
            return f"🟢 {label} connected"
        return f"🔴 {label} not connected"

    lines = ["📊 <b>Bot Status</b>\n"]
    lines.append(_conn_status(_client_1, "Account 1"))
    if ACCOUNT_2_ENABLED:
        lines.append(_conn_status(_client_2, "Account 2"))
    lines.append("")

    lines.append(f"👁 Account 1 groups: <b>{len(groups_a1)}</b>")
    if ACCOUNT_2_ENABLED:
        lines.append(f"👁 Account 2 groups: <b>{len(groups_a2)}</b>")
    lines.append("")

    target_str = (
        f"<b>{_html.escape(target['title'])}</b> (<code>{target['chat_id']}</code>)"
        if target else "❌ Not set — use 🎯 Set Target"
    )
    lines.append(f"🎯 Target: {target_str}")
    lines.append(f"📨 Total forwarded: <b>{processed}</b>")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


# /checkgroups ────────────────────────────────────────────────────

@router.message(Command("checkgroups"))
@router.message(F.text == "🔎 Check Groups")
async def cmd_check_groups(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    all_groups = db.list_source_groups()
    if not all_groups:
        await message.answer(
            "ℹ️ No source groups registered yet. Use ➕ Add Group.",
            reply_markup=_main_keyboard(),
        )
        return

    if _client_1 is None or not _client_1.is_connected():
        await message.answer(
            "⚠️ Account 1 not connected.\n"
            "Run <code>python main.py</code> to enable group status checking.",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
        return

    total = len(all_groups)
    wait_msg = await message.answer(
        f"⏳ Checking membership for <b>{total}</b> groups across "
        f"{'2 accounts' if ACCOUNT_2_ENABLED else '1 account'}. "
        f"This may take a few seconds...",
        parse_mode="HTML",
    )

    groups_a1 = db.list_source_groups(account=1)
    groups_a2 = db.list_source_groups(account=2)

    active_1, inactive_1 = await _check_all_memberships_for_account(
        groups_a1, _client_1, 1
    )
    active_2, inactive_2 = ([], [])
    if ACCOUNT_2_ENABLED and _client_2 and _client_2.is_connected():
        active_2, inactive_2 = await _check_all_memberships_for_account(
            groups_a2, _client_2, 2
        )
    elif ACCOUNT_2_ENABLED:
        inactive_2 = groups_a2  # can't check — assume all inactive

    lines = ["🔎 <b>Group Status Check</b>\n"]

    # Account 1 section
    lines.append(
        f"<b>Account 1</b> — ✅ {len(active_1)} subscribed, "
        f"❌ {len(inactive_1)} not subscribed"
    )
    if inactive_1:
        lines.append("  ❌ <b>Not subscribed (Account 1):</b>")
        for g in inactive_1:
            link = f"https://t.me/{g.username}" if g.username else f"<code>{g.chat_id}</code>"
            lines.append(f"    • <b>{_html.escape(g.title)}</b> — {link}")

    # Account 2 section (only if enabled)
    if ACCOUNT_2_ENABLED:
        lines.append("")
        conn_note = "" if (_client_2 and _client_2.is_connected()) else " (not connected)"
        lines.append(
            f"<b>Account 2</b>{conn_note} — ✅ {len(active_2)} subscribed, "
            f"❌ {len(inactive_2)} not subscribed"
        )
        if inactive_2:
            lines.append("  ❌ <b>Not subscribed (Account 2):</b>")
            for g in inactive_2:
                link = f"https://t.me/{g.username}" if g.username else f"<code>{g.chat_id}</code>"
                lines.append(f"    • <b>{_html.escape(g.title)}</b> — {link}")

    total_inactive = len(inactive_1) + len(inactive_2)
    if total_inactive:
        lines.append(
            "\n⚠️ Join the above groups with the respective monitor account "
            "to start receiving their messages."
        )

    try:
        await wait_msg.delete()
    except Exception:
        pass

    MAX_LEN = 3800
    full_text = "\n".join(lines)
    if len(full_text) <= MAX_LEN:
        await message.answer(full_text, parse_mode="HTML", reply_markup=_main_keyboard())
    else:
        chunks, current = [], ""
        for line in lines:
            if len(current) + len(line) + 1 > MAX_LEN:
                chunks.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current.strip():
            chunks.append(current)
        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            await message.answer(
                chunk,
                parse_mode="HTML",
                reply_markup=_main_keyboard() if is_last else None,
            )


# /blockuser ──────────────────────────────────────────────────────

@router.message(Command("blockuser"))
@router.message(F.text == "🚫 Blocked Users")
async def cmd_block_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    # "🚫 Blocked Users" button shows the list + block/unblock options
    blocked = db.list_blocked_users()
    lines = [f"🚫 <b>Blocked Users ({len(blocked)})</b>\n"]
    if blocked:
        for i, u in enumerate(blocked, 1):
            name = _html.escape(u.full_name or f"ID {u.user_id}")
            uname = f" @{u.username}" if u.username else ""
            reason = f"\n     Reason: {_html.escape(u.reason)}" if u.reason else ""
            lines.append(
                f"{i}. <b>{name}</b>{uname}\n"
                f"   ID: <code>{u.user_id}</code>\n"
                f"   Blocked: {u.blocked_at}{reason}\n"
            )
    else:
        lines.append("No users are currently blocked.")

    lines.append("\nUse /addblock to block a user.")
    lines.append("Use /unblockuser to unblock a user.")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


@router.message(Command("addblock"))
async def cmd_addblock_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.set_state(BlockUserState.waiting_for_user)
    await message.answer(
        "🚫 <b>Block a User</b>\n\n"
        "Send the user's Telegram ID, or <b>forward any message</b> from "
        "that user to this chat.\n\n"
        "💡 To get a user's ID: forward their message to @userinfobot.\n\n"
        "/cancel to abort.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(BlockUserState.waiting_for_user)
async def cmd_addblock_receive_user(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return

    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.", reply_markup=_main_keyboard())
        return

    user_id   = None
    full_name = ""
    username  = None

    # Case 1: admin forwarded a message from the target user
    fwd = getattr(message, "forward_from", None)
    if fwd is not None:
        user_id   = fwd.id
        full_name = f"{fwd.first_name or ''} {fwd.last_name or ''}".strip()
        username  = getattr(fwd, "username", None)

    # Case 2: admin typed a numeric user ID
    elif raw.lstrip("-").isdigit():
        user_id = int(raw)

    if not user_id:
        await message.answer(
            "❌ Could not identify a user.\n\n"
            "Please either:\n"
            "• Forward a message from the user to this chat, or\n"
            "• Send their numeric Telegram user ID.\n\n"
            "/cancel to abort.",
            parse_mode="HTML",
        )
        return

    # Save what we know so far, then ask for an optional reason
    await state.set_data({
        "user_id":   user_id,
        "full_name": full_name,
        "username":  username,
    })
    await state.set_state(BlockUserState.waiting_for_reason)

    name_display = _html.escape(full_name) if full_name else f"<code>{user_id}</code>"
    await message.answer(
        f"👤 User identified: <b>{name_display}</b>\n"
        f"🆔 <code>{user_id}</code>\n\n"
        "Add a reason for blocking (optional).\n"
        "Send <b>-</b> to skip, or type a short reason (e.g. <i>spam, repeated posts</i>).",
        parse_mode="HTML",
    )


@router.message(BlockUserState.waiting_for_reason)
async def cmd_addblock_receive_reason(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return

    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Cancelled.", reply_markup=_main_keyboard())
        return

    data      = await state.get_data()
    await state.clear()

    user_id   = data["user_id"]
    full_name = data["full_name"]
    username  = data.get("username")
    reason    = None if raw == "-" else raw

    blocked = db.block_user(
        user_id=user_id,
        full_name=full_name,
        username=username,
        reason=reason,
    )

    name_display = _html.escape(full_name) if full_name else f"ID {user_id}"
    uname_line   = f"\n👤 @{username}" if username else ""
    reason_line  = f"\n📝 Reason: {_html.escape(reason)}" if reason else ""

    if blocked:
        await message.answer(
            f"✅ <b>User blocked!</b>\n\n"
            f"<b>{name_display}</b>{uname_line}\n"
            f"🆔 <code>{user_id}</code>{reason_line}\n\n"
            f"All future messages from this user will be ignored.",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
    else:
        await message.answer(
            f"⚠️ <b>{name_display}</b> (<code>{user_id}</code>) is already blocked.",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )


# /unblockuser ────────────────────────────────────────────────────

@router.message(Command("unblockuser"))
async def cmd_unblock_start(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    blocked = db.list_blocked_users()
    if not blocked:
        await message.answer(
            "ℹ️ No users are currently blocked.",
            reply_markup=_main_keyboard(),
        )
        return

    await message.answer(
        "✅ <b>Unblock a User</b>\n\nTap a user to unblock them:",
        parse_mode="HTML",
        reply_markup=_unblock_keyboard(blocked),
    )


@router.callback_query(F.data.startswith("unblock:"))
async def cb_unblock_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("Access denied.", show_alert=True)
        return

    payload = callback.data[8:]  # strip "unblock:"
    if payload == "cancel":
        await callback.message.edit_text("Cancelled.")
        await callback.message.answer("Back to menu.", reply_markup=_main_keyboard())
        await callback.answer()
        return

    user_id = int(payload)
    blocked = db.list_blocked_users()
    user    = next((u for u in blocked if u.user_id == user_id), None)
    name    = _html.escape(user.full_name if user and user.full_name else str(user_id))

    removed = db.unblock_user(user_id)
    if removed:
        await callback.message.edit_text(
            f"✅ <b>{name}</b> (<code>{user_id}</code>) has been unblocked.",
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            f"⚠️ <code>{user_id}</code> was not found in the blocked list.",
            parse_mode="HTML",
        )
    await callback.message.answer("Back to menu.", reply_markup=_main_keyboard())
    await callback.answer()


# /cancel ─────────────────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()
    await message.answer("Operation cancelled.", reply_markup=_main_keyboard())


# /clearstats ─────────────────────────────────────────────────────

@router.message(Command("clearstats"))
async def cmd_clear_stats(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()
    # Confirm step
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data="clearstats:confirm"),
        InlineKeyboardButton(text="❌ Bekor qilish",  callback_data="clearstats:cancel"),
    ]])
    await message.answer(
        "⚠️ <b>Statistikani tozalash</b>\n\n"
        "Barcha forwarded_stats ma'lumotlari o'chiriladi.\n"
        "Bu amalni qaytarib bo'lmaydi.\n\n"
        "Davom etasizmi?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("clearstats:"))
async def cb_clear_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_USER_ID:
        await callback.answer("🚫 Ruxsat yo'q.")
        return
    action = callback.data.split(":")[1]
    if action == "cancel":
        await callback.message.edit_text(
            "❌ Bekor qilindi.",
            reply_markup=None,
        )
        await callback.answer("Bekor qilindi.")
        return
    deleted = db.clear_all_stats()
    await callback.message.edit_text(
        f"✅ <b>Statistika tozalandi.</b>\n\n"
        f"O'chirilgan yozuvlar: <b>{deleted}</b>",
        parse_mode="HTML",
        reply_markup=None,
    )
    await callback.answer("✅ Statistika tozalandi!")
    logger.info("[admin_bot] Stats cleared by admin. %d rows deleted.", deleted)



# ── /aifilter — toggle Groq AI halal filter ───────────────────────
#
# When ON  (default): every qualifying job post is sent to Groq for an
#   Islamic-compliance check.  Posts flagged as "haram" or "unclear" go
#   to the admin review queue instead of the target group.
#   Adds 1–10 s latency per post (external API call).
#
# When OFF: the Groq step is skipped entirely — posts are forwarded the
#   instant they pass the keyword filter.  The keyword-based halal filter
#   (halal_filter.py) still runs; only the Groq AI step is bypassed.
#
# The state is persisted in the bot_settings DB table so it survives
# restarts.  The monitor.py hot path reads an in-memory cache (O(1))
# that is invalidated as soon as this command updates the DB.

# ── /importgroups ────────────────────────────────────────────────
#
# Format (bot o'zining /listgroups chiqishidan nusxa olinadi):
#
#   [A1] Guruh nomi
#   ID: -1001234567890
#   Link: https://t.me/username
#
# Yoki oddiy format (har qatorda):
#   -1001234567890 Guruh nomi username
#
# Ikki formatni ham avtomatik aniqlaydi.
# Mavjud guruhlar o'tkazib yuboriladi (xato yo'q).

class ImportGroupsState(StatesGroup):
    waiting_for_data = State()


@router.message(Command("importgroups"))
async def cmd_importgroups(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.set_state(ImportGroupsState.waiting_for_data)
    await message.answer(
        "📥 <b>Guruhlar ro'yxatini yuboring</b>\n\n"
        "Bot o'zining <code>/listgroups</code> chiqishini to'g'ridan-to'g'ri "
        "shu yerga paste qiling.\n\n"
        "Yoki har qatorda:\n"
        "<code>-1001234567890 Guruh nomi username</code>\n\n"
        "Mavjud guruhlar o'tkazib yuboriladi.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(ImportGroupsState.waiting_for_data)
async def handle_importgroups_data(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return

    text = message.text or ""
    if not text.strip():
        await message.answer("Bo'sh xabar. Bekor qilindi.", reply_markup=_main_keyboard())
        await state.clear()
        return

    import re

    added   = 0
    skipped = 0
    errors  = 0
    parsed  = []   # list of (chat_id, title, username, account)

    lines = text.splitlines()

    # ── Format 1: /listgroups chiqishi ──────────────────────────
    # Har guruh uchun 3 qator:
    #   [A1] Guruh nomi          ← title + account
    #   ID: -1001234567890        ← chat_id
    #   Link: https://t.me/xxx   ← username (ixtiyoriy)
    i = 0
    listgroups_found = False
    while i < len(lines):
        line = lines[i].strip()
        # [A1] yoki [A2] bilan boshlangan qator
        # Raqam prefiksi bilan yoki prefikssiz [A1] qatorni aniqlash
        # Format 1: "1. [A1] Guruh nomi"  (listgroups chiqishi)
        # Format 2: "[A1] Guruh nomi"     (prefikssiz)
        m = re.match(r'^(?:\d+\.\s*)?\[A(\d)\]\s+(.+)))$', line)
        if m:
            listgroups_found = True
            account = int(m.group(1))
            title   = m.group(2).strip()
            chat_id  = None
            username = None
            # Keyingi qatorlarda ID va Link ni qidirish
            for j in range(i + 1, min(i + 5, len(lines))):
                l2 = lines[j].strip()
                id_m   = re.match(r'^ID:\s*(-?\d+)$', l2)
                link_m = re.match(r'^Link:\s*(?:https://t\.me/([^\s]+)|—)?$', l2)
                if id_m:
                    chat_id = int(id_m.group(1))
                elif link_m and link_m.group(1):
                    username = link_m.group(1)
            if chat_id:
                parsed.append((chat_id, title, username, account))
            i += 4
            continue
        i += 1

    # ── Format 2: oddiy qator  -1001234567890 Guruh nomi username ──
    if not listgroups_found:
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('📋'):
                continue
            parts = line.split()
            if not parts:
                continue
            # Birinchi token raqammi?
            try:
                chat_id = int(parts[0])
            except ValueError:
                continue
            title    = ' '.join(parts[1:-1]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else str(chat_id))
            username = parts[-1] if len(parts) > 2 and not parts[-1].startswith('-') else None
            parsed.append((chat_id, title, username, 1))

    if not parsed:
        await state.clear()
        await message.answer(
            "⚠️ Hech qanday guruh topilmadi.\n\n"
            "/listgroups natijasini to'liq paste qiling.",
            reply_markup=_main_keyboard(),
        )
        return

    # ── Databasega saqlash ──────────────────────────────────────
    for chat_id, title, username, account in parsed:
        try:
            db.add_source_group(
                chat_id=chat_id,
                title=title,
                username=username,
                assigned_account=account,
            )
            added += 1
        except Exception as e:
            err_str = str(e).lower()
            if "unique" in err_str or "duplicate" in err_str or "already" in err_str:
                skipped += 1
            else:
                errors += 1
                logger.warning("[importgroups] %s: %s", chat_id, e)

    # ── Natija ─────────────────────────────────────────────────
    total = db.list_source_groups()
    result = (
        f"✅ <b>Import yakunlandi!</b>\n\n"
        f"📥 Yuborildi:          <b>{len(parsed)}</b> ta\n"
        f"✅ Yangi qo\'shildi:    <b>{added}</b> ta\n"
        f"⏭️ Allaqachon bor:    <b>{skipped}</b> ta\n"
    )
    if errors:
        result += f"⚠️ Xato:              <b>{errors}</b> ta\n"
    result += f"\n📊 Jami database da: <b>{len(total)}</b> ta guruh"

    if added > 0:
        result += "\n\n⚠️ Botni qayta ishga tushiring (Railway Redeploy)."

    logger.info(
        "[admin_bot] importgroups: parsed=%d added=%d skipped=%d errors=%d",
        len(parsed), added, skipped, errors,
    )

    await state.clear()
    await message.answer(result, parse_mode="HTML", reply_markup=_main_keyboard())



# ── /aifilter ────────────────────────────────────────────────────

@router.message(Command("aifilter"))
@router.message(F.text.in_({"🤖 AI Filter: ON ✅", "🤖 AI Filter: OFF ❌"}))
async def cmd_toggle_ai_filter(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    new_state = db.toggle_ai_filter()

    if new_state:
        status_icon = "✅"
        status_text = "YOQildi"
        detail = (
            "Groq AI filtri endi <b>YOQIQ</b>.\n\n"
            "Har bir ish e'loni Groq orqali tekshiriladi.\n"
            "Harom yoki noaniq natijalar admin review queue ga yuboriladi."
        )
    else:
        status_icon = "❌"
        status_text = "O'chirildi"
        detail = (
            "Groq AI filtri endi <b>O'CHIQ</b>.\n\n"
            "Ish e'lonlari Groq tekshiruvisiz darhol yuboriladi.\n"
            "Kalit so'z asosidagi halal filtri hali ham ishlaydi."
        )

    logger.info(
        "[admin_bot] AI filter toggled to %s by admin %s",
        "ON" if new_state else "OFF",
        message.from_user.id,
    )

    await message.answer(
        f"{status_icon} <b>AI Filter {status_text}</b>\n\n"
        f"{detail}",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),   # button label auto-updates
    )


# ── Main ──────────────────────────────────────────────────────────

async def run_admin_bot() -> None:
    pathlib.Path("logs").mkdir(exist_ok=True)
    db.init_db()

    bot = Bot(token=BOT_TOKEN)
    notifier_set_bot(bot)

    # Monitor.py ga bot instansini uzatamiz — review queue xabarlari yuborish uchun
    from bot.monitor import set_aiogram_bot
    set_aiogram_bot(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("[admin_bot] Starting admin bot …")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    setup_logging("admin_bot")
    try:
        asyncio.run(run_admin_bot())
    except KeyboardInterrupt:
        logger.info("[admin_bot] Stopped.")
        sys.exit(0), line)
        if m:
            listgroups_found = True
            account = int(m.group(1))
            title   = m.group(2).strip()
            chat_id  = None
            username = None
            # Keyingi qatorlarda ID va Link ni qidirish
            for j in range(i + 1, min(i + 5, len(lines))):
                l2 = lines[j].strip()
                id_m   = re.match(r'^ID:\s*(-?\d+)$', l2)
                link_m = re.match(r'^Link:\s*(?:https://t\.me/([^\s]+)|—)?$', l2)
                if id_m:
                    chat_id = int(id_m.group(1))
                elif link_m and link_m.group(1):
                    username = link_m.group(1)
            if chat_id:
                parsed.append((chat_id, title, username, account))
            i += 4
            continue
        i += 1

    # ── Format 2: oddiy qator  -1001234567890 Guruh nomi username ──
    if not listgroups_found:
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('📋'):
                continue
            parts = line.split()
            if not parts:
                continue
            # Birinchi token raqammi?
            try:
                chat_id = int(parts[0])
            except ValueError:
                continue
            title    = ' '.join(parts[1:-1]) if len(parts) > 2 else (parts[1] if len(parts) > 1 else str(chat_id))
            username = parts[-1] if len(parts) > 2 and not parts[-1].startswith('-') else None
            parsed.append((chat_id, title, username, 1))

    if not parsed:
        await state.clear()
        await message.answer(
            "⚠️ Hech qanday guruh topilmadi.\n\n"
            "/listgroups natijasini to'liq paste qiling.",
            reply_markup=_main_keyboard(),
        )
        return

    # ── Databasega saqlash ──────────────────────────────────────
    for chat_id, title, username, account in parsed:
        try:
            db.add_source_group(
                chat_id=chat_id,
                title=title,
                username=username,
                assigned_account=account,
            )
            added += 1
        except Exception as e:
            err_str = str(e).lower()
            if "unique" in err_str or "duplicate" in err_str or "already" in err_str:
                skipped += 1
            else:
                errors += 1
                logger.warning("[importgroups] %s: %s", chat_id, e)

    # ── Natija ─────────────────────────────────────────────────
    total = db.list_source_groups()
    result = (
        f"✅ <b>Import yakunlandi!</b>\n\n"
        f"📥 Yuborildi:          <b>{len(parsed)}</b> ta\n"
        f"✅ Yangi qo\'shildi:    <b>{added}</b> ta\n"
        f"⏭️ Allaqachon bor:    <b>{skipped}</b> ta\n"
    )
    if errors:
        result += f"⚠️ Xato:              <b>{errors}</b> ta\n"
    result += f"\n📊 Jami database da: <b>{len(total)}</b> ta guruh"

    if added > 0:
        result += "\n\n⚠️ Botni qayta ishga tushiring (Railway Redeploy)."

    logger.info(
        "[admin_bot] importgroups: parsed=%d added=%d skipped=%d errors=%d",
        len(parsed), added, skipped, errors,
    )

    await state.clear()
    await message.answer(result, parse_mode="HTML", reply_markup=_main_keyboard())



# ── /aifilter ────────────────────────────────────────────────────

@router.message(Command("aifilter"))
@router.message(F.text.in_({"🤖 AI Filter: ON ✅", "🤖 AI Filter: OFF ❌"}))
async def cmd_toggle_ai_filter(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    new_state = db.toggle_ai_filter()

    if new_state:
        status_icon = "✅"
        status_text = "YOQildi"
        detail = (
            "Groq AI filtri endi <b>YOQIQ</b>.\n\n"
            "Har bir ish e'loni Groq orqali tekshiriladi.\n"
            "Harom yoki noaniq natijalar admin review queue ga yuboriladi."
        )
    else:
        status_icon = "❌"
        status_text = "O'chirildi"
        detail = (
            "Groq AI filtri endi <b>O'CHIQ</b>.\n\n"
            "Ish e'lonlari Groq tekshiruvisiz darhol yuboriladi.\n"
            "Kalit so'z asosidagi halal filtri hali ham ishlaydi."
        )

    logger.info(
        "[admin_bot] AI filter toggled to %s by admin %s",
        "ON" if new_state else "OFF",
        message.from_user.id,
    )

    await message.answer(
        f"{status_icon} <b>AI Filter {status_text}</b>\n\n"
        f"{detail}",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),   # button label auto-updates
    )


# ── Main ──────────────────────────────────────────────────────────

async def run_admin_bot() -> None:
    pathlib.Path("logs").mkdir(exist_ok=True)
    db.init_db()

    bot = Bot(token=BOT_TOKEN)
    notifier_set_bot(bot)

    # Monitor.py ga bot instansini uzatamiz — review queue xabarlari yuborish uchun
    from bot.monitor import set_aiogram_bot
    set_aiogram_bot(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("[admin_bot] Starting admin bot …")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    setup_logging("admin_bot")
    try:
        asyncio.run(run_admin_bot())
    except KeyboardInterrupt:
        logger.info("[admin_bot] Stopped.")
        sys.exit(0)