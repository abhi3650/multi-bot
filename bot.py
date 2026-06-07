#!/usr/bin/env python3
"""
Multipurpose Telegram Bot — pure Pyrogram
"""

import asyncio
import logging
import signal

import httpx
from pyrogram import Client

import database as db
import pyrogram_helper as pyro
from config import BOT_TOKEN, API_ID, API_HASH, STREAM_BASE_URL, STREAM_PORT, ADMIN_IDS
from handlers import user, admin, movie, media, ytdl, song, link

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Bot commands ──────────────────────────────────────────────────────────────

USER_COMMANDS = [
    ("start",      "Start the bot"),
    ("help",       "Full command reference"),
    ("imdb",       "Movie / series info"),
    ("ott",        "OTT streaming availability"),
    ("posters",    "Browse movie posters"),
    ("mediainfo",  "Media codec and bitrate info"),
    ("sample",     "Generate a 30-second preview clip"),
    ("screenshot", "Take 10 screenshots from a video"),
    ("link",       "Generate Watch + Download link"),
    ("yt",         "YouTube quality picker + download link"),
    ("song",       "Search YouTube Music and get MP3"),
    ("usage",      "Your monthly usage stats"),
    ("premium",    "Upgrade to Premium"),
]

ADMIN_COMMANDS = USER_COMMANDS + [
    ("addpremium",    "Grant premium  — /addpremium <uid> [days]"),
    ("removepremium", "Revoke premium — /removepremium <uid>"),
    ("pending",       "List pending payment verifications"),
    ("stats",         "Bot statistics"),
    ("broadcast",     "Message all users — /broadcast <text>"),
    ("restart",       "Restart the bot"),
]


async def _sync_commands() -> None:
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    def _payload(pairs):
        return [{"command": c, "description": d} for c, d in pairs]

    async with httpx.AsyncClient(timeout=15) as hx:
        # Wipe all scopes
        for scope in [
            {},
            {"scope": {"type": "all_private_chats"}},
            {"scope": {"type": "all_group_chats"}},
            {"scope": {"type": "all_chat_administrators"}},
            *[{"scope": {"type": "chat", "chat_id": uid}} for uid in ADMIN_IDS],
        ]:
            await hx.post(f"{base}/deleteMyCommands", json=scope)
        logger.info("Cleared all existing bot commands.")

        # Set user commands (default scope)
        r = await hx.post(f"{base}/setMyCommands", json={"commands": _payload(USER_COMMANDS)})
        if r.json().get("result"):
            logger.info("Registered %d user commands.", len(USER_COMMANDS))

        # Set admin commands per admin chat
        for uid in ADMIN_IDS:
            r = await hx.post(f"{base}/setMyCommands", json={
                "commands": _payload(ADMIN_COMMANDS),
                "scope":    {"type": "chat", "chat_id": uid},
            })
            if r.json().get("result"):
                logger.info("Registered %d admin commands for %d.", len(ADMIN_COMMANDS), uid)

        logger.info("Bot commands synced ✅")


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ Set BOT_TOKEN in your .env file!")

    # Database
    await db.init_db()
    logger.info("Database initialised.")

    # Pyrogram MTProto helper (large file streaming)
    await pyro.start(API_ID, API_HASH, BOT_TOKEN)

    # Streaming server for /link
    if STREAM_BASE_URL:
        from stream_server import start_stream_server
        await start_stream_server(STREAM_PORT)
        logger.info("Stream server → %s (port %d)", STREAM_BASE_URL, STREAM_PORT)
    else:
        logger.info("STREAM_BASE_URL not set — /link uses raw CDN URLs.")

    # Build bot client
    app = Client(
        name      = "multipurpose_bot",
        api_id    = API_ID  or 1,
        api_hash  = API_HASH or "a",
        bot_token = BOT_TOKEN,
        in_memory = True,
    )

    # Register all handlers
    user.register(app)
    admin.register(app)
    movie.register(app)
    media.register(app)
    ytdl.register(app)
    song.register(app)
    link.register(app)

    # Graceful shutdown event
    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("Stop signal received. Shutting down…")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, _handle_signal)

    logger.info("Bot is starting…")

    async with app:
        me = await app.get_me()
        logger.info("Bot started as @%s", me.username)

        await _sync_commands()

        logger.info("Ready! Press Ctrl+C to stop.")
        await stop_event.wait()   # block until signal

    # Cleanup
    if STREAM_BASE_URL:
        try:
            from stream_server import stop_stream_server
            await stop_stream_server()
        except Exception:
            pass
    await pyro.stop()
    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
