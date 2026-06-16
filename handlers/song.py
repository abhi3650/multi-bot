"""
handlers/song.py  —  /song command

Search:   YouTube Music InnerTube API (direct, no yt-dlp)
Download: pytubefix (handles cipher/auth without cookies)
Convert:  ffmpeg  (MP3 192kbps)
Art:      mutagen ID3 APIC

Flow:
  1. /song <query>  →  "Search Results (YT Music):" + 8 buttons
  2. Tap            →  "Preparing..." → download → "Uploading..."
  3. Send MP3 with caption:
       🎵 Song   : Title
       🎤 Artist  : Artist
       📀 Source  : YT Music
"""

import asyncio
import io
import os
import tempfile

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
import ytmusic as ym
from logger import log_action

MD = ParseMode.MARKDOWN

# In-memory session: { session_key: { vid_id: entry } }
_sessions: dict[str, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


def _key(seed: str) -> str:
    import hashlib
    return hashlib.md5(seed.encode()).hexdigest()[:10]


def _fmt_dur(dur_str: str) -> str:
    return dur_str if dur_str else "?:??"


# ── Handler registration ──────────────────────────────────────────────────────

def register(app: Client):

    @app.on_message(filters.command("song") & filters.private)
    async def cmd_song(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        args = message.command[1:]
        if not args:
            await message.reply(
                "🎵 **Song Search (YouTube Music)**\n\n"
                "**Usage:** `/song <song name>`\n"
                "**Example:** `/song Blinding Lights`\n\n"
                "_Searches music.youtube.com — tap a result to get the MP3._",
                parse_mode=MD,
            )
            return

        query = " ".join(args)
        wait  = await message.reply("🔍 Searching YouTube Music…")

        try:
            entries = await ym.search(query, n=8)
        except Exception as e:
            await wait.edit(f"❌ Search failed:\n`{e}`", parse_mode=MD)
            return

        if not entries:
            await wait.edit("❌ No results found. Try a different search term.")
            return

        sk = _key(f"{u.id}{query}")
        _sessions[sk] = {e["id"]: e for e in entries}

        # One button per result (full title visible)
        buttons = [
            [InlineKeyboardButton(
                f"{(entry.get('title') or 'Unknown')[:50]}  {entry.get('duration','')}",
                callback_data=f"song|{sk}|{entry['id']}",
            )]
            for entry in entries
        ]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="song_cancel")])

        await wait.edit(
            "**Search Results (YT Music):**",
            parse_mode=MD,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^song"))
    async def song_callback(client: Client, query: CallbackQuery):
        await query.answer()

        if query.data == "song_cancel":
            await query.message.delete()
            return

        parts = query.data.split("|", 2)
        if len(parts) != 3:
            return
        _, sk, vid_id = parts

        entry = _sessions.get(sk, {}).get(vid_id)
        if not entry:
            await query.message.reply(
                "❌ Session expired. Search again with `/song`.", parse_mode=MD
            )
            return

        title     = entry.get("title")    or "Unknown"
        artist    = entry.get("artist")   or "Unknown"
        dur_str   = entry.get("duration") or ""
        thumb_url = entry.get("thumbnail") or ""

        await query.message.edit("Preparing Your Song...")

        # Fetch thumbnail and download concurrently
        async def _get_thumb():
            return await ym.fetch_thumbnail(thumb_url) if thumb_url else None

        thumb_data, _ = await asyncio.gather(
            _get_thumb(),
            asyncio.sleep(0),   # placeholder so gather has 2 items
        )

        with tempfile.TemporaryDirectory() as tmp:
            await query.message.edit("Downloading ...")

            # Download runs in a thread (blocking pytubefix + ffmpeg)
            def _dl():
                return ym.download_mp3(
                    vid_id     = vid_id,
                    title      = title,
                    artist     = artist,
                    out_dir    = tmp,
                    thumb_bytes = thumb_data,
                )

            mp3_path = await asyncio.to_thread(_dl)

            if not mp3_path or not os.path.exists(mp3_path):
                await query.message.edit(
                    "❌ Download failed.\n"
                    "_The track may be unavailable or age-restricted._",
                    parse_mode=MD,
                )
                return

            size_mb = os.path.getsize(mp3_path) / 1024 / 1024
            if size_mb > 50:
                await query.message.edit(
                    f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB."
                )
                return

            # Build Telegram thumbnail from the same bytes
            thumb_io = None
            if thumb_data:
                thumb_io      = io.BytesIO(thumb_data)
                thumb_io.name = "thumb.jpg"

            await query.message.edit("Uploading ...")

            caption = (
                f"🎵 **Song**   : {title}\n"
                f"🎤 **Artist** : {artist}\n"
                f"📀 **Source** : YT Music"
            )

            await query.message.delete()
            await client.send_audio(
                query.message.chat.id,
                mp3_path,
                caption   = caption,
                parse_mode = MD,
                title      = title,
                performer  = artist,
                thumb      = thumb_io,
            )

        # Log
        class _FM:
            from_user = query.from_user
            chat      = query.message.chat

        await log_action(client, _FM(), "🎵 Song Download", f"`{title}` by {artist}")
