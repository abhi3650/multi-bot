"""
handlers/media.py  —  /mediainfo  /sample  /screenshot

Instant results strategy:
  - ALL commands use the Telegram CDN URL directly (via get_file Bot API call)
  - ffmpeg -ss seeks directly on the stream — no full download needed
  - /screenshot uses random timestamps — completely instant for any file size
  - /sample seeks to a random position — no duration probing needed
  - Full MTProto download only as fallback for files > 20 MB

Progress bar: ffmpeg -progress pipe → live Telegram message
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
from config import BOT_TOKEN
from cookie_helper import get_cookie_file
from logger import log_action

MD               = ParseMode.MARKDOWN
SCREENSHOT_COUNT = 10
_PLAYER_CLIENTS  = ["mweb", "ios", "tv_embedded", "web"]


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
    """Probe media info. Works with URLs (CDN streaming) and local files."""
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


def _run_ff(args: list[str], progress_cb=None) -> bool:
    """Run ffmpeg with optional progress reporting via pipe."""
    if progress_cb:
        import threading

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
                                progress_cb(min(out_us / duration_us * 100, 99))
                        except (ValueError, TypeError):
                            pass
                    elif line.startswith("duration="):
                        import re
                        m = re.match(r"(\d+):(\d+):(\d+)", line.split("=", 1)[1])
                        if m:
                            duration_us = (int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])) * 1_000_000

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


# ── CDN URL helper ────────────────────────────────────────────────────────────

async def _get_cdn_url(client: Client, media_obj) -> str | None:
    """
    Get a direct streamable URL for the file.

    Strategy:
    1. Try Bot API getFile (works ≤ 20 MB, instant)
    2. For larger files: use Pyrogram MTProto get_file which works for any size
       Pyrogram returns the file_path URL directly when using its own get_file.

    Returns None only if both methods fail.
    """
    try:
        tg_file = await client.get_file(media_obj.file_id)
        if tg_file.file_path:
            # Pyrogram may return either a relative path or a full HTTPS URL
            if tg_file.file_path.startswith("http"):
                return tg_file.file_path
            return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
    except Exception:
        pass

    # Fallback: try via the Pyrogram MTProto client (handles any file size)
    try:
        import pyrogram_helper as pyro
        pyro_client = pyro.get_client()
        if pyro_client:
            tg_file2 = await pyro_client.get_file(media_obj.file_id)
            if tg_file2 and tg_file2.file_path:
                if tg_file2.file_path.startswith("http"):
                    return tg_file2.file_path
                return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file2.file_path}"
    except Exception:
        pass

    return None


async def _download_tg(client: Client, media_obj, dest_dir: str, filename: str) -> str:
    """Full MTProto download — no size limit. Used only when CDN fails."""
    dest = os.path.join(dest_dir, filename)
    await client.download_media(media_obj, file_name=dest)
    return dest


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


def _make_ff_progress(status_msg, label: str, clip_s: float):
    last = [0.0]
    loop = asyncio.get_event_loop()

    def cb(pct: float):
        now = time.time()
        if now - last[0] < 3.0:
            return
        last[0] = now

        done_s    = pct / 100 * clip_s
        m, s      = divmod(int(done_s), 60)
        tot_m, tot_s = divmod(int(clip_s), 60)

        text = (
            f"**{label}**\n\n"
            f"`{_bar(pct)}` {pct:.0f}%\n"
            f"⏱ {m:02d}:{s:02d} / {tot_m:02d}:{tot_s:02d}"
        )

        async def _edit():
            try:
                await status_msg.edit(text, parse_mode=MD)
            except Exception:
                pass

        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_edit()))

    return cb


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
    _SECTION_EMOJI = {"General": "🗒", "Video": "🎞", "Audio": "🔊", "Text": "🔠", "Menu": "🗃"}
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


async def _ydl_stream_url(url: str) -> dict:
    social = ("youtube.com", "youtu.be", "vimeo.com", "instagram.com",
               "twitter.com", "tiktok.com", "facebook.com")
    if not any(s in url for s in social):
        return {"single": url}

    _cookie_path = await get_cookie_file()

    def _extract():
        opts = {
            "quiet": True, "skip_download": True,
            "format": "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "extractor_args": {"youtube": {
                "player_client": _PLAYER_CLIENTS,
                "player_skip":   ["webpage"],
            }},
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
            await log_action(client, message, "📊 MediaInfo", f"URL: `{name}`")
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                "↩️ **How to use /mediainfo**\n\n"
                "• Reply to a video/document with `/mediainfo`\n"
                "• Or: `/mediainfo <url>`", parse_mode=MD,
            )
            return

        wait    = await message.reply("⏳ Reading media info…")
        cdn_url = await _get_cdn_url(client, media_obj)

        if cdn_url:
            # Instant — probe directly from CDN URL
            await _do_mediainfo(wait, cdn_url, fname)
        else:
            # Large file — must download
            size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
            await wait.edit(f"⏳ Downloading `{fname}` ({size_str})…", parse_mode=MD)
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    path = await _download_tg(client, media_obj, tmp, fname)
                except Exception as e:
                    await wait.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
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
            await wait.edit("❌ Could not read media info. Make sure ffmpeg is installed.", parse_mode=MD)
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
            info  = await _ydl_stream_url(args[0])
            label = args[0].split("?")[0].split("/")[-1][:40] or "video"
            await _make_sample(client, message, wait, info, label)
            await log_action(client, message, "🎬 Sample", f"URL: `{label}`")
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                "↩️ **How to use /sample**\n\n"
                "• Reply to a video with `/sample`\n"
                "• Or: `/sample <url>`\n\n"
                "_Picks a random 30-second clip instantly._",
                parse_mode=MD,
            )
            return

        wait    = await message.reply("⏳ Generating 30s sample clip…")
        cdn_url = await _get_cdn_url(client, media_obj)

        if cdn_url:
            # Instant: seek directly on CDN stream
            await _make_sample(client, message, wait, {"single": cdn_url},
                                os.path.splitext(fname)[0])
        else:
            # Large file fallback
            size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
            await wait.edit(f"⏳ Downloading `{fname}` ({size_str})…", parse_mode=MD)
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    path = await _download_tg(client, media_obj, tmp, fname)
                except Exception as e:
                    await wait.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
                    return
                await _make_sample(client, message, wait,
                                    {"single": path}, os.path.splitext(fname)[0])

        await log_action(client, message, "🎬 Sample", f"`{fname}`")

    async def _make_sample(client, message, wait, stream_info: dict, label: str):
        """
        Generate a 30s sample using a RANDOM start point.
        No duration probing needed — we just pick a random time and seek.
        If the file is shorter than expected, ffmpeg handles it gracefully.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sample.mp4")
            src = stream_info.get("single") or stream_info.get("video")

            # Try to probe duration for smarter seeking, but don't block on it
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(_probe, src), timeout=5.0
                )
                dur = _duration(data)
            except Exception:
                dur = 0

            if dur > 60:
                # Pick a random start point in 10%–80% of the video
                start = random.uniform(dur * 0.10, dur * 0.80)
            elif dur > 10:
                start = random.uniform(5, dur * 0.7)
            else:
                # Unknown duration — try starting at a random offset (60–600s)
                start = random.uniform(60, 600)

            clip_s      = 30.0
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
            start_str = f"{int(start//60)}:{int(start%60):02d}"
            await wait.delete()
            await client.send_video(
                message.chat.id, out,
                caption=(
                    f"🎬 **[#Sample]** `{label}`\n"
                    f"⏱ 30s from `{start_str}`  •  💾 {out_size:.1f} MB"
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
            info  = await _ydl_stream_url(args[0])
            label = args[0].split("?")[0].split("/")[-1][:40] or "video"
            await _take_shots(client, message, wait, info, label)
            await log_action(client, message, "📸 Screenshot", f"URL: `{label}`")
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

        wait    = await message.reply(f"⏳ Taking {SCREENSHOT_COUNT} screenshots…")
        cdn_url = await _get_cdn_url(client, media_obj)

        if cdn_url:
            # Instant: seek random timestamps directly on CDN stream
            await _take_shots(client, message, wait,
                               {"single": cdn_url}, os.path.splitext(fname)[0])
        else:
            size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
            await wait.edit(f"⏳ Downloading `{fname}` ({size_str})…", parse_mode=MD)
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    path = await _download_tg(client, media_obj, tmp, fname)
                except Exception as e:
                    await wait.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
                    return
                await _take_shots(client, message, wait,
                                   {"single": path}, os.path.splitext(fname)[0])

        await log_action(client, message, "📸 Screenshot", f"`{fname}`")

    async def _take_shots(client, message, wait, stream_info: dict, label: str):
        """
        Take SCREENSHOT_COUNT screenshots at RANDOM timestamps.

        Key insight: we never need to know the video duration.
        We generate random timestamps spread over a wide range and
        let ffmpeg seek to each one. If the timestamp is beyond EOF,
        ffmpeg simply produces no frame — we skip those results.

        For a 5-hour movie, timestamps of 60s, 1800s, 3600s, etc. all work instantly
        because ffmpeg uses keyframe seeking (-ss before -i = fast seek).
        """
        src = stream_info.get("single") or stream_info.get("video")

        # Try quick duration probe (5s timeout) for smarter spread
        dur = 0.0
        try:
            data = await asyncio.wait_for(asyncio.to_thread(_probe, src), timeout=5.0)
            dur  = _duration(data)
        except Exception:
            pass

        if dur > 30:
            # Spread across the video with some randomness
            margin  = dur * 0.03
            step    = (dur - 2 * margin) / SCREENSHOT_COUNT
            times   = [margin + step * i + random.uniform(0, step * 0.5)
                       for i in range(SCREENSHOT_COUNT)]
        else:
            # Unknown duration — use random offsets spread from 0 to ~3 hours
            # ffmpeg will gracefully handle timestamps beyond EOF
            max_t = max(dur * 0.95 if dur > 0 else 10800, 300)
            times = sorted(random.uniform(10, max_t) for _ in range(SCREENSHOT_COUNT))

        labels = [f"{int(t//60)}:{int(t%60):02d}" for t in times]

        await wait.edit(
            f"📸 Taking **{SCREENSHOT_COUNT}** screenshots at random timestamps…",
            parse_mode=MD,
        )

        with tempfile.TemporaryDirectory() as tmp:
            async def _shot(i: int, t: float) -> str | None:
                path = os.path.join(tmp, f"shot_{i:02d}.jpg")
                # -ss BEFORE -i = fast keyframe seek (instant even for huge files)
                ok = await asyncio.to_thread(
                    _run_ff, ["-ss", str(t), "-i", src, "-vframes", "1", "-q:v", "2", "-y", path]
                )
                return path if ok and os.path.exists(path) else None

            # Take all screenshots concurrently
            results = await asyncio.gather(*[_shot(i, t) for i, t in enumerate(times)])
            shots   = [(labels[i], path) for i, path in enumerate(results) if path]

            if not shots:
                await wait.edit(
                    "❌ Could not generate screenshots.\n"
                    "_Make sure ffmpeg is installed._",
                    parse_mode=MD,
                )
                return

            caption = (
                f"📸 **[#Screenshot]** `{label}`\n"
                f"⏱ Timestamps: {', '.join(l for l, _ in shots)}"
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
