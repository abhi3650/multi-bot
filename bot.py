#!/usr/bin/env python3
"""
Multipurpose Telegram Bot — pure Pyrogram
Commands: /start /help /imdb /ott /posters
          /mediainfo /sample /screenshot /link /yt /song
          /usage /premium
          /restart /addpremium /removepremium /pending /stats /broadcast (admin)
"""

import asyncio
import logging

import httpx
from pyrogram import Client

import database as db
import pyrogram_helper as pyro
from config import (
    BOT_TOKEN, API_ID, API_HASH,
    STREAM_BASE_URL, STREAM_PORT,
)

# Import handler registration functions
from handlers import user, admin, movie, media, ytdl, song, link

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Bot command definitions ───────────────────────────────────────────────────
# Edit this list whenever you add / rename / remove a command.
# The bot will automatically sync these with Telegram on every startup.

# Commands shown to every user
USER_COMMANDS = [
    ("start",      "Start the bot and see the main menu"),
    ("help",       "Full command reference"),
    ("imdb",       "Movie / series info from TMDB + IMDB"),
    ("ott",        "Check OTT streaming availability"),
    ("posters",    "Browse movie posters"),
    ("mediainfo",  "Show codec, resolution and bitrate of a video"),
    ("sample",     "Generate a 30-second preview clip from a video"),
    ("screenshot", "Take 10 screenshots from a video"),
    ("link",       "Generate Watch Online + Download link for a file"),
    ("yt",         "YouTube quality picker + direct download link"),
    ("song",       "Search YouTube Music and download as MP3"),
    ("usage",      "View your monthly usage stats"),
    ("premium",    "Upgrade to Premium for unlimited access"),
]

# Extra commands shown only in the admin panel (BotFather → Edit Bot → Edit Commands)
ADMIN_COMMANDS = USER_COMMANDS + [
    ("addpremium",    "Grant premium to a user — /addpremium <uid> [days]"),
    ("removepremium", "Revoke premium from a user — /removepremium <uid>"),
    ("pending",       "List pending payment verifications"),
    ("stats",         "Bot usage statistics"),
    ("broadcast",     "Send a message to all users — /broadcast <text>"),
    ("restart",       "Restart the bot process"),
]


async def _sync_commands(bot_token: str, admin_ids: list[int]) -> None:
    """
    Delete all existing bot commands then register the fresh set.
    Uses the Bot API directly (simple HTTP, no extra dependency).

    Scopes registered:
      • default          — user-facing commands (seen by everyone)
      • chat (per admin) — full command list including admin-only commands
    """
    base = f"https://api.telegram.org/bot{bot_token}"

    def _cmd_payload(pairs: list[tuple[str, str]]) -> list[dict]:
        return [{"command": cmd, "description": desc} for cmd, desc in pairs]

    async with httpx.AsyncClient(timeout=15) as hx:

        # 1. Wipe ALL existing command scopes first
        await hx.post(f"{base}/deleteMyCommands", json={})                          # default scope
        await hx.post(f"{base}/deleteMyCommands", json={"scope": {"type": "all_private_chats"}})
        await hx.post(f"{base}/deleteMyCommands", json={"scope": {"type": "all_group_chats"}})
        await hx.post(f"{base}/deleteMyCommands", json={"scope": {"type": "all_chat_administrators"}})
        for uid in admin_ids:
            await hx.post(f"{base}/deleteMyCommands",
                          json={"scope": {"type": "chat", "chat_id": uid}})
        logger.info("Cleared all existing bot commands.")

        # 2. Set user-facing commands (default scope — visible to everyone)
        r = await hx.post(f"{base}/setMyCommands",
                          json={"commands": _cmd_payload(USER_COMMANDS)})
        if r.json().get("result"):
            logger.info("Registered %d user commands (default scope).", len(USER_COMMANDS))
        else:
            logger.warning("Failed to set default commands: %s", r.text)

        # 3. Set full command list for each admin's private chat
        for uid in admin_ids:
            r = await hx.post(
                f"{base}/setMyCommands",
                json={
                    "commands": _cmd_payload(ADMIN_COMMANDS),
                    "scope":    {"type": "chat", "chat_id": uid},
                },
            )
            if r.json().get("result"):
                logger.info("Registered %d admin commands for user %d.", len(ADMIN_COMMANDS), uid)
            else:
                logger.warning("Failed to set admin commands for %d: %s", uid, r.text)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("❌  Set BOT_TOKEN in your .env file first!")
    if not API_ID or not API_HASH:
        logger.warning(
            "API_ID / API_HASH not set — large file support disabled.\n"
            "Get them from https://my.telegram.org/apps and add to .env"
        )

    # ── Initialise database ────────────────────────────────────────────────────
    await db.init_db()
    logger.info("Database initialised.")

    # ── Start shared Pyrogram client (for MTProto file streaming) ──────────────
    await pyro.start(API_ID, API_HASH, BOT_TOKEN)

    # ── Start aiohttp streaming server (/link command) ────────────────────────
    if STREAM_BASE_URL:
        from stream_server import start_stream_server
        await start_stream_server(STREAM_PORT)
        logger.info("Stream server → %s (port %d)", STREAM_BASE_URL, STREAM_PORT)
    else:
        logger.info("STREAM_BASE_URL not set — /link will use raw CDN URLs only.")

    # ── Build the Pyrogram bot client ─────────────────────────────────────────
    app = Client(
        name       = "multipurpose_bot_main",
        api_id     = API_ID or 1,         # Pyrogram needs a non-zero int; 1 is safe for bot-only
        api_hash   = API_HASH or "a",     # same — will be unused if API_ID is 0
        bot_token  = BOT_TOKEN,
        in_memory  = True,
    )

    # ── Register all handlers ─────────────────────────────────────────────────
    user.register(app)
    admin.register(app)
    movie.register(app)
    media.register(app)
    ytdl.register(app)
    song.register(app)
    link.register(app)

    logger.info("Bot is starting…")

    # ── Run ────────────────────────────────────────────────────────────────────────────────────
    async with app:
        me = await app.get_me()
        logger.info("Bot running as @%s", me.username)

        # Sync bot commands on every startup: wipe old, register fresh
        from config import ADMIN_IDS
        await _sync_commands(BOT_TOKEN, ADMIN_IDS)
        logger.info("Bot commands synced ✅  —  Press Ctrl+C to stop.")

        await asyncio.Event().wait()   # run forever until Ctrl+C


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        # Stop streaming server and Pyrogram client on exit
        async def _cleanup():
            if STREAM_BASE_URL:
                from stream_server import stop_stream_server
                await stop_stream_server()
            await pyro.stop()
        asyncio.run(_cleanup())
