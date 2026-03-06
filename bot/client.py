"""
client.py – Shared Telethon TelegramClient singleton.

Both monitor.py and admin_bot.py import this single client object so they
can share one authenticated session when running in the same process.
The client is connected in main.py before either component starts.
"""

from telethon import TelegramClient
from bot.config import API_ID, API_HASH, SESSION_NAME

# Single module-level client — import this everywhere instead of creating new ones.
telethon_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)