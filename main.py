#!/usr/bin/env python3
"""
main.py – Unified entry point.

Starts in order:
  1. Database init
  2. Telethon client (personal account)
  3. Monitor event handlers
  4. Admin bot (background task)
  5. Daily summary scheduler at 21:00 KST (background task)
"""

import asyncio
import logging
import os
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

for d in ("logs", "data", "sessions"):
    pathlib.Path(d).mkdir(exist_ok=True)

from bot.utils import setup_logging
logger = setup_logging("main")

import bot.database as db
from bot.client import telethon_client
from bot.config import PHONE_NUMBER
from bot.monitor import setup_monitor
from bot.admin_bot import run_admin_bot, set_telethon_client
from bot.scheduler import run_daily_summary_scheduler


async def main() -> None:
    logger.info("=" * 60)
    logger.info("  Korea Ish E'lonlari — Starting")
    logger.info("=" * 60)

    db.init_db()

    logger.info("[main] Connecting Telethon …")
    await telethon_client.start(phone=PHONE_NUMBER)
    me = await telethon_client.get_me()
    logger.info("[main] Logged in as: %s (id=%s)", me.first_name, me.id)

    set_telethon_client(telethon_client)
    await setup_monitor()

    logger.info("[main] Starting admin bot …")
    admin_task     = asyncio.create_task(run_admin_bot(),                  name="admin_bot")
    scheduler_task = asyncio.create_task(run_daily_summary_scheduler(),    name="scheduler")

    logger.info("[main] ✅ All systems running. Press Ctrl+C to stop.")
    try:
        await telethon_client.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("[main] Shutting down …")
        for task in (admin_task, scheduler_task):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("[main] Stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[main] Stopped by user.")
        sys.exit(0)