"""
handlers/song.py  —  /song command

Domain: music.youtube.com ONLY (different from /yt which is youtube.com)
Search: ytsearch8: with site filter to prefer YT Music results
Download: always from music.youtube.com/watch?v=ID

Bot-detection bypass: same strategy as ytdl.py
  With cookies → web client (authenticated)
  Without cookies → tv_embedded (no login required for most tracks)
"""

import asyncio
import hashlib
import io
import os
import tempfile
import time

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
from cookie_helper import get_cookie_file
from logger import log_action

MD = ParseMode.MARKDOWN

_sessions: dict[str, dict] = {}

YT_MUSIC_BASE = "https://music.youtube.com/watch?v="


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


def _bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _human_speed(bps: float) -> str:
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.1f} MB/s"
    if bps >= 1_000:
        return f"{bps/1_000:.0f} KB/s"
    return f"{bps:.0f} B/s"


def _ydl_opts(cookie_path: str | None, extra: dict | None = None) -> dict:
    """
    With cookies → web client (authenticated, works on everything).
    Without cookies → tv_embedded (bypasses login for most music tracks).
    """
    client      = ["web"]        if cookie_path else ["tv_embedded"]
    player_skip = ["webpage", "configs"]

    opts = {
        "quiet":       True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {
                "player_client": client,
                "player_skip":   player_skip,
            }
        },
    }
    if cookie_path:
        opts["cookiefile"] = cookie_path
    if extra:
        opts.update(extra)
    return opts


def _embed_art(mp3_path: str, thumb_data: bytes, title: str, artist: str):
    try:
        img = Image.open(io.BytesIO(thumb_data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)

        audio = MP3(mp3_path, ID3=ID3)
        try:
            audio.add_tags()
        except ID3Error:
            pass
        audio.tags.delall("APIC")
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=buf.getvalue()))
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.save(v2_version=3)
    except Exception as e:
        print(f"[song/embed] {e}")


def _make_progress_hook(status_msg, title: str):
    last_edit = [0.0]
    loop      = asyncio.get_event_loop()

    def hook(d: dict):
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - last_edit[0] < 2.5:
            return
        last_edit[0] = now

        downloaded = d.get("downloaded_bytes") or 0
        total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        speed      = d.get("speed") or 0
        eta        = d.get("eta") or 0

        if total:
            pct  = downloaded / total * 100
            bar  = _bar(pct)
            size = f"{downloaded/1_048_576:.1f}/{total/1_048_576:.1f} MB"
        else:
            pct  = 0
            bar  = "░" * 10
            size = f"{downloaded/1_048_576:.1f} MB"

        text = (
            f"Downloading ...\n\n"
            f"🎵 **{title[:40]}**\n\n"
            f"`{bar}` {pct:.0f}%\n"
            f"📦 {size}\n"
            f"⚡ {_human_speed(speed)}  •  ⏳ {eta}s"
        )

        async def _edit():
            try:
                await status_msg.edit(text, parse_mode=MD)
            except Exception:
                pass

        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_edit()))

    return hook


# ── Handlers ──────────────────────────────────────────────────────────────────

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

        query       = " ".join(args)
        wait        = await message.reply("🔍 Searching…")
        cookie_path = await get_cookie_file()

        def _search():
            opts = _ydl_opts(cookie_path, {
                "extract_flat": True,
                "noplaylist":   False,
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Plain ytsearch on YouTube; we then use music URL for download
                return ydl.extract_info(f"ytsearch8:{query}", download=False)

        try:
            data = await asyncio.to_thread(_search)
        except Exception as e:
            err = str(e)
            if "Sign in" in err or "bot" in err.lower():
                await wait.edit(
                    "❌ **YouTube requires sign-in for search.**\n"
                    "Upload cookies with `/cook` and try again.",
                    parse_mode=MD,
                )
            else:
                await wait.edit(f"❌ Search failed:\n`{err}`", parse_mode=MD)
            return

        entries = [e for e in (data.get("entries") or []) if e and e.get("id")]
        if not entries:
            await wait.edit("❌ No results found. Try a different search term.")
            return

        sk = _key(f"{u.id}{query}")
        _sessions[sk] = {e["id"]: e for e in entries}

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

        title    = entry.get("title")    or "Unknown"
        artist   = entry.get("uploader") or entry.get("channel") or "Unknown"
        duration = int(entry.get("duration") or 0)

        # Always download from music.youtube.com
        yt_music_url = f"{YT_MUSIC_BASE}{vid_id}"

        thumb_url = (
            entry.get("thumbnail")
            or ((entry.get("thumbnails") or [{}])[-1].get("url"))
        )

        await query.message.edit("Preparing Your Song...")

        # Fetch thumbnail and cookies concurrently
        async def _get_thumb():
            if not thumb_url:
                return None
            try:
                async with httpx.AsyncClient(timeout=10) as hx:
                    r = await hx.get(thumb_url)
                    return r.content if r.status_code == 200 else None
            except Exception:
                return None

        thumb_data, cookie_path = await asyncio.gather(
            _get_thumb(),
            get_cookie_file(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            # Copy cookie to tmp dir so it stays valid for the thread
            if cookie_path and not cookie_path.startswith(tmp):
                import shutil
                local_cookie = os.path.join(tmp, "cookies.txt")
                shutil.copy2(cookie_path, local_cookie)
                cookie_path = local_cookie

            hook = _make_progress_hook(query.message, title)

            ydl_opts = _ydl_opts(cookie_path, {
                "format":         "bestaudio[ext=m4a]/bestaudio/best",
                "outtmpl":        os.path.join(tmp, "%(id)s.%(ext)s"),
                "progress_hooks": [hook],
                "postprocessors": [
                    {"key": "FFmpegExtractAudio",
                     "preferredcodec": "mp3", "preferredquality": "192"},
                    {"key": "FFmpegMetadata", "add_metadata": True},
                ],
            })

            def _dl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(yt_music_url, download=True)

            try:
                await asyncio.to_thread(_dl)
            except Exception as e:
                err = str(e)
                if "Sign in" in err or "bot" in err.lower():
                    await query.message.edit(
                        "❌ **YouTube requires sign-in.**\n"
                        "Upload cookies with `/cook` and try again.",
                        parse_mode=MD,
                    )
                else:
                    await query.message.edit(f"❌ Download failed:\n`{err}`", parse_mode=MD)
                return

            mp3 = os.path.join(tmp, f"{vid_id}.mp3")
            if not os.path.exists(mp3):
                mp3 = _find_mp3(tmp)
            if not mp3:
                await query.message.edit("❌ Could not find downloaded audio file.")
                return

            # Embed album art
            if thumb_data and isinstance(thumb_data, bytes):
                await asyncio.to_thread(_embed_art, mp3, thumb_data, title, artist)

            size_mb = os.path.getsize(mp3) / 1024 / 1024
            if size_mb > 50:
                await query.message.edit(
                    f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB."
                )
                return

            thumb_io = None
            if thumb_data and isinstance(thumb_data, bytes):
                thumb_io      = io.BytesIO(thumb_data)
                thumb_io.name = "thumb.jpg"

            await query.message.edit("Uploading ...")

            song_name = entry.get("track") or title
            caption   = (
                f"🎵 **Song**   : {song_name}\n"
                f"🎤 **Artist** : {artist}\n"
                f"📀 **Source** : YT Music"
            )

            await query.message.delete()
            await client.send_audio(
                query.message.chat.id, mp3,
                caption=caption, parse_mode=MD,
                title=title, performer=artist,
                duration=duration, thumb=thumb_io,
            )

            # Log (build a minimal message-like object for logger)
            class _FakeMsg:
                from_user = query.from_user
                chat      = query.message.chat

            await log_action(client, _FakeMsg(), "🎵 Song Download", f"`{title}` by {artist}")
