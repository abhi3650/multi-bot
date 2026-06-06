import asyncio
import hashlib
import io
import os
import tempfile

import httpx
import yt_dlp
from PIL import Image
from mutagen.id3 import APIC, ID3, TIT2, TPE1
from mutagen.id3 import error as ID3Error
from mutagen.mp3 import MP3
from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                             InlineKeyboardMarkup, Message)

import database as db

_sessions: dict = {}   # {key: {vid_id: entry_dict}}


def _key(seed: str) -> str:
    return hashlib.md5(seed.encode()).hexdigest()[:10]


def _fmt(s) -> str:
    if not s: return "?:??"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"


def _find_mp3(d: str) -> str | None:
    for f in os.listdir(d):
        if f.lower().endswith(".mp3"):
            return os.path.join(d, f)
    return None


def _embed_thumb(mp3_path: str, thumb_data: bytes, title: str, artist: str):
    try:
        img = Image.open(io.BytesIO(thumb_data))
        if img.mode in ("RGBA","P","LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        jpeg = buf.getvalue()

        audio = MP3(mp3_path, ID3=ID3)
        try:    audio.add_tags()
        except ID3Error: pass
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.save(v2_version=3)
    except Exception as e:
        print(f"[song] thumb embed failed: {e}")


@Client.on_message(filters.command("song"))
async def cmd_song(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")

    args = message.command[1:]
    if not args:
        return await message.reply(
            "🎵 **Song Search (YouTube Music)**\n\n"
            "Usage: `/song <song name>`\n"
            "Example: `/song Astronaut In The Ocean`"
        )

    query = " ".join(args)
    msg   = await message.reply(f"🔍 Searching YouTube Music for **{query}**…")

    def _search():
        with yt_dlp.YoutubeDL({
            "quiet": True, "extract_flat": True, "noplaylist": False,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }) as ydl:
            return ydl.extract_info(f"ytsearch8:{query}", download=False)

    try:
        data = await asyncio.to_thread(_search)
    except Exception as e:
        return await msg.edit(f"❌ Search failed:\n`{e}`")

    entries = [e for e in (data.get("entries") or []) if e and e.get("id")]
    if not entries:
        return await msg.edit("❌ No results found.")

    sk = _key(f"{u.id}{query}")
    _sessions[sk] = {e["id"]: e for e in entries}

    lines   = [f"🎵 **Results for:** `{query}`\n"]
    buttons = []
    for i, entry in enumerate(entries, 1):
        title    = (entry.get("title") or "Unknown")[:50]
        artist   = entry.get("uploader") or entry.get("channel") or "Unknown"
        duration = _fmt(entry.get("duration"))
        lines.append(f"`{i}.` **{title}**\n    👤 {artist}  ⏱ {duration}")
        buttons.append([InlineKeyboardButton(f"{i}. {title[:42]}", callback_data=f"song|{sk}|{entry['id']}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="song_cancel")])

    await msg.edit("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons),
                   disable_web_page_preview=True)


@Client.on_callback_query(filters.regex("^(song|song_cancel)"))
async def song_callback(client: Client, cq: CallbackQuery):
    await cq.answer()

    if cq.data == "song_cancel":
        return await cq.message.delete()

    _, sk, vid_id = cq.data.split("|", 2)
    entry = _sessions.get(sk, {}).get(vid_id)
    if not entry:
        return await cq.message.reply("❌ Session expired. Search again with `/song`.")

    title    = entry.get("title")    or "Unknown"
    artist   = entry.get("uploader") or entry.get("channel") or "Unknown"
    duration = int(entry.get("duration") or 0)
    yt_url   = f"https://music.youtube.com/watch?v={vid_id}"

    await cq.message.edit(f"⏳ Downloading **{title}**…")

    # Fetch thumb and download audio concurrently
    thumb_url = entry.get("thumbnail") or (entry.get("thumbnails") or [{}])[-1].get("url")

    async def _fetch_thumb():
        if not thumb_url: return None
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(thumb_url)
                return r.content if r.status_code == 200 else None
        except Exception:
            return None

    with tempfile.TemporaryDirectory() as tmp:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "quiet": True,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
            "postprocessors": [
                {"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"},
                {"key":"FFmpegMetadata","add_metadata":True},
            ],
        }

        def _dl():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(yt_url, download=True)

        thumb_data, dl_result = await asyncio.gather(
            _fetch_thumb(),
            asyncio.to_thread(_dl),
            return_exceptions=True,
        )

        if isinstance(dl_result, Exception):
            return await cq.message.edit(f"❌ Download failed:\n`{dl_result}`")
        if isinstance(thumb_data, Exception):
            thumb_data = None

        mp3 = os.path.join(tmp, f"{vid_id}.mp3") 
        if not os.path.exists(mp3):
            mp3 = _find_mp3(tmp)
        if not mp3:
            return await cq.message.edit("❌ Audio file not found.")

        if thumb_data and isinstance(thumb_data, bytes):
            await asyncio.to_thread(_embed_thumb, mp3, thumb_data, title, artist)

        size_mb = os.path.getsize(mp3) / 1024 / 1024
        if size_mb > 50:
            return await cq.message.edit(f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.")

        # Build thumb for Telegram display
        thumb_path = None
        if thumb_data and isinstance(thumb_data, bytes):
            thumb_path = os.path.join(tmp, "thumb.jpg")
            try:
                img = Image.open(io.BytesIO(thumb_data))
                img.convert("RGB").save(thumb_path, "JPEG")
            except Exception:
                thumb_path = None

        caption = (
            f"🎵 **{title}**\n"
            f"👤 `{artist}`\n"
            f"⏱ `{_fmt(duration)}`  •  💾 `{size_mb:.1f} MB`"
        )
        await cq.message.delete()
        await cq.message.reply_audio(
            audio=mp3, title=title, performer=artist,
            duration=duration, thumb=thumb_path, caption=caption,
        )
