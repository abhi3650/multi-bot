#!/usr/bin/env python3
"""
Multipurpose Telegram Bot — Pyrogram Edition
All file operations use MTProto (API_ID + API_HASH) — no 20 MB limit.
"""

import asyncio
import logging

from pyrogram import Client, idle

import database as db
from config import API_ID, API_HASH, BOT_TOKEN, STREAM_PORT, STREAM_BASE_URL
from stream_server import start_stream_server, stop_stream_server

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

app = Client(
    "multipurpose_bot",
    api_id    = API_ID,
    api_hash  = API_HASH,
    bot_token = BOT_TOKEN,
    plugins   = dict(root="handlers"),   # auto-loads all files in handlers/
)


async def main():
    await db.init_db()
    log.info("Database initialised.")

    await app.start()
    me = await app.get_me()
    log.info("Bot started as @%s", me.username)

    # Stream server always starts on localhost so ffmpeg can access ANY Telegram file
    await start_stream_server(STREAM_PORT, app)
    if STREAM_BASE_URL:
        log.info("Public stream server: %s", STREAM_BASE_URL)
    else:
        log.info("STREAM_BASE_URL not set — /link generates localhost URLs only.")

    log.info("Ready! Press Ctrl+C to stop.")
    await idle()

    await stop_stream_server()
    await app.stop()
    log.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
