"""
handlers/ytdl.py  —  /yt command

YouTube bot-detection fix:
  Uses yt-dlp's innertube API directly (no webpage fetch) with
  a real browser User-Agent. Falls back through multiple strategies.

Speed: video info fetch is near-instant since we skip webpage loading.
"""

import asyncio
import hashlib
import io
import os
import re
import tempfile
import time

import httpx
import yt_dlp
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

YT_RE = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|live/)|youtu\.be/)"
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

_sessions: dict[str, dict] = {}

# ── yt-dlp strategy ───────────────────────────────────────────────────────────
# Strategy 1 (with cookies): web client — fully authenticated, most reliable
# Strategy 2 (no cookies):   tv_embedded — bypasses bot check without login
#   Key: player_skip=["webpage"] avoids the JS bot-detection entirely
#   Key: innertube_client means we use the internal API, not the public webpage

def _ydl_opts(cookie_path: str | None, extra: dict | None = None) -> dict:
    if cookie_path:
        # Authenticated: use web client with real cookies
        client_list  = ["web"]
        player_skip  = ["webpage", "configs"]
    else:
        # Unauthenticated: tv_embedded skips age/login gates
        client_list  = ["tv_embedded"]
        player_skip  = ["webpage", "configs"]

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
                "player_client": client_list,
                "player_skip":   player_skip,
            }
        },
    }
    if cookie_path:
        opts["cookiefile"] = cookie_path
    if extra:
        opts.update(extra)
    return opts


# ── Helpers ───────────────────────────────────────────────────────────────────

def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


def _is_yt(url: str) -> bool:
    return bool(YT_RE.search(url.strip()))


def _key(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:10]


def _fmt_dur(s: int) -> str:
    h, r = divmod(int(s), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _find_file(directory: str, ext: str) -> str | None:
    for f in os.listdir(directory):
        if f.lower().endswith(f".{ext}"):
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


def _make_progress_hook(status_msg, label: str):
    """Returns a yt-dlp progress hook that updates status_msg live."""
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
            f"**{label}**\n\n"
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


# ── Handlers ──────────────────────────────────────────────────────────────────

def register(app: Client):

    @app.on_message(filters.command("yt") & filters.private)
    async def cmd_yt(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        args = message.command[1:]
        if not args:
            await message.reply(
                "📥 **YouTube Downloader**\n\n"
                "**Usage:** `/yt <youtube_url>`\n\n"
                "🎥 Video → pick quality → direct download link\n"
                "🎵 MP3   → bot downloads and sends the file\n\n"
                "⚠️ _YouTube links only. For songs use `/song`._",
                parse_mode=MD,
            )
            return

        url = args[0].strip()
        if not _is_yt(url):
            await message.reply(
                "❌ **Not a valid YouTube link.**\n\n"
                "• `https://youtube.com/watch?v=...`\n"
                "• `https://youtu.be/...`\n"
                "• `https://youtube.com/shorts/...`",
                parse_mode=MD,
            )
            return

        wait        = await message.reply("⏳ Fetching video info…")
        cookie_path = await get_cookie_file()

        def _extract():
            opts = _ydl_opts(cookie_path, {"skip_download": True, "noplaylist": True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.to_thread(_extract)
        except Exception as e:
            err = str(e)
            # Give a helpful message for the most common errors
            if "Sign in" in err or "bot" in err.lower():
                await wait.edit(
                    "❌ **YouTube requires sign-in for this video.**\n\n"
                    "Upload your cookies using the `/cook` command and try again.",
                    parse_mode=MD,
                )
            else:
                await wait.edit(f"❌ Could not fetch video info:\n`{err}`", parse_mode=MD)
            return

        title     = info.get("title", "Unknown")
        uploader  = info.get("uploader") or info.get("channel") or "Unknown"
        duration  = int(info.get("duration") or 0)
        views     = f"{info.get('view_count', 0):,}"
        likes     = f"{info.get('like_count', 0):,}" if info.get("like_count") else "N/A"
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

        rows, pair = [], []
        for lbl, q_val in QUALITY_OPTIONS:
            if int(q_val) <= max_h:
                pair.append(InlineKeyboardButton(lbl, callback_data=f"ytq|{k}|{q_val}"))
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
        if pair:
            rows.append(pair)
        rows.append([InlineKeyboardButton("🎵 MP3 Audio (file)", callback_data=f"ytq|{k}|audio")])
        rows.append([InlineKeyboardButton("❌ Cancel",           callback_data="ytq_cancel")])

        caption = (
            f"🎬 **{title}**\n"
            f"👤 `{uploader}`\n"
            f"⏱ `{_fmt_dur(duration)}`  •  👁 `{views}`  •  👍 `{likes}`\n\n"
            "**Select a format:**"
        )

        await wait.delete()
        if thumbnail:
            await client.send_photo(
                message.chat.id, thumbnail,
                caption=caption, parse_mode=MD,
                reply_markup=InlineKeyboardMarkup(rows),
            )
        else:
            await message.reply(caption, parse_mode=MD, reply_markup=InlineKeyboardMarkup(rows))

        await log_action(client, message, "📥 YT Info", f"`{title}`")

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

        if quality == "audio":
            await _send_audio(client, query, session["url"], session["meta"])
        else:
            await _send_video_link(query, session["url"], quality)

    # ── Video link (cobalt.tools) ─────────────────────────────────────────────

    async def _send_video_link(query: CallbackQuery, url: str, quality: str):
        orig = query.message.caption or ""
        try:
            await query.message.edit_caption(
                orig + f"\n\n⏳ Getting **{quality}p** link…", parse_mode=MD
            )
        except Exception:
            pass

        dl_link = await _cobalt_link(url, quality)

        try:
            await query.message.edit_caption(
                orig, parse_mode=MD, reply_markup=query.message.reply_markup
            )
        except Exception:
            pass

        if not dl_link:
            await query.message.reply(
                f"❌ Could not get a **{quality}p** link.\n"
                "Try a different quality or try again later.",
                parse_mode=MD,
            )
            return

        await query.message.reply(
            f"✅ **{quality}p Download Link**\n\n"
            f"[⬇️ Click here to download]({dl_link})\n\n"
            "⏳ _Link is temporary — download soon!_",
            parse_mode=MD,
        )

    # ── MP3 audio download ────────────────────────────────────────────────────

    async def _send_audio(client: Client, query: CallbackQuery, url: str, meta: dict):
        title     = meta.get("title",    "Unknown")
        uploader  = meta.get("uploader", "Unknown")
        duration  = int(meta.get("duration") or 0)
        thumb_url = meta.get("thumbnail", "")
        video_id  = meta.get("video_id", "")

        try:
            await query.message.delete()
        except Exception:
            pass

        status = await client.send_message(
            query.message.chat.id,
            "🎵 **Preparing your MP3…**",
            parse_mode=MD,
        )

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
            # If cookie_path from get_cookie_file() is outside tmp, copy into tmp
            if cookie_path and not cookie_path.startswith(tmp):
                import shutil
                local_cookie = os.path.join(tmp, "cookies.txt")
                shutil.copy2(cookie_path, local_cookie)
                cookie_path = local_cookie

            hook = _make_progress_hook(status, f"⬇️ Downloading: **{title[:40]}**")

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
                    ydl.extract_info(url, download=True)

            try:
                await asyncio.to_thread(_dl)
            except Exception as e:
                err = str(e)
                if "Sign in" in err or "bot" in err.lower():
                    await status.edit(
                        "❌ **YouTube requires sign-in.**\n"
                        "Upload cookies with `/cook` and try again.",
                        parse_mode=MD,
                    )
                else:
                    await status.edit(f"❌ Download failed:\n`{err}`", parse_mode=MD)
                return

            mp3 = os.path.join(tmp, f"{video_id}.mp3")
            if not os.path.exists(mp3):
                mp3 = _find_file(tmp, "mp3")
            if not mp3:
                await status.edit("❌ Could not find the downloaded audio file.")
                return

            size_mb = os.path.getsize(mp3) / 1024 / 1024
            if size_mb > 50:
                await status.edit(f"❌ File too large ({size_mb:.1f} MB). Telegram limit is 50 MB.")
                return

            # Embed album art
            if thumb_data:
                try:
                    from mutagen.id3 import ID3, APIC, TIT2, TPE1, error as ID3Error
                    from mutagen.mp3 import MP3
                    from PIL import Image

                    img = Image.open(io.BytesIO(thumb_data))
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    jbuf = io.BytesIO()
                    img.save(jbuf, format="JPEG", quality=85)

                    audio = MP3(mp3, ID3=ID3)
                    try:
                        audio.add_tags()
                    except ID3Error:
                        pass
                    audio.tags.delall("APIC")
                    audio.tags.add(APIC(encoding=3, mime="image/jpeg",
                                        type=3, desc="Cover", data=jbuf.getvalue()))
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
                f"🎵 **{title}**\n"
                f"👤 `{uploader}`\n"
                f"⏱ `{_fmt_dur(duration)}`  •  💾 `{size_mb:.1f} MB`"
            )

            await status.edit("📤 **Uploading…**", parse_mode=MD)
            await status.delete()
            await client.send_audio(
                query.message.chat.id, mp3,
                caption=caption, parse_mode=MD,
                title=title, performer=uploader,
                duration=duration, thumb=thumb_io,
            )
