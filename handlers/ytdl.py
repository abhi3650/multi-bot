"""
handlers/ytdl.py
Command: /yt <youtube_url>
  Video qualities → cobalt.tools direct download link
  MP3 Audio       → downloads and uploads the file
"""

import asyncio
import hashlib
import io
import os
import re
import tempfile

import httpx
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db

YT_RE = re.compile(
    r"^(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)"
    r"[\w\-]+"
)

COBALT_API     = "https://api.cobalt.tools/"
COBALT_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

QUALITY_OPTIONS = [
    ("🎥 4K  (2160p)", "2160"),
    ("🎥 2K  (1440p)", "1440"),
    ("🎥 1080p FHD",   "1080"),
    ("🎥 720p  HD",    "720"),
    ("🎥 480p",        "480"),
    ("🎥 360p",        "360"),
]

# In-memory session store: {key: {url, meta}}
_sessions: dict[str, dict] = {}


def _is_yt(url: str) -> bool:
    return bool(YT_RE.match(url.strip()))


def _key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:10]


def _fmt_dur(s: int) -> str:
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _find_file(directory: str, ext: str) -> str | None:
    for f in os.listdir(directory):
        if f.lower().endswith(f".{ext}"):
            return os.path.join(directory, f)
    return None


async def _cobalt_link(url: str, quality: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=20) as hx:
            resp = await hx.post(
                COBALT_API,
                json={"url": url, "videoQuality": quality,
                      "downloadMode": "auto", "filenameStyle": "pretty"},
                headers=COBALT_HEADERS,
            )
            data   = resp.json()
            status = data.get("status", "")
            if status in ("stream", "redirect", "tunnel"):
                return data.get("url")
            if status == "picker" and data.get("picker"):
                return data["picker"][0].get("url")
    except Exception:
        pass
    return None


def register(app: Client):

    @app.on_message(filters.command("yt"))
    async def cmd_yt(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)

        args = message.command[1:]
        if not args:
            await message.reply(
                "📥 *YouTube Downloader*\n\n"
                "Usage: `/yt <youtube_url>`\n\n"
                "🎥 *Video* → direct download link\n"
                "🎵 *MP3*   → bot downloads & sends the file\n\n"
                "⚠️ _YouTube links only._"
            )
            return

        url = args[0].strip()
        if not _is_yt(url):
            await message.reply(
                "❌ Only YouTube links are supported.\n\n"
                "Example:\n`https://youtube.com/watch?v=dQw4w9WgXcQ`"
            )
            return

        wait = await message.reply("⏳ Fetching video info…")

        def _extract():
            with yt_dlp.YoutubeDL({
                "quiet":         True,
                "skip_download": True,
                "noplaylist":    True,
                "extractor_args": {"youtube": {"player_client": ["android"]}},
            }) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.to_thread(_extract)
        except Exception as e:
            await wait.edit(f"❌ Could not fetch info:\n`{e}`")
            return

        title     = info.get("title", "Unknown")
        uploader  = info.get("uploader", "Unknown")
        duration  = int(info.get("duration") or 0)
        views     = f"{info.get('view_count', 0):,}"
        thumbnail = info.get("thumbnail", "")
        formats   = info.get("formats", [])
        video_id  = info.get("id", "")
        clean_url = f"https://www.youtube.com/watch?v={video_id}"

        max_h = max(
            (f.get("height", 0) for f in formats
             if f.get("vcodec", "none") != "none" and f.get("height")),
            default=720,
        )

        k = _key(clean_url)
        _sessions[k] = {
            "url":  clean_url,
            "meta": {
                "title":     title,
                "uploader":  uploader,
                "duration":  duration,
                "thumbnail": thumbnail,
                "video_id":  video_id,
            },
        }

        rows = []
        pair = []
        for label, q_val in QUALITY_OPTIONS:
            if int(q_val) <= max_h:
                pair.append(InlineKeyboardButton(label, callback_data=f"ytq|{k}|{q_val}"))
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
        if pair:
            rows.append(pair)
        rows.append([InlineKeyboardButton("🎵 MP3 Audio (send file)", callback_data=f"ytq|{k}|audio")])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="ytq_cancel")])

        caption = (
            f"🎬 *{title}*\n"
            f"👤 `{uploader}`\n"
            f"⏱ `{_fmt_dur(duration)}`  •  👁 `{views} views`\n\n"
            "🎥 Video → direct download link\n"
            "🎵 Audio → bot sends the MP3 file"
        )

        await wait.delete()
        if thumbnail:
            await client.send_photo(
                message.chat.id, thumbnail,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(rows),
            )
        else:
            await message.reply(caption, reply_markup=InlineKeyboardMarkup(rows))

    # ── Callback ──────────────────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^ytq"))
    async def yt_callback(client: Client, query: CallbackQuery):
        await query.answer()

        if query.data == "ytq_cancel":
            await query.message.delete()
            return

        _, k, quality = query.data.split("|", 2)
        session = _sessions.get(k)
        if not session:
            await query.message.reply("❌ Session expired. Please send the link again.")
            return

        url  = session["url"]
        meta = session["meta"]

        if quality == "audio":
            await _send_audio(client, query, url, meta)
        else:
            await _send_video_link(query, url, quality)

    async def _send_video_link(query, url: str, quality: str):
        orig = query.message.caption or ""
        try:
            await query.message.edit_caption(orig + f"\n\n⏳ Getting *{quality}p* link…")
        except Exception:
            pass

        link = await _cobalt_link(url, quality)

        try:
            await query.message.edit_caption(orig, reply_markup=query.message.reply_markup)
        except Exception:
            pass

        if not link:
            await query.message.reply(
                f"❌ Could not get link for *{quality}p*.\nTry a different quality."
            )
            return

        await query.message.reply(
            f"✅ *{quality}p — Download Link*\n\n"
            f"[⬇️ Tap here to download]({link})\n\n"
            "⏳ _Link is temporary — download soon!_"
        )

    async def _send_audio(client, query, url: str, meta: dict):
        title     = meta.get("title",    "Unknown")
        uploader  = meta.get("uploader", "Unknown")
        duration  = int(meta.get("duration") or 0)
        thumb_url = meta.get("thumbnail", "")
        video_id  = meta.get("video_id", "")

        try:
            await query.message.edit_caption(
                (query.message.caption or "") + "\n\n⏳ Downloading MP3…"
            )
        except Exception:
            pass

        # Download thumbnail bytes
        thumb_data = None
        if thumb_url:
            try:
                async with httpx.AsyncClient(timeout=10) as hx:
                    r = await hx.get(thumb_url)
                    if r.status_code == 200:
                        thumb_data = r.content
            except Exception:
                pass

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
                    ydl.extract_info(url, download=True)

            try:
                await asyncio.to_thread(_dl)
            except Exception as e:
                await query.message.reply(f"❌ Download failed:\n`{e}`")
                return

            mp3 = os.path.join(tmp, f"{video_id}.mp3")
            if not os.path.exists(mp3):
                mp3 = _find_file(tmp, "mp3")
            if not mp3:
                await query.message.reply("❌ Could not find downloaded audio.")
                return

            size_mb = os.path.getsize(mp3) / 1024 / 1024
            if size_mb > 50:
                await query.message.reply(f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.")
                return

            # Embed thumbnail via mutagen
            if thumb_data:
                from mutagen.id3 import ID3, APIC, TIT2, TPE1, error as ID3Error
                from mutagen.mp3 import MP3
                from PIL import Image
                try:
                    img = Image.open(io.BytesIO(thumb_data))
                    if img.mode in ("RGBA", "P", "LA"):
                        img = img.convert("RGB")
                    jbuf = io.BytesIO()
                    img.save(jbuf, format="JPEG", quality=90)
                    jpeg = jbuf.getvalue()

                    audio = MP3(mp3, ID3=ID3)
                    try:
                        audio.add_tags()
                    except ID3Error:
                        pass
                    audio.tags.delall("APIC")
                    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg))
                    audio.tags.add(TIT2(encoding=3, text=title))
                    audio.tags.add(TPE1(encoding=3, text=uploader))
                    audio.save(v2_version=3)
                except Exception:
                    pass

            thumb_io = None
            if thumb_data:
                thumb_io      = io.BytesIO(thumb_data)
                thumb_io.name = "thumb.jpg"

            caption = (
                f"🎵 *{title}*\n"
                f"👤 `{uploader}`\n"
                f"⏱ `{_fmt_dur(duration)}`  •  💾 `{size_mb:.1f} MB`"
            )

            await query.message.delete()
            await client.send_audio(
                query.message.chat.id,
                mp3,
                caption=caption,
                title=title,
                performer=uploader,
                duration=duration,
                thumb=thumb_io,
            )
