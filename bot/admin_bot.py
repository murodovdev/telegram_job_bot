"""
admin_bot.py – aiogram v3 Admin Control Panel.

Changes in v3
-------------
• Feature 5  — /testkeyword: paste any text and see if the filter catches it.
• Feature 6  — Group membership verification when adding a group.
• Feature 7  — /stats command with full analytics breakdown.
• Feature 10 — Registers aiogram Bot with notifier.set_bot() on startup.
• Pagination for /listgroups (42+ groups no longer crashes).
• html.escape() on all group titles (fixes "E'lonlari" apostrophe crash).
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

from bot.config import BOT_TOKEN, ADMIN_USER_ID
import bot.database as db
from bot.filters import is_job_message
from bot.notifier import set_bot as notifier_set_bot
from bot.utils import setup_logging

logger = logging.getLogger(__name__)

_telethon_client = None


def set_telethon_client(client) -> None:
    global _telethon_client
    _telethon_client = client


# ── FSM States ────────────────────────────────────────────────────

class AddGroupState(StatesGroup):
    waiting_for_input = State()


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
        buttons.append([InlineKeyboardButton(
            text=f"❌ {g.title}",
            callback_data=f"rm:{g.chat_id}",
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Cancel", callback_data="rm:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Telethon helpers ──────────────────────────────────────────────

async def _resolve_group(input_str: str) -> tuple:
    """Returns (chat_id, title, username) or (None, error_html, None)."""
    if _telethon_client is None or not _telethon_client.is_connected():
        if input_str.lstrip("-").isdigit():
            cid = int(input_str)
            return cid, f"Group {cid}", None
        return None, "⚠️ Run <code>python main.py</code> for @username lookup.", None

    try:
        entity = await _telethon_client.get_entity(input_str)
        from telethon.tl.types import Channel
        chat_id = (
            int(f"-100{entity.id}") if isinstance(entity, Channel) else entity.id
        )
        title    = getattr(entity, "title", None) or str(chat_id)
        username = getattr(entity, "username", None)
        return chat_id, title, username
    except Exception as exc:
        logger.warning("[admin_bot] Cannot resolve %r: %s", input_str, exc)
        return (
            None,
            f"❌ Could not find group <code>{_html.escape(input_str)}</code>.\n\n"
            "Make sure:\n• The ID/username is correct\n"
            "• Your account is a member of that group",
            None,
        )


async def _check_membership(chat_id: int) -> tuple[bool, str]:
    """
    Feature 6 — verify the personal account is actually a member.
    Returns (is_member, warning_text).
    """
    if _telethon_client is None or not _telethon_client.is_connected():
        return True, ""  # can't check, assume OK

    try:
        from telethon.tl.functions.channels import GetParticipantRequest
        from telethon.errors import UserNotParticipantError, ChatAdminRequiredError
        await _telethon_client(GetParticipantRequest(chat_id, "me"))
        return True, ""
    except Exception:
        return (
            False,
            "⚠️ <b>Warning:</b> The monitor account does not appear to be a member "
            "of this group.\n\n"
            "The group was added to the list, but <b>you must join it manually</b> "
            "with the monitor account, otherwise no messages will be received from it.",
        )


async def _check_all_memberships(groups: list) -> tuple[list, list]:
    """
    Check every registered source group and return two lists:
      active   — groups the monitor account IS a member of
      inactive — groups the monitor account is NOT a member of

    Each entry is a SourceGroup dataclass object.
    Falls back to (all_groups, []) if Telethon is not connected.
    """
    if _telethon_client is None or not _telethon_client.is_connected():
        return groups, []

    from telethon.tl.functions.channels import GetParticipantRequest

    active, inactive = [], []
    for g in groups:
        try:
            await _telethon_client(GetParticipantRequest(g.chat_id, "me"))
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
        "• /status — current config",
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
        link = f"https://t.me/{g.username}" if g.username else "—"
        entries.append(
            f"{i}. <b>{_html.escape(g.title)}</b>\n"
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
            if (_telethon_client and _telethon_client.is_connected())
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

    await state.clear()
    added = db.add_source_group(chat_id=chat_id, title=title, username=username)

    if added:
        link_line = f"\n🔗 https://t.me/{username}" if username else ""
        await message.answer(
            f"✅ <b>Source group added!</b>\n\n"
            f"🏢 <b>{_html.escape(title)}</b>\n"
            f"🆔 <code>{chat_id}</code>{link_line}",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
        # ── Feature 6: membership check ───────────────────────────
        is_member, warning = await _check_membership(chat_id)
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
        "⚠️ The monitor account must be a member with send permission.\n\n"
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


# /testkeyword ── Feature 5 ───────────────────────────────────────

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
        confidence_bar = "█" * int(result.confidence * 10) + "░" * (10 - int(result.confidence * 10))
        await message.answer(
            f"✅ <b>DETECTED as job post</b>\n\n"
            f"{flag} Language: <b>{(result.matched_lang or 'unknown').capitalize()}</b>\n"
            f"🎯 Match type: <b>{tier_label}</b>\n"
            f"🔑 Keywords: <code>{kw_safe}</code>\n"
            f"📊 Confidence: {confidence_bar} {int(result.confidence * 100)}%",
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


# /stats ── Feature 7 ─────────────────────────────────────────────

@router.message(Command("stats"))
@router.message(F.text == "📈 Stats")
async def cmd_stats(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    s = db.get_stats_summary(days=7)
    lang_flags = {"uzbek": "🇺🇿", "russian": "🇷🇺", "korean": "🇰🇷"}

    lines = [f"📈 <b>Statistics (last 7 days)</b>\n"]
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
        lines.append("")

    if s["by_tier"]:
        lines.append("🔍 <b>By detection method:</b>")
        tier_labels = {1: "Strong keyword", 2: "Word combination"}
        for tier, cnt in sorted(s["by_tier"].items()):
            lines.append(f"  • {tier_labels.get(tier, f'Tier {tier}')}: <b>{cnt}</b>")

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
    if _telethon_client is None or not _telethon_client.is_connected():
        await message.answer(
            "⚠️ Telethon not connected. Run <code>python main.py</code>.",
            parse_mode="HTML", reply_markup=_main_keyboard(),
        )
        return
    target = target_cfg["chat_id"]
    await message.answer(
        f"⏳ Sending test to <b>{_html.escape(target_cfg['title'])}</b> …",
        parse_mode="HTML",
    )
    try:
        await _telethon_client.send_message(
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

    groups    = db.list_source_groups()
    target    = db.get_target_group()
    processed = db.get_processed_count()

    monitor_status = (
        "🟢 Connected"
        if (_telethon_client and _telethon_client.is_connected())
        else "🔴 Not connected — run python main.py"
    )
    target_str = (
        f"<b>{_html.escape(target['title'])}</b> (<code>{target['chat_id']}</code>)"
        if target else "❌ Not set — use 🎯 Set Target"
    )
    group_list = ""
    if groups:
        group_list = "\n" + "\n".join(
            f"  • <b>{_html.escape(g.title)}</b> (<code>{g.chat_id}</code>)"
            for g in groups
        )

    await message.answer(
        f"📊 <b>Bot Status</b>\n\n"
        f"🤖 Monitor: {monitor_status}\n"
        f"👁 Source groups: <b>{len(groups)}</b>{group_list}\n\n"
        f"🎯 Target group: {target_str}\n"
        f"📨 Total forwarded: <b>{processed}</b>",
        parse_mode="HTML",
        reply_markup=_main_keyboard(),
    )


# /checkgroups ───────────────────────────────────────────────────

@router.message(Command("checkgroups"))
@router.message(F.text == "🔎 Check Groups")
async def cmd_check_groups(message: Message, state: FSMContext):
    if not await _is_admin(message):
        return
    await state.clear()

    groups = db.list_source_groups()
    if not groups:
        await message.answer(
            "ℹ️ No source groups registered yet. Use ➕ Add Group.",
            reply_markup=_main_keyboard(),
        )
        return

    if _telethon_client is None or not _telethon_client.is_connected():
        await message.answer(
            "⚠️ Telethon not connected.\n"
            "Run <code>python main.py</code> to enable group status checking.",
            parse_mode="HTML",
            reply_markup=_main_keyboard(),
        )
        return

    # Send a placeholder — checking 40+ groups takes a few seconds
    wait_msg = await message.answer(
        f"⏳ Checking membership for <b>{len(groups)}</b> groups. "
        f"This may take a few seconds...",
        parse_mode="HTML",
    )

    active, inactive = await _check_all_memberships(groups)

    lines = ["🔎 <b>Group Status Check</b>\n"]
    lines.append(
        f"✅ Subscribed: <b>{len(active)}</b>   "
        f"❌ Not subscribed: <b>{len(inactive)}</b>\n"
    )

    if inactive:
        lines.append("❌ <b>NOT subscribed (bot receives NO messages from these):</b>")
        for g in inactive:
            link = f"https://t.me/{g.username}" if g.username else f"ID: <code>{g.chat_id}</code>"
            lines.append(f"  • <b>{_html.escape(g.title)}</b> — {link}")
        lines.append(
            "\n⚠️ Join these groups with your <b>monitor account</b> "
            "to start receiving their messages."
        )
        lines.append("")

    if active:
        lines.append("✅ <b>Subscribed and active:</b>")
        for g in active:
            lines.append(f"  • {_html.escape(g.title)}")

    try:
        await wait_msg.delete()
    except Exception:
        pass

    # Paginate if the list is very long
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

    # ── Feature 10: register bot with notifier ────────────────────
    notifier_set_bot(bot)

    dp  = Dispatcher(storage=MemoryStorage())
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