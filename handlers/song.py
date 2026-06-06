"""
handlers/song.py
Command: /song <song name>
Searches YouTube Music → shows 8 results → downloads MP3
→ embeds thumbnail via mutagen → uploads to Telegram.
"""

import asyncio
import hashlib
import io
import os
import tempfile

import httpx
from PIL import Image
import yt_dlp
from mutagen.id3 import ID3, APIC, TIT2, TPE1, error as ID3Error
from mutagen.mp3 import MP3
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db

_sessions: dict[str, dict] = {}


def _key(seed: str) -> str:
    return hashlib.md5(seed.encode()).hexdigest()[:10]


def _fmt_dur(s) -> str:
    if not s:
        return "?:??"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"


def _find_mp3(directory: str) -> str | None:
    for f in os.listdir(directory):
        if f.lower().endswith(".mp3"):
            return os.path.join(directory, f)
    return None


def _embed(mp3_path: str, thumb_data: bytes, title: str, artist: str):
    """Embed album art + metadata into MP3 (works in VLC, WMP, Apple Music, etc.)."""
    try:
        img = Image.open(io.BytesIO(thumb_data))
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        jbuf = io.BytesIO()
        img.save(jbuf, format="JPEG", quality=90)
        jpeg = jbuf.getvalue()

        audio = MP3(mp3_path, ID3=ID3)
        try:
            audio.add_tags()
        except ID3Error:
            pass
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.save(v2_version=3)
    except Exception as e:
        print(f"[song] embed failed: {e}")


def register(app: Client):

    @app.on_message(filters.command("song"))
    async def cmd_song(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)

        args = message.command[1:]
        if not args:
            await message.reply(
                "🎵 *Song Search (YouTube Music)*\n\n"
                "Usage: `/song <song name>`\n"
                "Example: `/song Astronaut In The Ocean`\n\n"
                "_Shows 8 results — tap one to download & receive the MP3 with album art._"
            )
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Searching YouTube Music for *{query}*…")

        def _search():
            with yt_dlp.YoutubeDL({
                "quiet":         True,
                "extract_flat":  True,
                "noplaylist":    False,
                "extractor_args": {"youtube": {"player_client": ["android"]}},
            }) as ydl:
                return ydl.extract_info(f"ytsearch8:{query}", download=False)

        try:
            data = await asyncio.to_thread(_search)
        except Exception as e:
            await wait.edit(f"❌ Search failed:\n`{e}`")
            return

        entries = [e for e in (data.get("entries") or []) if e and e.get("id")]
        if not entries:
            await wait.edit("❌ No results found. Try a different search.")
            return

        sk = _key(f"{u.id}{query}")
        _sessions[sk] = {e["id"]: e for e in entries}

        lines   = [f"🎵 *Results for:* `{query}`\n"]
        buttons = []
        for i, entry in enumerate(entries, 1):
            title  = (entry.get("title") or "Unknown")[:50]
            artist = entry.get("uploader") or entry.get("channel") or "Unknown"
            dur    = _fmt_dur(entry.get("duration"))
            lines.append(f"`{i}.` *{title}*\n    👤 {artist}  ⏱ {dur}")
            buttons.append([InlineKeyboardButton(
                f"{i}. {title[:42]}",
                callback_data=f"song|{sk}|{entry['id']}",
            )])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="song_cancel")])

        await wait.edit(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )

    @app.on_callback_query(filters.regex(r"^song"))
    async def song_callback(client: Client, query: CallbackQuery):
        await query.answer()

        if query.data == "song_cancel":
            await query.message.delete()
            return

        _, sk, vid_id = query.data.split("|", 2)
        entry = _sessions.get(sk, {}).get(vid_id)
        if not entry:
            await query.message.reply("❌ Session expired. Search again with `/song`.")
            return

        title    = entry.get("title")    or "Unknown"
        artist   = entry.get("uploader") or entry.get("channel") or "Unknown"
        duration = int(entry.get("duration") or 0)
        yt_url   = f"https://music.youtube.com/watch?v={vid_id}"
        thumb_url = (
            entry.get("thumbnail")
            or (entry.get("thumbnails") or [{}])[-1].get("url")
        )

        await query.message.edit(f"⏳ Downloading *{title}*…")

        async def _fetch_thumb():
            if not thumb_url:
                return None
            try:
                async with httpx.AsyncClient(timeout=10) as hx:
                    r = await hx.get(thumb_url)
                    return r.content if r.status_code == 200 else None
            except Exception:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            ydl_opts = {
                "format":         "bestaudio/best",
                "outtmpl":        os.path.join(tmp, "%(id)s.%(ext)s"),
                "quiet":          True,
                "extractor_args": {"youtube": {"player_client": ["android"]}},
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                    {"key": "FFmpegMetadata",     "add_metadata": True},
                ],
            }

            def _dl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(yt_url, download=True)

            # Run download and thumbnail fetch concurrently
            results = await asyncio.gather(
                _fetch_thumb(),
                asyncio.to_thread(_dl),
                return_exceptions=True,
            )
            thumb_data = results[0] if not isinstance(results[0], Exception) else None
            dl_result  = results[1]

            if isinstance(dl_result, Exception):
                await query.message.edit(f"❌ Download failed:\n`{dl_result}`")
                return

            mp3 = os.path.join(tmp, f"{vid_id}.mp3")
            if not os.path.exists(mp3):
                mp3 = _find_mp3(tmp)
            if not mp3:
                await query.message.edit("❌ Could not find downloaded audio file.")
                return

            # Embed cover art into MP3 using mutagen
            if thumb_data and isinstance(thumb_data, bytes):
                await asyncio.to_thread(_embed, mp3, thumb_data, title, artist)

            size_mb = os.path.getsize(mp3) / 1024 / 1024
            if size_mb > 50:
                await query.message.edit(f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.")
                return

            # Build a fresh BytesIO for the Telegram thumbnail (never share with mutagen)
            thumb_io = None
            if thumb_data and isinstance(thumb_data, bytes):
                thumb_io      = io.BytesIO(thumb_data)
                thumb_io.name = "thumb.jpg"

            caption = (
                f"🎵 *{title}*\n"
                f"👤 `{artist}`\n"
                f"⏱ `{_fmt_dur(duration)}`  •  💾 `{size_mb:.1f} MB`"
            )

            await query.message.delete()
            await client.send_audio(
                query.message.chat.id,
                mp3,
                caption=caption,
                title=title,
                performer=artist,
                duration=duration,
                thumb=thumb_io,
            )
