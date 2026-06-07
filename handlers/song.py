"""
handlers/song.py  —  /song command

Flow (matches UI screenshots exactly):
  1. User sends /song NoCopyrights
  2. Bot replies with "Search Results (YT Music):" + one button per result
  3. User taps a button
  4. Status: "Preparing Your Song..."  →  "Downloading ..."  →  "Uploading ..."
  5. Bot sends the MP3 audio file with caption:
       🎵 Song   : <title>
       🎤 Artist  : <artist>
       📀 Source  : YT Music
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

# In-memory session store: { session_key: { video_id: entry_dict } }
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


def _embed_art(mp3_path: str, thumb_data: bytes, title: str, artist: str):
    """Embed album art + ID3 tags. Works in VLC, WMP, Apple Music, foobar2000."""
    try:
        img = Image.open(io.BytesIO(thumb_data))
        if img.mode != "RGB":
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

    # ── /song ─────────────────────────────────────────────────────────────────
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
                "_Tap a result to download it as MP3 with album art._",
                parse_mode=MD,
            )
            return

        query = " ".join(args)
        wait  = await message.reply("🔍 Searching…")

        # ── Search YouTube Music ───────────────────────────────────────────────
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

        # Store session
        sk = _key(f"{u.id}{query}")
        _sessions[sk] = {e["id"]: e for e in entries}

        # ── Build result buttons (one per row, matches screenshot) ─────────────
        # Header: "Search Results (YT Music):"
        buttons = [
            [InlineKeyboardButton(
                (entry.get("title") or "Unknown")[:55],
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

    # ── Callback: user taps a song ─────────────────────────────────────────────
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
                "❌ Session expired. Search again with `/song`.",
                parse_mode=MD,
            )
            return

        title    = entry.get("title")    or "Unknown"
        artist   = entry.get("uploader") or entry.get("channel") or "Unknown"
        duration = int(entry.get("duration") or 0)
        yt_url   = f"https://music.youtube.com/watch?v={vid_id}"
        thumb_url = (
            entry.get("thumbnail")
            or ((entry.get("thumbnails") or [{}])[-1].get("url"))
        )

        # ── Status: Preparing ──────────────────────────────────────────────────
        await query.message.edit("Preparing Your Song...")

        # ── Fetch thumbnail ────────────────────────────────────────────────────
        async def _fetch_thumb() -> bytes | None:
            if not thumb_url:
                return None
            try:
                async with httpx.AsyncClient(timeout=10) as hx:
                    r = await hx.get(thumb_url)
                    return r.content if r.status_code == 200 else None
            except Exception:
                return None

        thumb_data = await _fetch_thumb()

        # ── Status: Downloading ────────────────────────────────────────────────
        await query.message.edit("Downloading ...")

        with tempfile.TemporaryDirectory() as tmp:
            ydl_opts = {
                "format":         "bestaudio/best",
                "outtmpl":        os.path.join(tmp, "%(id)s.%(ext)s"),
                "quiet":          True,
                "extractor_args": {"youtube": {"player_client": ["android"]}},
                "postprocessors": [
                    {
                        "key":              "FFmpegExtractAudio",
                        "preferredcodec":   "mp3",
                        "preferredquality": "192",
                    },
                    {
                        "key":          "FFmpegMetadata",
                        "add_metadata": True,
                    },
                ],
            }

            def _dl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(yt_url, download=True)

            try:
                await asyncio.to_thread(_dl)
            except Exception as e:
                await query.message.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
                return

            # Locate the mp3
            mp3 = os.path.join(tmp, f"{vid_id}.mp3")
            if not os.path.exists(mp3):
                mp3 = _find_mp3(tmp)
            if not mp3:
                await query.message.edit("❌ Could not find downloaded audio file.")
                return

            # Embed album art via mutagen (in background thread)
            if thumb_data and isinstance(thumb_data, bytes):
                await asyncio.to_thread(_embed_art, mp3, thumb_data, title, artist)

            size_mb = os.path.getsize(mp3) / 1024 / 1024
            if size_mb > 50:
                await query.message.edit(
                    f"❌ File too large ({size_mb:.1f} MB). Telegram's limit is 50 MB."
                )
                return

            # Fresh BytesIO for Telegram thumbnail (mutagen must not share this)
            thumb_io = None
            if thumb_data and isinstance(thumb_data, bytes):
                thumb_io      = io.BytesIO(thumb_data)
                thumb_io.name = "thumb.jpg"

            # ── Status: Uploading ──────────────────────────────────────────────
            await query.message.edit("Uploading ...")

            # ── Caption matching screenshot ────────────────────────────────────
            # 🎵 Song   : Nocopyright
            # 🎤 Artist  : Charlie Brown
            # 📀 Source  : YT Music
            song_name = entry.get("track") or title   # yt-dlp may parse track separately
            caption = (
                f"🎵 **Song**   : {song_name}\n"
                f"🎤 **Artist** : {artist}\n"
                f"📀 **Source** : YT Music"
            )

            # Delete the status message, then send audio
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
