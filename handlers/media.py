"""
handlers/media.py  —  /mediainfo  /sample  /screenshot

ffmpeg strategy:
  Uses imageio.plugins.ffmpeg to auto-download a static ffmpeg binary
  on first run — works on any cloud host even without system ffmpeg/ffprobe.
  Falls back to system ffmpeg if already installed.
"""

import asyncio
import os
import subprocess
import tempfile

import httpx
import yt_dlp
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, InputMediaPhoto,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db

MD               = ParseMode.MARKDOWN
SCREENSHOT_COUNT = 10

_SECTION_EMOJI = {
    "General": "🗒", "Video": "🎞", "Audio": "🔊", "Text": "🔠", "Menu": "🗃",
}

# ── ffmpeg binary resolution ──────────────────────────────────────────────────
# Try system first; fall back to imageio's auto-downloaded static binary.

def _find_ffmpeg() -> tuple[str, str]:
    """Return (ffmpeg_path, ffprobe_path). Downloads static binary if needed."""
    import shutil

    ff  = shutil.which("ffmpeg")
    ffp = shutil.which("ffprobe")

    if ff and ffp:
        return ff, ffp

    # Use imageio to get a static binary
    try:
        import imageio_ffmpeg as ioff
        ff  = ioff.get_ffmpeg_exe()
        # imageio only ships ffmpeg; derive ffprobe path from same dir
        ffp_candidate = os.path.join(os.path.dirname(ff), "ffprobe")
        if os.path.exists(ffp_candidate):
            return ff, ffp_candidate
        # No ffprobe alongside — use ffmpeg for probing too (via -show_streams flag)
        return ff, ff   # we'll handle ffprobe-only calls specially
    except Exception:
        pass

    return "ffmpeg", "ffprobe"   # last resort; will fail with clear error


_FFMPEG, _FFPROBE = _find_ffmpeg()


def _probe(src: str) -> dict:
    """Run ffprobe (or ffmpeg -hide_banner) to get stream info."""
    import json

    # Try with the resolved ffprobe
    exe = _FFPROBE if _FFPROBE != _FFMPEG else _FFMPEG

    if exe == _FFMPEG:
        # Use ffmpeg itself to probe
        cmd = [
            _FFMPEG, "-hide_banner", "-v", "quiet",
            "-print_format", "json", "-show_format", "-show_streams",
            "-i", src,
        ]
    else:
        cmd = [
            exe, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            src,
        ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        text = r.stdout or r.stderr
        # ffprobe puts json in stdout; ffmpeg -print_format puts it in stderr
        for candidate in (r.stdout, r.stderr):
            if candidate and candidate.strip().startswith("{"):
                return json.loads(candidate)
    except Exception:
        pass
    return {}


def _run_ff_cmd(args: list[str]) -> bool:
    """Run an ffmpeg command. Returns True on success."""
    cmd = [_FFMPEG] + args
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        return r.returncode == 0
    except Exception:
        return False


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
    dest = os.path.join(dest_dir, filename)
    await client.download_media(media_obj, file_name=dest)
    return dest


async def _ydl_stream_url(url: str) -> dict:
    social = ("youtube.com", "youtu.be", "vimeo.com", "instagram.com",
               "twitter.com", "tiktok.com", "facebook.com")
    if not any(s in url for s in social):
        return {"single": url}

    def _extract():
        with yt_dlp.YoutubeDL({
            "quiet":        True,
            "skip_download": True,
            "format":       "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
        }) as ydl:
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
            acc  = await hx.get(
                "https://api.telegra.ph/createAccount",
                params={"short_name": "MediaBot", "author_name": "MediaInfo"},
            )
            tok  = acc.json()["result"]["access_token"]
            page = await hx.post(
                "https://api.telegra.ph/createPage",
                json={
                    "access_token": tok,
                    "title":        f"MediaInfo — {filename}"[:256],
                    "author_name":  "MediaInfo Bot",
                    "content":      nodes,
                },
            )
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
            fps = st.get("avg_frame_rate", "?")
            vbr = int(st.get("bit_rate", 0)) // 1000
            lines += [
                "🎥 **Video Stream**",
                f"  Codec      : `{st.get('codec_name','?').upper()}`",
                f"  Profile    : `{st.get('profile','?')}`",
                f"  Resolution : `{st.get('width','?')}×{st.get('height','?')}`",
                f"  FPS        : `{fps}`",
                f"  Bitrate    : `{vbr} kbps`\n",
            ]
        elif ct == "audio":
            abr = int(st.get("bit_rate", 0)) // 1000
            lines += [
                "🔊 **Audio Stream**",
                f"  Codec       : `{st.get('codec_name','?').upper()}`",
                f"  Channels    : `{st.get('channels','?')}`",
                f"  Sample Rate : `{st.get('sample_rate','?')} Hz`",
                f"  Bitrate     : `{abr} kbps`\n",
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
                "• Or: `/mediainfo <url>`",
                parse_mode=MD,
            )
            return

        size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
        wait     = await message.reply(f"⏳ Downloading `{fname}` ({size_str})…", parse_mode=MD)

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
                "❌ Could not read media info.\n"
                "_Make sure ffmpeg is installed on the server._",
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
            await _make_sample(client, message, wait, info, label)
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

        size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
        wait     = await message.reply(
            f"⏳ Downloading `{fname}` ({size_str}) and creating sample…", parse_mode=MD
        )

        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = await _download_tg(client, media_obj, tmp, fname)
            except Exception as e:
                await wait.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
                return
            await _make_sample(client, message, wait,
                                {"single": path}, os.path.splitext(fname)[0])

    async def _make_sample(client: Client, message: Message, wait,
                            stream_info: dict, label: str):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sample.mp4")
            src = stream_info.get("single") or stream_info.get("video")

            data = await asyncio.to_thread(_probe, src)
            dur  = _duration(data)
            if dur < 10:
                await wait.edit("❌ Video is too short to generate a sample (minimum 10s).")
                return

            start = max(10.0, dur * 0.15)

            def _do_sample():
                if "single" in stream_info:
                    return _run_ff_cmd([
                        "-ss", str(start), "-i", stream_info["single"],
                        "-t", "30",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                        "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart",
                        "-y", out,
                    ])
                else:
                    return _run_ff_cmd([
                        "-ss", str(start), "-i", stream_info["video"],
                        "-ss", str(start), "-i", stream_info["audio"],
                        "-t", "30",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                        "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart",
                        "-y", out,
                    ])

            ok = await asyncio.to_thread(_do_sample)
            if not ok or not os.path.exists(out):
                await wait.edit(
                    "❌ Sample generation failed.\n"
                    "_Make sure ffmpeg is installed on the server._",
                    parse_mode=MD,
                )
                return

            out_size = os.path.getsize(out) / 1024 / 1024
            await wait.delete()
            await client.send_video(
                message.chat.id,
                out,
                caption=(
                    f"🎬 **[#Sample]** `{label}`\n"
                    f"⏱ 30s clip  •  💾 {out_size:.1f} MB"
                ),
                parse_mode=MD,
                supports_streaming=True,
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
            await _take_shots(client, message, wait, info, label)
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

        size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
        wait     = await message.reply(
            f"⏳ Downloading `{fname}` ({size_str}) and taking {SCREENSHOT_COUNT} screenshots…",
            parse_mode=MD,
        )

        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = await _download_tg(client, media_obj, tmp, fname)
            except Exception as e:
                await wait.edit(f"❌ Download failed:\n`{e}`", parse_mode=MD)
                return
            await _take_shots(client, message, wait,
                               {"single": path}, os.path.splitext(fname)[0])

    async def _take_shots(client: Client, message: Message, wait,
                           stream_info: dict, label: str):
        with tempfile.TemporaryDirectory() as tmp:
            src  = stream_info.get("single") or stream_info.get("video")
            data = await asyncio.to_thread(_probe, src)
            dur  = _duration(data)
            if dur < 10:
                await wait.edit("❌ Video is too short (minimum 10 seconds).")
                return

            margin = dur * 0.05
            step   = (dur - 2 * margin) / SCREENSHOT_COUNT
            times  = [margin + step * i for i in range(SCREENSHOT_COUNT)]
            labels = [f"{int(t)}s" for t in times]

            def _take_one(i: int, t: float) -> str | None:
                path = os.path.join(tmp, f"shot_{i:02d}.jpg")
                ok = _run_ff_cmd([
                    "-ss", str(t), "-i", src,
                    "-vframes", "1", "-q:v", "2",
                    "-y", path,
                ])
                return path if ok and os.path.exists(path) else None

            shots = await asyncio.gather(
                *[asyncio.to_thread(_take_one, i, t) for i, t in enumerate(times)]
            )
            shots = [s for s in shots if s]
            if not shots:
                await wait.edit(
                    "❌ Could not generate screenshots.\n"
                    "_Make sure ffmpeg is installed on the server._",
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
