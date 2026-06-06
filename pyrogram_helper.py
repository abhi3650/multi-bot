"""
pyrogram_helper.py

Single shared Pyrogram Client that runs alongside the bot.
Used by /link (ByteStreamer) and media handlers (large file download via MTProto).

If API_ID / API_HASH are missing the client stays None and everything
falls back gracefully to the 20 MB Bot API path.
"""

import logging
from typing import Optional

from pyrogram import Client

logger = logging.getLogger(__name__)

_client:   Optional[Client] = None
_streamer = None   # ByteStreamer, created lazily on first /link call


def is_available() -> bool:
    return _client is not None


def get_client() -> Optional[Client]:
    return _client


def get_streamer():
    global _streamer
    if _client is None:
        return None
    if _streamer is None:
        from utils.byte_streamer import ByteStreamer
        _streamer = ByteStreamer(_client)
    return _streamer


async def start(api_id: int, api_hash: str, bot_token: str):
    global _client
    if not (api_id and api_hash and bot_token):
        logger.info("API_ID/API_HASH not set — Pyrogram disabled. /link limited to 20 MB files.")
        return
    try:
        client = Client(
            name       = "multipurpose_bot",
            api_id     = api_id,
            api_hash   = api_hash,
            bot_token  = bot_token,
            no_updates = True,   # we only need file access, not update handling
            in_memory  = True,   # no session file on disk
        )
        await client.start()
        client.media_sessions = {}
        _client = client
        me = await client.get_me()
        logger.info("Pyrogram started as @%s — unlimited file streaming enabled ✅", me.username)
    except Exception as exc:
        logger.warning("Pyrogram failed to start: %s — falling back to CDN proxy.", exc)


async def stop():
    global _client, _streamer
    if _client:
        await _client.stop()
        _client   = None
        _streamer = None
        logger.info("Pyrogram client stopped.")
