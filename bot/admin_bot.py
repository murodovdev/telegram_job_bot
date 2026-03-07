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
from bot.notifier import set_bot as notifier_set_bot
from bot.utils import setup_logging

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


# ── Auth guard ────────────────────────────────────────────────────

async def _is_admin(message: Message) -> bool:
    if message.from_user and message.from_user.id == ADMIN_USER_ID:
        return True
    await message.answer("🚫 Access denied.")
    return False


# ── Keyboards ─────────────────────────────────────────────────────

def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 List Groups"),  KeyboardButton(text="➕ Add Group")],
            [KeyboardButton(text="➖ Remove Group"), KeyboardButton(text="🎯 Set Target")],
            [KeyboardButton(text="📊 Status"),       KeyboardButton(text="🧪 Test Send")],
            [KeyboardButton(text="📈 Stats"),        KeyboardButton(text="🔍 Test Keyword")],
            [KeyboardButton(text="🔎 Check Groups")],
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
    2. If client_1 fails (e.g. banned from the group), try client_2.
    3. If both fail but the input is a numeric ID, accept it as-is with a
       generic title — this lets you add groups that neither account can
       currently resolve (e.g. private groups not yet joined).
    4. Only hard-fail for @username inputs that no client can resolve.
    """
    from telethon.tl.types import Channel as _Channel

    is_numeric = input_str.lstrip("-").isdigit()

    async def _try_client(client) -> tuple:
        if client is None or not client.is_connected():
            return None, None, None
        try:
            entity   = await client.get_entity(input_str)
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

    # Both clients failed — for numeric IDs accept the raw value
    if is_numeric:
        cid = int(input_str)
        logger.warning(
            "[admin_bot] Neither client resolved %s — accepting numeric ID as-is", cid
        )
        return cid, f"Group {cid}", None

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
        "• /checkgroups — membership check for all accounts"
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
        # Membership check against the assigned account's client
        client = _get_client(account)
        is_member, warning = await _check_membership(chat_id, client)
        if not is_member:
            await message.answer(warning, parse_mode="HTML")
    else:
        await message.answer(
            f"⚠️ <b>{_html.escape(title)}</b> (<code>{chat_id}</code>) is already in the list.",
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


# /cancel ─────────────────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()
    await message.answer("Operation cancelled.", reply_markup=_main_keyboard())


# ── Main ──────────────────────────────────────────────────────────

async def run_admin_bot() -> None:
    pathlib.Path("logs").mkdir(exist_ok=True)
    db.init_db()

    bot = Bot(token=BOT_TOKEN)
    notifier_set_bot(bot)

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