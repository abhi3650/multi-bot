"""
handlers/media.py — /mediainfo /sample /screenshot

Architecture for instant results (< 30 seconds even for 5-hour files):

/mediainfo:
  - Files ≤ 20MB: Bot API CDN URL → ffprobe directly on stream (no download)
  - Files > 20MB: Generate /link permanent URL → ffprobe on stream URL

/sample & /screenshot:
  - NEVER download the file first
  - Strategy A (files ≤ 20MB): Bot API CDN URL → ffmpeg -ss seek on stream
  - Strategy B (files > 20MB): Forward to DUMP_CHANNEL → get permanent stream URL
                                → ffmpeg -ss seek on stream URL
  - ffmpeg -ss BEFORE -i = keyframe seek, instant for any file size
  - Screenshots: all 10 taken CONCURRENTLY with asyncio.gather

Key ffmpeg flags for speed:
  -ss <t> -i <url>   : seek before opening (instant, no buffering)
  -frames:v 1        : take exactly one frame and stop
  -preset ultrafast  : fastest encoding for sample clip
  -threads 0         : use all available CPU cores
"""

import asyncio
import json
import os
import random
import subprocess
import tempfile
import time

import httpx
import yt_dlp
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, InputMediaPhoto,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from config import BOT_TOKEN, STREAM_BASE_URL, DUMP_CHANNEL
from logger import log_action

MD               = ParseMode.MARKDOWN
SCREENSHOT_COUNT = 10
_PLAYER_CLIENTS  = ["android", "tv_embedded"]


# ── ffmpeg binary resolution ──────────────────────────────────────────────────

def _find_ffmpeg() -> tuple[str, str]:
    import shutil
    ff  = shutil.which("ffmpeg")
    ffp = shutil.which("ffprobe")
    if ff and ffp:
        return ff, ffp
    try:
        import imageio_ffmpeg as ioff
        ff  = ioff.get_ffmpeg_exe()
        ffp = os.path.join(os.path.dirname(ff), "ffprobe")
        if not os.path.exists(ffp):
            ffp = ff
        return ff, ffp
    except Exception:
        pass
    return "ffmpeg", "ffprobe"


_FFMPEG, _FFPROBE = _find_ffmpeg()


def _probe(src: str) -> dict:
    """Probe via ffprobe/ffmpeg. Works on HTTP URLs (no download, seeks in)."""
    exe = _FFPROBE
    if exe == _FFMPEG:
        cmd = [_FFMPEG, "-hide_banner", "-v", "quiet",
               "-print_format", "json", "-show_format", "-show_streams", "-i", src]
    else:
        cmd = [exe, "-v", "quiet", "-print_format", "json",
               "-show_format", "-show_streams", src]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for candidate in (r.stdout, r.stderr):
            if candidate and candidate.strip().startswith("{"):
                return json.loads(candidate)
    except Exception:
        pass
    return {}


def _duration(data: dict) -> float:
    try:
        return float(data.get("format", {}).get("duration", 0))
    except (ValueError, TypeError):
        return 0.0


def _bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _run_ff(args: list[str]) -> tuple[bool, str]:
    """Run ffmpeg. Returns (success, stderr_output)."""
    cmd = [_FFMPEG] + args + ["-nostats", "-loglevel", "warning"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode == 0, r.stderr
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


# ── Permanent stream URL helpers ──────────────────────────────────────────────

async def _get_stream_url(client: Client, media_obj, message: Message) -> tuple[str | None, str]:
    """
    Get a streamable URL for ffmpeg to seek on — no download needed.

    For files ≤ 20 MB: use Bot API CDN URL (instant)
    For files > 20 MB: forward to DUMP_CHANNEL and get permanent stream URL
                       (same as /link but automated)

    Returns (stream_url, display_name)
    """
    file_size = getattr(media_obj, "file_size", 0) or 0
    file_name = getattr(media_obj, "file_name", None) or "video"
    mime_type = getattr(media_obj, "mime_type", None) or "video/mp4"

    # Strategy A: small file — use Bot API CDN
    if file_size <= 20 * 1024 * 1024:
        try:
            tg_file = await client.get_file(media_obj.file_id)
            if tg_file.file_path:
                path = tg_file.file_path
                if path.startswith("http"):
                    return path, file_name
                return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", file_name
        except Exception:
            pass

    # Strategy B: large file — store in DUMP_CHANNEL, get permanent URL
    if not STREAM_BASE_URL or not DUMP_CHANNEL:
        return None, file_name

    try:
        dump_msg = await client.forward_messages(
            chat_id      = DUMP_CHANNEL,
            from_chat_id = message.chat.id,
            message_ids  = message.reply_to_message.id,
        )
        dump_media  = dump_msg.video or dump_msg.document or dump_msg.audio
        stream_file_id = getattr(dump_media, "file_id", media_obj.file_id)
        file_uid    = getattr(media_obj, "file_unique_id", "")

        mongo_id = await db.add_link_file(
            tg_file_id        = stream_file_id,
            tg_file_unique_id = file_uid,
            file_name         = file_name,
            file_size         = file_size,
            mime_type         = mime_type,
            dump_msg_id       = dump_msg.id,
        )
        stream_url = f"{STREAM_BASE_URL.rstrip('/')}/dl/{mongo_id}"
        return stream_url, file_name
    except Exception as e:
        return None, file_name


# ── Helpers ───────────────────────────────────────────────────────────────────

def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


def _limit_err(allowed: bool, used: int, limit: int) -> str | None:
    if not allowed:
        return (
            f"⚠️ You've used **{used}/{limit}** media operations this month.\n"
            "Upgrade to /premium for unlimited access!"
        )
    return None


def _get_video_from_reply(message: Message):
    r = message.reply_to_message
    if not r:
        return None, None, 0
    if r.video:
        return r.video, r.video.file_name or "video.mp4", r.video.file_size or 0
    if r.document and (r.document.mime_type or "").startswith("video"):
        return r.document, r.document.file_name or "video.mkv", r.document.file_size or 0
    return None, None, 0


async def _download_tg(client: Client, media_obj, dest_dir: str, filename: str) -> str:
    """Full MTProto download — only used when streaming fails completely."""
    dest = os.path.join(dest_dir, filename)
    await client.download_media(media_obj, file_name=dest)
    return dest


def _has_mediainfo() -> bool:
    try:
        subprocess.run(["mediainfo", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_mediainfo(path: str) -> str | None:
    try:
        r = subprocess.run(["mediainfo", path], capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


async def _post_telegraph(filename: str, raw: str) -> str | None:
    _EMOJI = {"General": "🗒", "Video": "🎞", "Audio": "🔊", "Text": "🔠", "Menu": "🗃"}
    lines, nodes, section = raw.split("\n"), [], []
    for line in lines:
        is_sec = any(line.startswith(s) for s in _EMOJI)
        if is_sec:
            if section:
                nodes.append({"tag": "pre", "children": ["\n".join(section)]})
                section = []
            emoji = next((e for s, e in _EMOJI.items() if line.startswith(s)), "📋")
            nodes.append({"tag": "h4", "children": [f"{emoji} {line}"]})
        elif line.strip():
            section.append(line)
    if section:
        nodes.append({"tag": "pre", "children": ["\n".join(section)]})
    try:
        async with httpx.AsyncClient(timeout=20) as hx:
            acc  = await hx.get("https://api.telegra.ph/createAccount",
                                 params={"short_name": "MediaBot", "author_name": "MediaInfo"})
            tok  = acc.json()["result"]["access_token"]
            page = await hx.post("https://api.telegra.ph/createPage", json={
                "access_token": tok,
                "title": f"MediaInfo — {filename}"[:256],
                "author_name": "MediaInfo Bot",
                "content": nodes,
            })
            return f"https://telegra.ph/{page.json()['result']['path']}"
    except Exception:
        return None


def _build_ffprobe_text(data: dict, name: str) -> str:
    fmt     = data.get("format", {})
    dur     = float(fmt.get("duration", 0))
    h, rem  = divmod(int(dur), 3600)
    m, s    = divmod(rem, 60)
    size_mb = int(fmt.get("size", 0)) / 1024 / 1024
    bitrate = int(fmt.get("bit_rate", 0)) // 1000
    lines   = [
        f"📊 **MediaInfo — {name}**\n",
        f"⏱ Duration : `{h:02d}:{m:02d}:{s:02d}`",
        f"💾 Size     : `{size_mb:.2f} MB`",
        f"📡 Bitrate  : `{bitrate} kbps`\n",
    ]
    for st in data.get("streams", []):
        ct = st.get("codec_type", "")
        if ct == "video":
            lines += [
                "🎥 **Video Stream**",
                f"  Codec      : `{st.get('codec_name','?').upper()}`",
                f"  Resolution : `{st.get('width','?')}×{st.get('height','?')}`",
                f"  FPS        : `{st.get('avg_frame_rate','?')}`",
                f"  Bitrate    : `{int(st.get('bit_rate',0))//1000} kbps`\n",
            ]
        elif ct == "audio":
            lines += [
                "🔊 **Audio Stream**",
                f"  Codec       : `{st.get('codec_name','?').upper()}`",
                f"  Channels    : `{st.get('channels','?')}`",
                f"  Sample Rate : `{st.get('sample_rate','?')} Hz`",
                f"  Bitrate     : `{int(st.get('bit_rate',0))//1000} kbps`\n",
            ]
        elif ct == "subtitle":
            lang = st.get("tags", {}).get("language", "?")
            lines.append(f"💬 Subtitle : `{st.get('codec_name','?')} ({lang})`")
    return "\n".join(lines)


async def _ydl_stream_url(url: str) -> dict:
    from cookie_helper import get_cookie_file
    social = ("youtube.com", "youtu.be", "vimeo.com", "instagram.com",
               "twitter.com", "tiktok.com", "facebook.com")
    if not any(s in url for s in social):
        return {"single": url}

    _cookie_path = await get_cookie_file()

    def _extract():
        client = ["web", "android"] if _cookie_path else ["android", "tv_embedded"]
        opts = {
            "quiet": True, "skip_download": True,
            "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "http_headers": {
                "User-Agent": "com.google.android.youtube/17.36.4 (Linux; U; Android 13) gzip",
            },
            "extractor_args": {"youtube": {"player_client": client}},
        }
        if _cookie_path:
            opts["cookiefile"] = _cookie_path
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            fmts = info.get("requested_formats") or []
            if len(fmts) >= 2:
                v = next((f["url"] for f in fmts if f.get("vcodec", "none") != "none"), None)
                a = next((f["url"] for f in fmts
                          if f.get("acodec", "none") != "none"
                          and f.get("vcodec", "none") == "none"), None)
                if v and a:
                    return {"video": v, "audio": a}
            return {"single": info.get("url") or url}
    try:
        return await asyncio.to_thread(_extract)
    except Exception:
        return {"single": url}


# ── Handler registration ──────────────────────────────────────────────────────

def register(app: Client):

    # ── /mediainfo ────────────────────────────────────────────────────────────
    @app.on_message(filters.command("mediainfo") & filters.private)
    async def cmd_mediainfo(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))
        allowed, used, limit = await db.check_and_consume(u.id, "media")
        if err := _limit_err(allowed, used, limit):
            await message.reply(err, parse_mode=MD)
            return

        args = message.command[1:]
        if args and args[0].startswith("http"):
            wait = await message.reply("⏳ Reading media info from URL…")
            info = await _ydl_stream_url(args[0])
            src  = info.get("single") or info.get("video")
            name = args[0].split("?")[0].split("/")[-1][:60] or "video"
            await _do_mediainfo(wait, src, name)
            await log_action(client, message, "📊 MediaInfo", f"URL `{name}`")
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                "↩️ **How to use /mediainfo**\n\n"
                "• Reply to a video/document with `/mediainfo`\n"
                "• Or: `/mediainfo <url>`",
                parse_mode=MD,
            )
            return

        wait = await message.reply("⏳ Reading media info…")

        # Get stream URL (no download needed)
        stream_url, _ = await _get_stream_url(client, media_obj, message)
        if stream_url:
            await _do_mediainfo(wait, stream_url, fname)
        else:
            # Last resort: full download
            size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "?"
            await wait.edit(f"⏳ Downloading `{fname}` ({size_str})…", parse_mode=MD)
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    path = await _download_tg(client, media_obj, tmp, fname)
                except Exception as e:
                    await wait.edit(f"❌ Failed:\n`{e}`", parse_mode=MD)
                    return
                await _do_mediainfo(wait, path, fname)

        await log_action(client, message, "📊 MediaInfo", f"`{fname}`")

    async def _do_mediainfo(wait, src: str, name: str):
        if _has_mediainfo():
            raw = await asyncio.to_thread(_run_mediainfo, src)
            if raw:
                tg_url = await _post_telegraph(name, raw)
                if tg_url:
                    await wait.edit(
                        f"📊 **MediaInfo — {name}**\n\nFull report ready 👇",
                        parse_mode=MD,
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📄 View Full Report", url=tg_url)
                        ]]),
                    )
                    return
                lines = [l for l in raw.split("\n") if l.strip()][:60]
                await wait.edit(f"📊 **{name}**\n\n```\n" + "\n".join(lines) + "\n```", parse_mode=MD)
                return
        data = await asyncio.to_thread(_probe, src)
        if not data:
            await wait.edit("❌ Could not read media info.", parse_mode=MD)
            return
        await wait.edit(_build_ffprobe_text(data, name), parse_mode=MD)

    # ── /sample ───────────────────────────────────────────────────────────────
    @app.on_message(filters.command("sample") & filters.private)
    async def cmd_sample(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))
        allowed, used, limit = await db.check_and_consume(u.id, "media")
        if err := _limit_err(allowed, used, limit):
            await message.reply(err, parse_mode=MD)
            return

        args = message.command[1:]
        if args and args[0].startswith("http"):
            wait  = await message.reply("⏳ Generating 30s sample clip…")
            src   = args[0]
            label = src.split("?")[0].split("/")[-1][:40] or "video"
            await _make_sample(client, message, wait, src, label)
            await log_action(client, message, "🎬 Sample", f"URL `{label}`")
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                "↩️ **How to use /sample**\n\n"
                "• Reply to a video with `/sample`\n"
                "• Or: `/sample <url>`\n\n"
                "_Generates a random 30-second clip instantly — no full download._",
                parse_mode=MD,
            )
            return

        size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "?"
        wait     = await message.reply(
            f"⏳ Generating 30s sample clip from `{fname}` ({size_str})…",
            parse_mode=MD,
        )

        # Get stream URL — avoids full download
        stream_url, label = await _get_stream_url(client, media_obj, message)
        if not stream_url:
            await wait.edit(
                "❌ Could not get stream URL.\n"
                "Make sure `STREAM_BASE_URL` and `DUMP_CHANNEL` are configured.",
                parse_mode=MD,
            )
            return

        await _make_sample(client, message, wait, stream_url, os.path.splitext(fname)[0])
        await log_action(client, message, "🎬 Sample", f"`{fname}`")

    async def _make_sample(client: Client, message: Message, wait, src: str, label: str):
        """
        Generate a 30s sample clip by seeking directly on the stream URL.
        Uses -ss BEFORE -i for instant keyframe seek — no buffering.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sample.mp4")

            # Quick duration probe (5s timeout) for smarter seek point
            dur = 0.0
            try:
                data = await asyncio.wait_for(asyncio.to_thread(_probe, src), timeout=8.0)
                dur  = _duration(data)
            except Exception:
                pass

            if dur > 60:
                start = random.uniform(dur * 0.10, dur * 0.75)
            elif dur > 10:
                start = random.uniform(5.0, dur * 0.7)
            else:
                # Unknown duration — start at random offset
                start = random.uniform(60, 300)

            clip_s    = 30.0
            start_str = f"{int(start // 60)}:{int(start % 60):02d}"

            await wait.edit(
                f"🎬 Creating 30s clip from `{start_str}`…",
                parse_mode=MD,
            )

            # -ss BEFORE -i = instant keyframe seek (no buffering)
            # -threads 0 = use all CPU cores
            # -preset ultrafast = fastest encoding
            ok, err = await asyncio.to_thread(_run_ff, [
                "-ss", str(start),
                "-i", src,
                "-t", str(clip_s),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                "-c:a", "aac", "-b:a", "96k",
                "-threads", "0",
                "-movflags", "+faststart",
                "-y", out,
            ])

            if not ok or not os.path.exists(out):
                await wait.edit(
                    f"❌ Sample generation failed.\n`{err[:200]}`",
                    parse_mode=MD,
                )
                return

            out_mb = os.path.getsize(out) / 1024 / 1024
            await wait.delete()
            await client.send_video(
                message.chat.id, out,
                caption=(
                    f"🎬 **[#Sample]** `{label}`\n"
                    f"⏱ 30s from `{start_str}`  •  💾 {out_mb:.1f} MB"
                ),
                parse_mode=MD, supports_streaming=True,
            )

    # ── /screenshot ───────────────────────────────────────────────────────────
    @app.on_message(filters.command("screenshot") & filters.private)
    async def cmd_screenshot(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))
        allowed, used, limit = await db.check_and_consume(u.id, "media")
        if err := _limit_err(allowed, used, limit):
            await message.reply(err, parse_mode=MD)
            return

        args = message.command[1:]
        if args and args[0].startswith("http"):
            wait  = await message.reply(f"⏳ Taking {SCREENSHOT_COUNT} screenshots…")
            src   = args[0]
            label = src.split("?")[0].split("/")[-1][:40] or "video"
            await _take_shots(client, message, wait, src, label)
            await log_action(client, message, "📸 Screenshot", f"URL `{label}`")
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                f"↩️ **How to use /screenshot**\n\n"
                f"• Reply to a video with `/screenshot`\n"
                f"• Or: `/screenshot <url>`\n\n"
                f"_Takes {SCREENSHOT_COUNT} random screenshots instantly._",
                parse_mode=MD,
            )
            return

        size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "?"
        wait     = await message.reply(
            f"⏳ Taking {SCREENSHOT_COUNT} screenshots from `{fname}` ({size_str})…",
            parse_mode=MD,
        )

        stream_url, label = await _get_stream_url(client, media_obj, message)
        if not stream_url:
            await wait.edit(
                "❌ Could not get stream URL.\n"
                "Make sure `STREAM_BASE_URL` and `DUMP_CHANNEL` are configured.",
                parse_mode=MD,
            )
            return

        await _take_shots(client, message, wait, stream_url, os.path.splitext(fname)[0])
        await log_action(client, message, "📸 Screenshot", f"`{fname}`")

    async def _take_shots(client: Client, message: Message, wait, src: str, label: str):
        """
        Take SCREENSHOT_COUNT screenshots at random timestamps.
        All shots run CONCURRENTLY — total time = time of slowest single seek.
        Each uses -ss BEFORE -i = instant keyframe seek, no buffering.
        """
        # Quick duration probe
        dur = 0.0
        try:
            data = await asyncio.wait_for(asyncio.to_thread(_probe, src), timeout=8.0)
            dur  = _duration(data)
        except Exception:
            pass

        if dur > 30:
            margin = dur * 0.03
            step   = (dur - 2 * margin) / SCREENSHOT_COUNT
            times  = [margin + step * i + random.uniform(0, step * 0.4)
                      for i in range(SCREENSHOT_COUNT)]
        else:
            # Unknown duration — spread random times over 10s–3h range
            max_t = max(dur * 0.9 if dur > 0 else 10800, 60)
            times = sorted(random.uniform(5, max_t) for _ in range(SCREENSHOT_COUNT))

        labels = [f"{int(t // 60)}:{int(t % 60):02d}" for t in times]

        await wait.edit(
            f"📸 Taking **{SCREENSHOT_COUNT}** screenshots…",
            parse_mode=MD,
        )

        with tempfile.TemporaryDirectory() as tmp:

            def _take_one(i: int, t: float) -> str | None:
                path = os.path.join(tmp, f"shot_{i:02d}.jpg")
                # -ss BEFORE -i = instant keyframe seek
                ok, _ = _run_ff([
                    "-ss", str(t),
                    "-i", src,
                    "-frames:v", "1",
                    "-q:v", "3",
                    "-threads", "0",
                    "-y", path,
                ])
                return path if ok and os.path.exists(path) else None

            # All 10 shots concurrently
            results = await asyncio.gather(
                *[asyncio.to_thread(_take_one, i, t) for i, t in enumerate(times)]
            )
            shots = [(labels[i], path) for i, path in enumerate(results) if path]

            if not shots:
                await wait.edit("❌ Could not generate any screenshots.", parse_mode=MD)
                return

            caption = (
                f"📸 **[#Screenshot]** `{label}`\n"
                f"⏱ {', '.join(l for l, _ in shots)}"
            )
            media = [
                InputMediaPhoto(
                    path,
                    caption=caption if i == 0 else "",
                    parse_mode=MD if i == 0 else None,
                )
                for i, (_, path) in enumerate(shots)
            ]

            await wait.delete()
            await client.send_media_group(message.chat.id, media)
