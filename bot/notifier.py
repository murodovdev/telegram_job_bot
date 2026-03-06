"""
notifier.py – Admin error notification system.

Sends a Telegram DM to the admin (ADMIN_USER_ID) when something goes wrong.
Uses the aiogram Bot instance (light, no Telethon needed for DMs).

Usage
-----
    from bot.notifier import notify_admin, set_bot
    set_bot(bot_instance)           # called once in run_admin_bot()
    await notify_admin("⚠️ error")  # called anywhere
"""

import logging
from typing import Optional
from aiogram import Bot

from bot.config import ADMIN_USER_ID

logger = logging.getLogger(__name__)

_bot: Optional[Bot] = None


def set_bot(bot: Bot) -> None:
    """Register the aiogram Bot instance. Call once at startup."""
    global _bot
    _bot = bot


async def notify_admin(text: str) -> None:
    """
    Send a plain-text DM to the admin user.
    Silently logs and returns if the bot is not yet initialised.
    Never raises — notification failure must not crash the caller.
    """
    if _bot is None:
        logger.warning("[notifier] Bot not set — cannot send admin notification.")
        return
    try:
        await _bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=text,
            parse_mode="HTML",
        )
    except Exception as exc:
        # Never let notification failure propagate — just log it
        logger.error("[notifier] Failed to send admin notification: %s", exc)