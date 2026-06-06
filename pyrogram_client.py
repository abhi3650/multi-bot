"""
pyrogram_client.py

Manages a single Pyrogram client that runs alongside python-telegram-bot.
Pyrogram uses the MTProto protocol (API_ID + API_HASH) which allows
accessing Telegram files of ANY size — no 20 MB Bot API limit.

The client is only started when API_ID and API_HASH are configured.
If they are missing, all Pyrogram features are silently disabled and
the bot falls back to the CDN-proxy approach (≤ 20 MB files only).
"""

import logging
from typing import Optional

from pyrogram import Client

logger = logging.getLogger(__name__)

_client: Optional[Client] = None
_streamer = None           # ByteStreamer instance, created lazily


def is_available() -> bool:
    """Return True if the Pyrogram client is running."""
    return _client is not None


def get_client() -> Optional[Client]:
    return _client


def get_streamer():
    """Return the ByteStreamer singleton (created on first call)."""
    global _streamer
    if _client is None:
        return None
    if _streamer is None:
        from utils.byte_streamer import ByteStreamer
        _streamer = ByteStreamer(_client)
    return _streamer


async def start(api_id: int, api_hash: str, bot_token: str):
    """Start the Pyrogram client. Call once on bot startup."""
    global _client
    if not (api_id and api_hash and bot_token):
        logger.info(
            "API_ID / API_HASH not set — Pyrogram disabled. "
            "/link will only work for files ≤ 20 MB."
        )
        return

    try:
        client = Client(
            name      = "multipurpose_bot_stream",
            api_id    = api_id,
            api_hash  = api_hash,
            bot_token = bot_token,
            no_updates = True,    # we only need file access, not updates
            in_memory  = True,    # no session file written to disk
        )
        await client.start()
        client.media_sessions = {}   # ByteStreamer uses this dict for DC sessions
        _client = client
        me = await client.get_me()
        logger.info("Pyrogram client started as @%s — unlimited file streaming enabled ✅", me.username)
    except Exception as exc:
        logger.warning("Pyrogram client failed to start: %s — falling back to CDN proxy.", exc)


async def stop():
    """Stop the Pyrogram client. Call on bot shutdown."""
    global _client, _streamer
    if _client:
        await _client.stop()
        _client   = None
        _streamer = None
        logger.info("Pyrogram client stopped.")
