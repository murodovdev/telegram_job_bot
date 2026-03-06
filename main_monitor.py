#!/usr/bin/env python3
"""
main_monitor.py – Entry point for the Telethon userbot monitor.

Usage
-----
    python main_monitor.py

This script simply delegates to bot.monitor so that the project root
can be used as the working directory without needing -m flags.
"""

import pathlib
import sys

# Ensure project root is on sys.path when run directly
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import asyncio
import signal
import logging

from bot.utils import setup_logging

# Set up logging before any other imports that might log
pathlib.Path("logs").mkdir(exist_ok=True)
logger = setup_logging("monitor")

from bot.monitor import start_monitor


def _handle_signal(sig, frame):
    logger.info("Signal %s received – requesting shutdown …", sig)
    loop = asyncio.get_event_loop()
    for task in asyncio.all_tasks(loop):
        task.cancel()


if __name__ == "__main__":
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _handle_signal)

    logger.info("=" * 55)
    logger.info("  Ish E'lonlari  –  Job Aggregator Monitor")
    logger.info("=" * 55)

    try:
        asyncio.run(start_monitor())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Monitor stopped.")
        sys.exit(0)
