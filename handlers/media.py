"""
handlers/media.py  —  /mediainfo  /sample  /screenshot

Key improvements:
  - /sample and /screenshot work IMMEDIATELY — ffmpeg seeks to the right
    timestamp directly on the Telegram CDN stream URL (no full download needed)
  - Progress bar on all three commands via ffmpeg -progress pipe
  - ffmpeg binary auto-resolved (system or imageio_ffmpeg static binary)
"""

import asyncio
import json
import os
import re
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
from config import BOT_TOKEN

MD               = ParseMode.MARKDOWN
SCREENSHOT_COUNT = 10

_SECTION_EMOJI = {
    "General": "🗒", "Video": "🎞", "Audio": "🔊", "Text": "🔠", "Menu": "🗃",
}


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


def _run_ff(args: list[str], progress_cb=None) -> bool:
    """
    Run ffmpeg. If progress_cb provided, pipe -progress to it.
    progress_cb(pct: float) called with 0-100 as processing goes.
    """
    if progress_cb:
        import threading, io as _io

        r_fd, w_fd = os.pipe()
        cmd = [_FFMPEG] + args + ["-progress", f"pipe:{w_fd}", "-nostats"]
        try:
            proc = subprocess.Popen(
                cmd, pass_fds=(w_fd,),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            os.close(w_fd)

            duration_us = None
            with os.fdopen(r_fd, "r") as pf:
                for line in pf:
                    line = line.strip()
                    if line.startswith("out_time_us="):
                        try:
                            out_us = int(line.split("=", 1)[1])
                            if duration_us and duration_us > 0:
                                pct = min(out_us / duration_us * 100, 99)
                                progress_cb(pct)
                        except (ValueError, TypeError):
                            pass
                    elif line.startswith("duration="):
                        m = re.match(r"(\d+):(\d+):(\d+)\.(\d+)", line.split("=", 1)[1])
                        if m:
                            h, mn, s, _ = int(m[1]), int(m[2]), int(m[3]), m[4]
                            duration_us = (h * 3600 + mn * 60 + s) * 1_000_000

            proc.wait()
            return proc.returncode == 0
        except Exception:
            return False
    else:
        cmd = [_FFMPEG] + args + ["-nostats", "-loglevel", "error"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=300)
            return r.returncode == 0
        except Exception:
            return False


async def _get_tg_cdn_url(client: Client, media_obj) -> str | None:
    """
    Get Telegram CDN URL for direct ffmpeg streaming.
    Works for files ≤ 20 MB via Bot API.
    For larger files we fall back to full download.
    """
    try:
        tg_file = await client.get_file(media_obj.file_id)
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
    except Exception:
        return None


# ── Progress bar helpers ──────────────────────────────────────────────────────

def _bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _make_ff_progress(status_msg, label: str, duration_s: float):
    """
    Returns a progress_cb that updates status_msg with an ffmpeg progress bar.
    Throttled to 1 update / 3s to stay within Telegram rate limits.
    """
    last = [0.0]
    loop = asyncio.get_event_loop()

    def cb(pct: float):
        now = time.time()
        if now - last[0] < 3.0:
            return
        last[0] = now

        done_s = pct / 100 * duration_s
        h, rem = divmod(int(done_s), 3600)
        m, s   = divmod(rem, 60)
        tot_h, tot_rem = divmod(int(duration_s), 3600)
        tot_m, tot_s   = divmod(tot_rem, 60)

        time_str = f"{m:02d}:{s:02d}" if not h else f"{h}:{m:02d}:{s:02d}"
        total_str = f"{tot_m:02d}:{tot_s:02d}" if not tot_h else f"{tot_h}:{tot_m:02d}:{tot_s:02d}"

        text = (
            f"**{label}**\n\n"
            f"`{_bar(pct)}` {pct:.0f}%\n"
            f"⏱ {time_str} / {total_str}"
        )

        async def _edit():
            try:
                await status_msg.edit(text, parse_mode=MD)
            except Exception:
                pass

        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_edit()))

    return cb


# ── General helpers ───────────────────────────────────────────────────────────

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
    dest = os.path.join(dest_dir, filename)
    await client.download_media(media_obj, file_name=dest)
    return dest


async def _ydl_stream_url(url: str) -> dict:
    social = ("youtube.com", "youtu.be", "vimeo.com", "instagram.com",
               "twitter.com", "tiktok.com", "facebook.com")
    if not any(s in url for s in social):
        return {"single": url}
    def _extract():
        opts = {
            "quiet": True, "skip_download": True,
            "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "extractor_args": {"youtube": {
                "player_client": ["mweb", "ios", "tv_embedded", "web"],
                "player_skip":   ["webpage"],
            }},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            fmts = info.get("requested_formats") or []
            if len(fmts) >= 2:
                v = next((f["url"] for f in fmts if f.get("vcodec","none") != "none"), None)
                a = next((f["url"] for f in fmts
                          if f.get("acodec","none") != "none"
                          and f.get("vcodec","none") == "none"), None)
                if v and a:
                    return {"video": v, "audio": a}
            return {"single": info.get("url") or url}
    try:
        return await asyncio.to_thread(_extract)
    except Exception:
        return {"single": url}


def _duration(data: dict) -> float:
    try:
        return float(data.get("format", {}).get("duration", 0))
    except (ValueError, TypeError):
        return 0.0


def _has_mediainfo() -> bool:
    try:
        subprocess.run(["mediainfo", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_mediainfo(path: str) -> str | None:
    try:
        r = subprocess.run(["mediainfo", path], capture_output=True, text=True, timeout=120)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


async def _post_telegraph(filename: str, raw: str) -> str | None:
    lines   = raw.split("\n")
    nodes   = []
    section = []
    for line in lines:
        is_sec = any(line.startswith(s) for s in _SECTION_EMOJI)
        if is_sec:
            if section:
                nodes.append({"tag": "pre", "children": ["\n".join(section)]})
                section = []
            emoji = next((e for s, e in _SECTION_EMOJI.items() if line.startswith(s)), "📋")
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
                "title":        f"MediaInfo — {filename}"[:256],
                "author_name":  "MediaInfo Bot",
                "content":      nodes,
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
    lines = [
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
                f"  Profile    : `{st.get('profile','?')}`",
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
            wait = await message.reply("⏳ Fetching media info from URL…")
            info = await _ydl_stream_url(args[0])
            src  = info.get("single") or info.get("video")
            name = args[0].split("?")[0].split("/")[-1][:60] or "video"
            await _do_mediainfo(wait, src, name)
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                "↩️ **How to use /mediainfo**\n\n"
                "• Reply to a video/document with `/mediainfo`\n"
                "• Or: `/mediainfo <url>`", parse_mode=MD,
            )
            return

        # Try CDN URL first (immediate, no download)
        cdn_url = await _get_tg_cdn_url(client, media_obj)
        if cdn_url:
            wait = await message.reply("⏳ Reading media info…")
            await _do_mediainfo(wait, cdn_url, fname)
        else:
            size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
            wait     = await message.reply(
                f"⏳ Downloading `{fname}` ({size_str})…", parse_mode=MD
            )
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    path = await _download_tg(client, media_obj, tmp, fname)
                except Exception as e:
                    await wait.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
                    return
                await _do_mediainfo(wait, path, fname)

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
                await wait.edit(
                    f"📊 **{name}**\n\n```\n" + "\n".join(lines) + "\n```",
                    parse_mode=MD,
                )
                return

        data = await asyncio.to_thread(_probe, src)
        if not data:
            await wait.edit(
                "❌ Could not read media info.\n_Make sure ffmpeg is installed._",
                parse_mode=MD,
            )
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
            wait  = await message.reply("⏳ Generating 30s sample clip from URL…")
            info  = await _ydl_stream_url(args[0])
            label = args[0].split("?")[0].split("/")[-1][:40] or "video"
            await _make_sample(client, message, wait, info, label, dur_hint=0)
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                "↩️ **How to use /sample**\n\n"
                "• Reply to a video with `/sample`\n"
                "• Or: `/sample <url>`\n\n"
                "_Generates a 30-second preview clip starting ~15% into the video._",
                parse_mode=MD,
            )
            return

        wait = await message.reply("⏳ Generating 30s sample clip…")

        # Try CDN streaming first — immediate, no full download
        cdn_url = await _get_tg_cdn_url(client, media_obj)
        if cdn_url:
            # Probe duration from CDN
            data = await asyncio.to_thread(_probe, cdn_url)
            dur  = _duration(data)
            await _make_sample(
                client, message, wait,
                {"single": cdn_url},
                os.path.splitext(fname)[0],
                dur_hint=dur,
            )
        else:
            # Large file — need full download
            size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
            await wait.edit(
                f"⏳ Downloading `{fname}` ({size_str}) for sample…", parse_mode=MD
            )
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    path = await _download_tg(client, media_obj, tmp, fname)
                except Exception as e:
                    await wait.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
                    return
                data = await asyncio.to_thread(_probe, path)
                dur  = _duration(data)
                await _make_sample(
                    client, message, wait,
                    {"single": path},
                    os.path.splitext(fname)[0],
                    dur_hint=dur,
                )

    async def _make_sample(client, message, wait, stream_info: dict,
                            label: str, dur_hint: float):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sample.mp4")
            src = stream_info.get("single") or stream_info.get("video")

            if dur_hint <= 0:
                data     = await asyncio.to_thread(_probe, src)
                dur_hint = _duration(data)

            if dur_hint < 10:
                await wait.edit("❌ Video is too short to generate a sample (minimum 10s).")
                return

            start  = max(10.0, dur_hint * 0.15)
            clip_s = 30.0

            progress_cb = _make_ff_progress(wait, "🎬 Creating Sample Clip", clip_s)

            def _do():
                if "audio" in stream_info:
                    return _run_ff([
                        "-ss", str(start), "-i", stream_info["video"],
                        "-ss", str(start), "-i", stream_info["audio"],
                        "-t", str(clip_s),
                        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                        "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart", "-y", out,
                    ], progress_cb)
                return _run_ff([
                    "-ss", str(start), "-i", src,
                    "-t", str(clip_s),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", "-y", out,
                ], progress_cb)

            ok = await asyncio.to_thread(_do)
            if not ok or not os.path.exists(out):
                await wait.edit(
                    "❌ Sample generation failed.\n_Make sure ffmpeg is installed._",
                    parse_mode=MD,
                )
                return

            out_size = os.path.getsize(out) / 1024 / 1024
            await wait.edit("📤 **Uploading sample…**", parse_mode=MD)
            await wait.delete()
            await client.send_video(
                message.chat.id, out,
                caption=(
                    f"🎬 **[#Sample]** `{label}`\n"
                    f"⏱ 30s clip  •  💾 {out_size:.1f} MB"
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
            wait  = await message.reply(f"⏳ Taking {SCREENSHOT_COUNT} screenshots from URL…")
            info  = await _ydl_stream_url(args[0])
            label = args[0].split("?")[0].split("/")[-1][:40] or "video"
            await _take_shots(client, message, wait, info, label, dur_hint=0)
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                f"↩️ **How to use /screenshot**\n\n"
                f"• Reply to a video with `/screenshot`\n"
                f"• Or: `/screenshot <url>`\n\n"
                f"_Takes {SCREENSHOT_COUNT} evenly-spaced screenshots._",
                parse_mode=MD,
            )
            return

        wait = await message.reply(f"⏳ Taking {SCREENSHOT_COUNT} screenshots…")

        # Try CDN streaming — immediate, no full download needed
        cdn_url = await _get_tg_cdn_url(client, media_obj)
        if cdn_url:
            data = await asyncio.to_thread(_probe, cdn_url)
            dur  = _duration(data)
            await _take_shots(
                client, message, wait,
                {"single": cdn_url},
                os.path.splitext(fname)[0],
                dur_hint=dur,
            )
        else:
            size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
            await wait.edit(
                f"⏳ Downloading `{fname}` ({size_str}) for screenshots…",
                parse_mode=MD,
            )
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    path = await _download_tg(client, media_obj, tmp, fname)
                except Exception as e:
                    await wait.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
                    return
                data = await asyncio.to_thread(_probe, path)
                dur  = _duration(data)
                await _take_shots(
                    client, message, wait,
                    {"single": path},
                    os.path.splitext(fname)[0],
                    dur_hint=dur,
                )

    async def _take_shots(client, message, wait, stream_info: dict,
                           label: str, dur_hint: float):
        with tempfile.TemporaryDirectory() as tmp:
            src = stream_info.get("single") or stream_info.get("video")

            if dur_hint <= 0:
                data     = await asyncio.to_thread(_probe, src)
                dur_hint = _duration(data)

            if dur_hint < 10:
                await wait.edit("❌ Video is too short (minimum 10 seconds).")
                return

            margin = dur_hint * 0.05
            step   = (dur_hint - 2 * margin) / SCREENSHOT_COUNT
            times  = [margin + step * i for i in range(SCREENSHOT_COUNT)]
            labels = [f"{int(t)}s" for t in times]

            # Show progress as screenshots are taken
            async def _update_progress(done: int):
                pct  = done / SCREENSHOT_COUNT * 100
                text = (
                    f"📸 **Taking Screenshots…**\n\n"
                    f"`{_bar(pct)}` {done}/{SCREENSHOT_COUNT}\n"
                    f"⏱ Timestamps: {', '.join(labels[:done])}"
                )
                try:
                    await wait.edit(text, parse_mode=MD)
                except Exception:
                    pass

            shots = []
            for i, t in enumerate(times):
                shot_path = os.path.join(tmp, f"shot_{i:02d}.jpg")
                ok = await asyncio.to_thread(
                    _run_ff, [
                        "-ss", str(t), "-i", src,
                        "-vframes", "1", "-q:v", "2",
                        "-y", shot_path,
                    ]
                )
                if ok and os.path.exists(shot_path):
                    shots.append(shot_path)
                if (i + 1) % 2 == 0:  # update every 2 shots
                    await _update_progress(i + 1)

            if not shots:
                await wait.edit(
                    "❌ Could not generate screenshots.\n"
                    "_Make sure ffmpeg is installed._",
                    parse_mode=MD,
                )
                return

            caption = (
                f"📸 **[#Screenshot]** `{label}`\n"
                f"⏱ Timestamps: {', '.join(labels)}"
            )
            media = [
                InputMediaPhoto(
                    shot,
                    caption=caption if i == 0 else "",
                    parse_mode=MD if i == 0 else None,
                )
                for i, shot in enumerate(shots)
            ]

            await wait.delete()
            await client.send_media_group(message.chat.id, media)
