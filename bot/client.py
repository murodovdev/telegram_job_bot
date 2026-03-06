"""
client.py – Shared Telethon TelegramClient singleton.

Automatically selects session type:
  • SESSION_STRING set  → StringSession (Railway / cloud, no file needed)
  • SESSION_STRING empty → FileSession  (local development)
"""

from telethon import TelegramClient
from telethon.sessions import StringSession

from bot.config import API_ID, API_HASH, SESSION_STRING, SESSION_NAME

if SESSION_STRING:
    # Cloud / Railway mode — session lives in memory, loaded from env var
    _session = StringSession(SESSION_STRING)
else:
    # Local development mode — session stored in a .session file
    _session = SESSION_NAME

telethon_client = TelegramClient(_session, API_ID, API_HASH)