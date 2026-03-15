"""
client.py – Telethon client instances.

Exports
-------
client_1        Primary account (always present)
client_2        Fallback account (None if ACCOUNT_2_ENABLED is False)
all_clients     List of all active clients — [client_1] or [client_1, client_2]
telethon_client Alias for client_1 (backward compatibility)
"""

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.network import ConnectionTcpObfuscated

from bot.config import (
    API_ID, API_HASH,
    SESSION_STRING,  SESSION_NAME,
    SESSION_STRING_2, SESSION_NAME_2,
    ACCOUNT_2_ENABLED,
)


def _make_client(session) -> TelegramClient:
    return TelegramClient(
        session,
        API_ID,
        API_HASH,
        connection=ConnectionTcpObfuscated,
        connection_retries=10,
        retry_delay=5,
        timeout=30,
        auto_reconnect=True,
    )


# ── Account 1 (primary) ───────────────────────────────────────────
_session_1 = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_NAME
client_1   = _make_client(_session_1)

# ── Account 2 (optional fallback) ────────────────────────────────
client_2 = None
if ACCOUNT_2_ENABLED:
    _session_2 = StringSession(SESSION_STRING_2) if SESSION_STRING_2 else SESSION_NAME_2
    client_2   = _make_client(_session_2)

# ── Convenience exports ───────────────────────────────────────────
telethon_client = client_1                                 # backward-compat alias
all_clients     = [c for c in (client_1, client_2) if c]  # always a flat list