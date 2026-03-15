"""
client.py – Telethon client instances.

Exports
-------
client_1        Primary account (always present)
client_2        Fallback account (None if ACCOUNT_2_ENABLED is False)
all_clients     List of all active clients — [client_1] or [client_1, client_2]
telethon_client Alias for client_1 (backward compatibility)

MTProxy support
---------------
If PROXY_HOST and PROXY_SECRET are set in environment variables, all clients
will connect through the MTProxy. This is required on Railway where Telegram
ports are sometimes blocked depending on the region.

Set these in Railway Variables:
    PROXY_HOST   = proxy server hostname or IP
    PROXY_PORT   = 443  (default)
    PROXY_SECRET = the secret string starting with 'ee...'
"""

import os

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

from bot.config import (
    API_ID, API_HASH,
    SESSION_STRING,  SESSION_NAME,
    SESSION_STRING_2, SESSION_NAME_2,
    ACCOUNT_2_ENABLED,
)

# ── MTProxy configuration (optional) ─────────────────────────────
# If PROXY_HOST and PROXY_SECRET are set, all clients use the proxy.
# If not set, clients connect directly (no proxy).

_proxy_host   = os.getenv("PROXY_HOST", "").strip()
_proxy_port   = int(os.getenv("PROXY_PORT", "443"))
_proxy_secret = os.getenv("PROXY_SECRET", "").strip()

_USE_PROXY = bool(_proxy_host and _proxy_secret)


def _make_client(session) -> TelegramClient:
    """Create a TelegramClient with or without MTProxy."""
    if _USE_PROXY:
        return TelegramClient(
            session,
            API_ID,
            API_HASH,
            connection=ConnectionTcpMTProxyRandomizedIntermediate,
            proxy=(_proxy_host, _proxy_port, _proxy_secret),
            connection_retries=10,
            retry_delay=5,
            timeout=30,
            auto_reconnect=True,
        )
    return TelegramClient(
        session,
        API_ID,
        API_HASH,
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