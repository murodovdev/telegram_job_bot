#!/usr/bin/env python3
"""
main_admin.py – Admin-only entry point (DEGRADED mode).

Usage
-----
    python main_admin.py

⚠️  IMPORTANT — this runs the admin bot WITHOUT the Telethon monitor.
Telethon clients are never connected here, so any feature that needs a
user account is unavailable in this mode:

    • 🧪 Test Send          • 🔎 Check Groups
    • 📊 Status (shows "not connected")
    • Group/@username resolution in ➕ Add Group / 🎯 Set Target
    • ✅ Tasdiqlash (review approve → forward to target)

DB-only features still work (List/Remove Groups, Stats, Settings,
Blocked Users, Import, toggles).

👉 For the full bot (monitor + admin + scheduler in one process) run:

    python main.py

Use this file only for quick DB-only admin tasks.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import asyncio

from bot.utils import setup_logging

pathlib.Path("logs").mkdir(exist_ok=True)
logger = setup_logging("admin_bot")

from bot.admin_bot import run_admin_bot


if __name__ == "__main__":
    logger.info("=" * 55)
    logger.info("  Ish E'lonlari  –  Admin Control Panel (ADMIN-ONLY)")
    logger.info("=" * 55)
    logger.warning(
        "Running in ADMIN-ONLY mode — Telethon monitor is NOT started. "
        "Account-dependent features (Test Send, Check Groups, group "
        "resolution, review approve) will be unavailable. "
        "Run 'python main.py' for the full bot."
    )

    try:
        asyncio.run(run_admin_bot())
    except KeyboardInterrupt:
        logger.info("Admin bot stopped.")
        sys.exit(0)
