#!/usr/bin/env python3
"""
main.py – Unified entry point (dual-account).

Startup order:
  1. Database init (+ migration)
  2. Account 1 Telethon client (required)
  3. Account 2 Telethon client (if ACCOUNT_2_ENABLED)
  4. Monitor event handlers for all active clients
  5. Admin bot background task
  6. Daily summary scheduler background task
  7. asyncio.gather(*[c.run_until_disconnected() for c in all_clients])
     — keeps all connections alive concurrently
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
from bot.client import client_1, client_2, all_clients
from bot.config import PHONE_NUMBER, PHONE_NUMBER_2, ACCOUNT_2_ENABLED
from bot.monitor import setup_monitor
from bot.admin_bot import run_admin_bot, set_telethon_clients
from bot.scheduler import run_daily_summary_scheduler


async def main() -> None:
    logger.info("=" * 60)
    logger.info("  Korea Ish E'lonlari — Starting")
    logger.info("  Accounts active: %d", len(all_clients))
    logger.info("=" * 60)

    db.init_db()

    # setup_monitor() starts all clients internally
    await setup_monitor()

    # Pass both clients to the admin bot
    set_telethon_clients(client_1, client_2)

    logger.info("[main] Starting admin bot and scheduler …")
    admin_task     = asyncio.create_task(run_admin_bot(),               name="admin_bot")
    scheduler_task = asyncio.create_task(run_daily_summary_scheduler(), name="scheduler")

    logger.info("[main] ✅ All systems running. Press Ctrl+C to stop.")
    try:
        # Keep ALL Telethon connections alive concurrently
        await asyncio.gather(*[c.run_until_disconnected() for c in all_clients])
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