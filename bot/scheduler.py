"""
scheduler.py – Daily summary scheduler.

Sends a summary message to the admin every day at 21:00 KST (Korea Standard
Time = UTC+9).  Runs as a background asyncio task inside main.py.

The summary is sent as a DM to ADMIN_USER_ID via the aiogram bot.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import bot.database as db
from bot.config import DEDUP_WINDOW_HOURS
from bot.notifier import notify_admin

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
SUMMARY_HOUR_KST = 21   # 9 PM


async def _send_daily_summary() -> None:
    """Build and send the evening summary DM to the admin."""
    summary = db.get_daily_summary()
    now_kst = datetime.now(KST).strftime("%Y-%m-%d")

    total = summary["total"]
    if total == 0:
        text = (
            f"📊 <b>Daily Summary — {now_kst}</b>\n\n"
            f"No job posts were forwarded today."
        )
    else:
        lines = [f"📊 <b>Daily Summary — {now_kst}</b>\n"]
        lines.append(f"📨 Total forwarded today: <b>{total}</b>\n")

        if summary["by_group"]:
            lines.append("🏆 <b>Top source groups:</b>")
            for i, (title, cnt) in enumerate(summary["by_group"], 1):
                lines.append(f"  {i}. {title} — <b>{cnt}</b> posts")
            lines.append("")

        if summary["by_lang"]:
            lang_flags = {"uzbek": "🇺🇿", "russian": "🇷🇺", "korean": "🇰🇷"}
            lines.append("🌐 <b>By language:</b>")
            for lang, cnt in summary["by_lang"].items():
                flag = lang_flags.get(lang, "🌐")
                lines.append(f"  {flag} {lang.capitalize()}: <b>{cnt}</b>")

        text = "\n".join(lines)

    await notify_admin(text)
    logger.info("[scheduler] Daily summary sent.")


async def run_daily_summary_scheduler() -> None:
    """
    Infinite loop that sleeps until 21:00 KST, sends the summary,
    then sleeps until the next day's 21:00 KST.

    Designed to run as an asyncio background task.
    """
    logger.info("[scheduler] Daily summary scheduler started (fires at 21:00 KST).")

    while True:
        now = datetime.now(KST)
        # Build next trigger time: today at 21:00 KST
        target = now.replace(hour=SUMMARY_HOUR_KST, minute=0, second=0, microsecond=0)

        # If we've already passed 21:00 today, schedule for tomorrow
        if now >= target:
            target = target + timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        logger.info(
            "[scheduler] Next daily summary in %.0f seconds (at %s KST).",
            wait_seconds,
            target.strftime("%Y-%m-%d %H:%M"),
        )

        await asyncio.sleep(wait_seconds)

        try:
            await _send_daily_summary()
            # Nightly maintenance: prune all tables that would grow forever
            db.cleanup_content_hashes(DEDUP_WINDOW_HOURS)
            db.cleanup_processed_msgs(keep_days=30)
            db.cleanup_original_msg_index(keep_days=7)
            db.cleanup_review_queue(keep_hours=48)
            db.cleanup_forwarded_msgs(keep_hours=48)
        except Exception as exc:
            logger.error("[scheduler] Error sending daily summary: %s", exc)

        # Small sleep to avoid double-firing if we wake up exactly on time
        await asyncio.sleep(60)