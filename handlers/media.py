"""
handlers/media.py
Commands: /mediainfo  /sample  /screenshot

Large-file strategy
───────────────────
Pyrogram (MTProto) can download files of ANY size using client.download_media().
This completely replaces the old Bot-API 20 MB cap.

Usage:
  Reply to any video/document with the command, OR pass a URL:
  /mediainfo <url>  /sample <url>  /screenshot <url>
"""

import asyncio
import io
import json
import os
import subprocess
import tempfile

import ffmpeg
import httpx
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message, InputMediaPhoto

import database as db

SCREENSHOT_COUNT = 10

_SECTION_EMOJI = {
    "General": "🗒", "Video": "🎞", "Audio": "🔊", "Text": "🔠", "Menu": "🗃",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _limit_err(allowed: bool, used: int, limit: int) -> str | None:
    if not allowed:
        return (
            f"⚠️ You've used {used}/{limit} media operations this month.\n"
            "Upgrade to Premium for unlimited access!"
        )
    return None


def _get_video_from_reply(message: Message):
    """Return (media_obj, filename, size) from the replied-to message, or (None,None,0)."""
    r = message.reply_to_message
    if not r:
        return None, None, 0
    if r.video:
        return r.video, r.video.file_name or "video.mp4", r.video.file_size or 0
    if r.document and (r.document.mime_type or "").startswith("video"):
        return r.document, r.document.file_name or "video.mkv", r.document.file_size or 0
    return None, None, 0


async def _download_tg(client: Client, media_obj, dest_dir: str, filename: str) -> str:
    """
    Download a Telegram media file using Pyrogram's MTProto path — no size limit.
    Returns the local file path.
    """
    dest = os.path.join(dest_dir, filename)
    await client.download_media(media_obj, file_name=dest)
    return dest


async def _ydl_stream_url(url: str) -> dict:
    """
    Resolve a YouTube / social URL to a direct CDN URL via yt-dlp.
    Returns {"single": url} or {"video": v_url, "audio": a_url}.
    Falls back to {"single": url} for plain HTTP links.
    """
    social = ("youtube.com", "youtu.be", "vimeo.com", "instagram.com",
              "twitter.com", "tiktok.com", "facebook.com")
    if not any(s in url for s in social):
        return {"single": url}

    def _extract():
        with yt_dlp.YoutubeDL({
            "quiet":         True,
            "skip_download": True,
            "format":        "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            fmts = info.get("requested_formats") or []
            if len(fmts) >= 2:
                v = next((f["url"] for f in fmts if f.get("vcodec", "none") != "none"), None)
                a = next((f["url"] for f in fmts if
                          f.get("acodec", "none") != "none" and
                          f.get("vcodec", "none") == "none"), None)
                if v and a:
                    return {"video": v, "audio": a}
            return {"single": info.get("url") or url}

    try:
        return await asyncio.to_thread(_extract)
    except Exception:
        return {"single": url}


def _probe(src: str) -> dict:
    try:
        return ffmpeg.probe(src)
    except ffmpeg.Error:
        return {}


def _duration(data: dict) -> float:
    try:
        return float(data.get("format", {}).get("duration", 0))
    except (ValueError, TypeError):
        return 0.0


async def _run_ff(node):
    await asyncio.to_thread(lambda: node.overwrite_output().run(capture_output=True))


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
    """Post mediainfo CLI output to Telegraph and return the page URL."""
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
                params={"short_name": "MediaInfoBot", "author_name": "MediaInfo"},
            )
            tok  = acc.json()["result"]["access_token"]
            page = await hx.post(
                "https://api.telegra.ph/createPage",
                json={
                    "access_token": tok,
                    "title":        f"MediaInfo: {filename}"[:256],
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
    lines   = [
        f"📊 *MediaInfo: {name}*\n",
        f"⏱ Duration : `{h:02d}:{m:02d}:{s:02d}`",
        f"💾 Size     : `{size_mb:.2f} MB`",
        f"📡 Bitrate  : `{bitrate} kbps`\n",
    ]
    for st in data.get("streams", []):
        ct = st.get("codec_type", "")
        if ct == "video":
            lines += [
                "🎥 *Video Stream*",
                f"  Codec     : `{st.get('codec_name','?').upper()}`",
                f"  Resolution: `{st.get('width','?')}×{st.get('height','?')}`",
                f"  FPS       : `{st.get('avg_frame_rate','?')}`",
                f"  Bitrate   : `{int(st.get('bit_rate',0))//1000} kbps`\n",
            ]
        elif ct == "audio":
            lines += [
                "🔊 *Audio Stream*",
                f"  Codec      : `{st.get('codec_name','?').upper()}`",
                f"  Channels   : `{st.get('channels','?')}`",
                f"  Sample Rate: `{st.get('sample_rate','?')} Hz`",
                f"  Bitrate    : `{int(st.get('bit_rate',0))//1000} kbps`\n",
            ]
        elif ct == "subtitle":
            lang = st.get("tags", {}).get("language", "?")
            lines.append(f"💬 Subtitle: `{st.get('codec_name','?')} ({lang})`")
    return "\n".join(lines)


# ── Handler registration ──────────────────────────────────────────────────────

def register(app: Client):

    # ── /mediainfo ────────────────────────────────────────────────────────────

    @app.on_message(filters.command("mediainfo"))
    async def cmd_mediainfo(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)
        allowed, used, limit = await db.check_and_consume(u.id, "media")
        if err := _limit_err(allowed, used, limit):
            await message.reply(err)
            return

        args = message.command[1:]

        # URL mode
        if args and args[0].startswith("http"):
            wait = await message.reply("⏳ Fetching media info from URL…")
            info = await _ydl_stream_url(args[0])
            src  = info.get("single") or info.get("video")
            name = args[0].split("?")[0].split("/")[-1][:60] or "video"
            await _do_mediainfo(wait, src, name, local=False)
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                "↩️ Reply to a video with `/mediainfo`, or:\n`/mediainfo <url>`"
            )
            return

        size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
        wait     = await message.reply(f"⏳ Downloading ({size_str}) and reading media info…")

        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = await _download_tg(client, media_obj, tmp, fname)
            except Exception as e:
                await wait.edit(f"❌ Download failed:\n`{e}`")
                return
            await _do_mediainfo(wait, path, fname, local=True)

    async def _do_mediainfo(wait, src: str, name: str, local: bool):
        use_cli = _has_mediainfo()
        if use_cli:
            raw = await asyncio.to_thread(_run_mediainfo, src)
            if raw:
                tg_url = await _post_telegraph(name, raw)
                if tg_url:
                    from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                    await wait.edit(
                        f"📊 *MediaInfo: {name}*\n\n[Click here to view full report]({tg_url})",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📄 Open Report", url=tg_url)
                        ]]),
                    )
                    return
                # Telegraph failed — show first 60 lines inline
                lines = [l for l in raw.split("\n") if l.strip()]
                await wait.edit(f"📊 *{name}*\n\n```\n" + "\n".join(lines[:60]) + "\n```")
                return

        data = await asyncio.to_thread(_probe, src)
        if not data:
            await wait.edit("❌ Could not read media info.")
            return
        await wait.edit(_build_ffprobe_text(data, name))

    # ── /sample ───────────────────────────────────────────────────────────────

    @app.on_message(filters.command("sample"))
    async def cmd_sample(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)
        allowed, used, limit = await db.check_and_consume(u.id, "media")
        if err := _limit_err(allowed, used, limit):
            await message.reply(err)
            return

        args = message.command[1:]

        if args and args[0].startswith("http"):
            wait  = await message.reply("⏳ Generating sample clip from URL…")
            info  = await _ydl_stream_url(args[0])
            label = args[0].split("?")[0].split("/")[-1][:40] or "video"
            await _make_sample(client, message, wait, info, label, local_src=None)
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply("↩️ Reply to a video with `/sample`, or:\n`/sample <url>`")
            return

        size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
        wait     = await message.reply(f"⏳ Downloading ({size_str}) and generating 30s sample…")

        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = await _download_tg(client, media_obj, tmp, fname)
            except Exception as e:
                await wait.edit(f"❌ Download failed:\n`{e}`")
                return
            await _make_sample(client, message, wait, {"single": path},
                               os.path.splitext(fname)[0], local_src=tmp)

    async def _make_sample(client, message, wait, stream_info, label, local_src):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sample.mp4")
            src = stream_info.get("single") or stream_info.get("video")

            data = await asyncio.to_thread(_probe, src)
            dur  = _duration(data)
            if dur < 10:
                await wait.edit("❌ Video is too short to generate a sample.")
                return

            start = max(10.0, dur * 0.15)
            try:
                if "single" in stream_info:
                    node = (ffmpeg
                            .input(stream_info["single"], ss=start, t=30)
                            .output(out, **{
                                "c:v": "libx264", "preset": "fast", "crf": "28",
                                "c:a": "aac", "b:a": "128k", "movflags": "+faststart",
                            }))
                else:
                    v = ffmpeg.input(stream_info["video"], ss=start, t=30)
                    a = ffmpeg.input(stream_info["audio"], ss=start, t=30)
                    node = ffmpeg.output(v["v"], a["a"], out, **{
                        "c:v": "libx264", "preset": "fast", "crf": "28",
                        "c:a": "aac", "b:a": "128k", "movflags": "+faststart",
                    })
                await _run_ff(node)
            except Exception as e:
                await wait.edit(f"❌ Sample generation failed:\n`{e}`")
                return

            if not os.path.exists(out):
                await wait.edit("❌ Sample generation failed.")
                return

            await wait.delete()
            await client.send_video(
                message.chat.id,
                out,
                caption=f"🎬 *[#Sample]* `{label}`",
                supports_streaming=True,
            )

    # ── /screenshot ───────────────────────────────────────────────────────────

    @app.on_message(filters.command("screenshot"))
    async def cmd_screenshot(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)
        allowed, used, limit = await db.check_and_consume(u.id, "media")
        if err := _limit_err(allowed, used, limit):
            await message.reply(err)
            return

        args = message.command[1:]

        if args and args[0].startswith("http"):
            wait  = await message.reply("⏳ Taking screenshots from URL…")
            info  = await _ydl_stream_url(args[0])
            label = args[0].split("?")[0].split("/")[-1][:40] or "video"
            await _take_shots(client, message, wait, info, label)
            return

        media_obj, fname, fsize = _get_video_from_reply(message)
        if not media_obj:
            await message.reply(
                f"↩️ Reply to a video with `/screenshot`, or:\n`/screenshot <url>`"
            )
            return

        size_str = f"{fsize/1024/1024:.1f} MB" if fsize else "unknown size"
        wait     = await message.reply(f"⏳ Downloading ({size_str}) and taking {SCREENSHOT_COUNT} screenshots…")

        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = await _download_tg(client, media_obj, tmp, fname)
            except Exception as e:
                await wait.edit(f"❌ Download failed:\n`{e}`")
                return
            await _take_shots(client, message, wait, {"single": path},
                              os.path.splitext(fname)[0])

    async def _take_shots(client, message, wait, stream_info, label):
        with tempfile.TemporaryDirectory() as tmp:
            src  = stream_info.get("single") or stream_info.get("video")
            data = await asyncio.to_thread(_probe, src)
            dur  = _duration(data)
            if dur < 10:
                await wait.edit("❌ Video is too short.")
                return

            margin = dur * 0.05
            step   = (dur - 2 * margin) / SCREENSHOT_COUNT
            times  = [margin + step * i for i in range(SCREENSHOT_COUNT)]
            labels = [f"{int(t)}s" for t in times]

            async def _shot(i: int, t: float) -> str | None:
                path = os.path.join(tmp, f"shot_{i:02d}.jpg")
                try:
                    node = ffmpeg.input(src, ss=t).output(path, vframes=1, **{"q:v": "2"})
                    await _run_ff(node)
                    return path if os.path.exists(path) else None
                except Exception:
                    return None

            shots = await asyncio.gather(*[_shot(i, t) for i, t in enumerate(times)])
            shots = [s for s in shots if s]
            if not shots:
                await wait.edit("❌ Could not generate screenshots.")
                return

            caption = f"📸 *[#Screenshot]* At {', '.join(labels)}\n`{label}`"
            media   = []
            for i, shot in enumerate(shots):
                media.append(InputMediaPhoto(
                    shot,
                    caption=caption if i == 0 else "",
                ))

            await wait.delete()
            await client.send_media_group(message.chat.id, media)
