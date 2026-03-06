#!/usr/bin/env python3
"""
main_admin.py – Entry point for the aiogram Admin Control Panel Bot.

Usage
-----
    python main_admin.py

Run this in a separate terminal alongside main_monitor.py.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import asyncio
import logging

from bot.utils import setup_logging

pathlib.Path("logs").mkdir(exist_ok=True)
logger = setup_logging("admin_bot")

from bot.admin_bot import run_admin_bot


if __name__ == "__main__":
    logger.info("=" * 55)
    logger.info("  Ish E'lonlari  –  Admin Control Panel Bot")
    logger.info("=" * 55)

    try:
        asyncio.run(run_admin_bot())
    except KeyboardInterrupt:
        logger.info("Admin bot stopped.")
        sys.exit(0)
