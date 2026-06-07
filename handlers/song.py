"""
handlers/song.py  —  /song command
Search YouTube Music → pick from 8 results → download MP3 with embedded album art.
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
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db

MD = ParseMode.MARKDOWN

# In-memory session store {session_key: {video_id: entry}}
_sessions: dict[str, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


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
    """Embed album art + ID3 metadata — works in VLC, WMP, Apple Music, foobar2000."""
    try:
        img = Image.open(io.BytesIO(thumb_data))
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        jpeg = buf.getvalue()

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
        print(f"[song/embed] {e}")


# ── Handler registration ──────────────────────────────────────────────────────

def register(app: Client):

    @app.on_message(filters.command("song"))
    async def cmd_song(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        args = message.command[1:]
        if not args:
            await message.reply(
                "🎵 **Song Search (YouTube Music)**\n\n"
                "**Usage:** `/song <song name>`\n"
                "**Example:** `/song Blinding Lights`\n\n"
                "_Shows 8 results — tap one to download the MP3 with album art._",
                parse_mode=MD,
            )
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Searching YouTube Music for **{query}**…", parse_mode=MD)

        def _search():
            with yt_dlp.YoutubeDL({
                "quiet":        True,
                "extract_flat": True,
                "noplaylist":   False,
                "extractor_args": {"youtube": {"player_client": ["android"]}},
            }) as ydl:
                return ydl.extract_info(f"ytsearch8:{query}", download=False)

        try:
            data = await asyncio.to_thread(_search)
        except Exception as e:
            await wait.edit(f"❌ Search failed:\n`{e}`", parse_mode=MD)
            return

        entries = [e for e in (data.get("entries") or []) if e and e.get("id")]
        if not entries:
            await wait.edit("❌ No results found. Try a different search term.")
            return

        sk = _key(f"{u.id}{query}")
        _sessions[sk] = {e["id"]: e for e in entries}

        # Build result list text
        lines = [f"🎵 **Results for:** `{query}`\n"]
        for i, entry in enumerate(entries, 1):
            title  = (entry.get("title") or "Unknown")[:50]
            artist = entry.get("uploader") or entry.get("channel") or "Unknown"
            dur    = _fmt_dur(entry.get("duration"))
            lines.append(f"**{i}.** {title}\n    👤 {artist}  ⏱ {dur}")

        # One button per row
        buttons = [
            [InlineKeyboardButton(
                f"{i}. {(entry.get('title') or 'Unknown')[:44]}",
                callback_data=f"song|{sk}|{entry['id']}",
            )]
            for i, entry in enumerate(entries, 1)
        ]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="song_cancel")])

        await wait.edit(
            "\n".join(lines),
            parse_mode=MD,
            reply_markup=InlineKeyboardMarkup(buttons),
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
            await query.message.reply(
                "❌ Session expired. Search again with `/song`.",
                parse_mode=MD,
            )
            return

        title     = entry.get("title")    or "Unknown"
        artist    = entry.get("uploader") or entry.get("channel") or "Unknown"
        duration  = int(entry.get("duration") or 0)
        yt_url    = f"https://music.youtube.com/watch?v={vid_id}"
        thumb_url = (
            entry.get("thumbnail")
            or ((entry.get("thumbnails") or [{}])[-1].get("url"))
        )

        await query.message.edit(
            f"⏳ Downloading **{title}**…\n_This may take a moment._",
            parse_mode=MD,
        )

        async def _fetch_thumb() -> bytes | None:
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

            # Fetch thumbnail and download audio concurrently
            thumb_data, dl_err = await asyncio.gather(
                _fetch_thumb(),
                asyncio.to_thread(_dl),
                return_exceptions=True,
            )
            if isinstance(thumb_data, Exception):
                thumb_data = None
            if isinstance(dl_err, Exception):
                await query.message.edit(f"❌ Download failed:\n`{dl_err}`", parse_mode=MD)
                return

            # Find the downloaded mp3
            mp3 = os.path.join(tmp, f"{vid_id}.mp3")
            if not os.path.exists(mp3):
                mp3 = _find_mp3(tmp)
            if not mp3:
                await query.message.edit("❌ Could not find the downloaded audio file.")
                return

            # Embed album art via mutagen
            if thumb_data and isinstance(thumb_data, bytes):
                await asyncio.to_thread(_embed, mp3, thumb_data, title, artist)

            size_mb = os.path.getsize(mp3) / 1024 / 1024
            if size_mb > 50:
                await query.message.edit(
                    f"❌ File too large ({size_mb:.1f} MB). Telegram's limit is 50 MB."
                )
                return

            # Fresh BytesIO for Telegram thumb — never reuse after mutagen
            thumb_io = None
            if thumb_data and isinstance(thumb_data, bytes):
                thumb_io      = io.BytesIO(thumb_data)
                thumb_io.name = "thumb.jpg"

            caption = (
                f"🎵 **{title}**\n"
                f"👤 `{artist}`\n"
                f"⏱ `{_fmt_dur(duration)}`  •  💾 `{size_mb:.1f} MB`"
            )

            await query.message.delete()
            await client.send_audio(
                query.message.chat.id,
                mp3,
                caption=caption,
                parse_mode=MD,
                title=title,
                performer=artist,
                duration=duration,
                thumb=thumb_io,
            )
