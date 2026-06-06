"""
handlers/media.py
Commands: /mediainfo  /sample  /screenshot

All three work in two modes:
  1. Reply to any video/document → uses internal stream server (ByteStreamer)
     → ffmpeg accesses the file via http://127.0.0.1:PORT/dl/TOKEN
     → NO file size limit, no local download needed
  2. /command <url> → yt-dlp extracts CDN URL → ffmpeg streams it

All blocking operations run in asyncio.to_thread() for concurrency.
"""
import asyncio
import os
import subprocess
import tempfile
from typing import Optional

import ffmpeg
import httpx
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InputMediaPhoto, Message

import database as db
from config import STREAM_PORT
from stream_server import create_token

SCREENSHOT_COUNT = 10


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _limit_msg(allowed: bool, used: int, limit: int) -> str | None:
    return (
        f"⚠️ You've used {used}/{limit} media operations this month.\n"
        "Upgrade to Premium for unlimited access!"
    ) if not allowed else None


def _get_media(message: Message):
    """Return (file_id, filename, file_size, mime) from a message, or all-None."""
    if message.video:
        v = message.video
        return v.file_id, (v.file_name or "video.mp4"), (v.file_size or 0), (v.mime_type or "video/mp4")
    if message.document:
        d = message.document
        if (d.mime_type or "").startswith("video"):
            return d.file_id, (d.file_name or "video.mkv"), (d.file_size or 0), (d.mime_type or "video/x-matroska")
    return None, None, 0, None


def _internal_stream_url(file_id: str, size: int, mime: str, name: str) -> str:
    """Create a token and return the localhost stream URL for ffmpeg."""
    token = create_token(name, size, mime, tg_file_id=file_id)
    return f"http://127.0.0.1:{STREAM_PORT}/dl/{token}"


async def _ydl_stream_url(url: str) -> str:
    """Get a direct CDN URL for a YouTube/social URL via yt-dlp."""
    def _extract():
        with yt_dlp.YoutubeDL({
            "quiet": True, "skip_download": True,
            "format": "best[ext=mp4]/best",
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("url") or url
    try:
        return await asyncio.to_thread(_extract)
    except Exception:
        return url


def _probe(src: str) -> dict:
    try:
        return ffmpeg.probe(src)
    except ffmpeg.Error:
        return {}


def _dur(probe_data: dict) -> float:
    try:
        return float(probe_data.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0


async def _run_ff(node):
    await asyncio.to_thread(lambda: node.overwrite_output().run(capture_output=True))


# ── /mediainfo ─────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("mediainfo"))
async def cmd_mediainfo(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")
    allowed, used, limit = await db.check_and_consume(u.id, "media")
    if err := _limit_msg(allowed, used, limit):
        return await message.reply(err)

    args = message.command[1:]

    # URL mode
    if args and args[0].startswith("http"):
        msg = await message.reply("⏳ Fetching media info from URL…")
        src = await _ydl_stream_url(args[0])
        return await _do_mediainfo(message, msg, src, args[0].split("/")[-1][:50])

    # File mode
    replied = message.reply_to_message
    if not replied:
        return await message.reply(
            "↩️ Reply to a video with `/mediainfo`, or:\n`/mediainfo <url>`"
        )
    file_id, name, size, mime = _get_media(replied)
    if not file_id:
        return await message.reply("❌ Reply to a video or video file.")

    msg = await message.reply("⏳ Fetching media info…")
    src = _internal_stream_url(file_id, size, mime, name)
    await _do_mediainfo(message, msg, src, name)


async def _do_mediainfo(message: Message, msg, src: str, name: str):
    # Try mediainfo CLI first
    def _run_cli():
        try:
            r = subprocess.run(["mediainfo", src], capture_output=True, text=True, timeout=60)
            return r.stdout if r.returncode == 0 else None
        except Exception:
            return None

    raw_output = await asyncio.to_thread(_run_cli)

    if raw_output:
        # Post to Telegraph
        telegraph_url = await _to_telegraph(name, raw_output)
        if telegraph_url:
            from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            return await msg.edit(
                f"📊 **MediaInfo:** `{name}`\n\n[Click here to view full report]({telegraph_url})",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📄 Open Report", url=telegraph_url)
                ]]),
            )
        # Telegraph failed → inline
        lines = [l for l in raw_output.split("\n") if l.strip()]
        return await msg.edit(f"📊 **{name}**\n\n```\n" + "\n".join(lines[:50]) + "\n```")

    # Fallback: ffprobe
    data = await asyncio.to_thread(_probe, src)
    if not data:
        return await msg.edit("❌ Could not read media info.")
    await msg.edit(_ffprobe_text(data, name))


def _ffprobe_text(data: dict, name: str) -> str:
    fmt     = data.get("format", {})
    dur     = float(fmt.get("duration", 0))
    h, rem  = divmod(int(dur), 3600)
    m, s    = divmod(rem, 60)
    size_mb = int(fmt.get("size", 0)) / 1024 / 1024
    bitrate = int(fmt.get("bit_rate", 0)) // 1000
    lines   = [
        f"📊 **MediaInfo: {name}**\n",
        f"⏱ Duration : `{h:02d}:{m:02d}:{s:02d}`",
        f"💾 Size     : `{size_mb:.2f} MB`",
        f"📡 Bitrate  : `{bitrate} kbps`\n",
    ]
    for st in data.get("streams", []):
        ct = st.get("codec_type", "")
        if ct == "video":
            lines += [
                "🎥 **Video Stream**",
                f"  Codec     : `{st.get('codec_name','?').upper()}`",
                f"  Resolution: `{st.get('width','?')}×{st.get('height','?')}`",
                f"  FPS       : `{st.get('avg_frame_rate','?')}`",
                f"  Bitrate   : `{int(st.get('bit_rate',0))//1000} kbps`\n",
            ]
        elif ct == "audio":
            lines += [
                "🔊 **Audio Stream**",
                f"  Codec      : `{st.get('codec_name','?').upper()}`",
                f"  Channels   : `{st.get('channels','?')}`",
                f"  Sample Rate: `{st.get('sample_rate','?')} Hz`\n",
            ]
    return "\n".join(lines)


async def _to_telegraph(filename: str, raw_output: str) -> Optional[str]:
    """Post mediainfo to Telegraph; return URL or None."""
    try:
        lines   = raw_output.split("\n")
        nodes   = []
        section = []
        SECTION_EMOJI = {"General":"🗒","Video":"🎞","Audio":"🔊","Text":"🔠","Menu":"🗃"}
        for line in lines:
            is_section = any(line.startswith(s) for s in SECTION_EMOJI)
            if is_section:
                if section:
                    nodes.append({"tag":"pre","children":["\n".join(section)]})
                    section = []
                emoji = next((e for s,e in SECTION_EMOJI.items() if line.startswith(s)), "📋")
                nodes.append({"tag":"h4","children":[f"{emoji} {line}"]})
            elif line.strip():
                section.append(line)
        if section:
            nodes.append({"tag":"pre","children":["\n".join(section)]})

        async with httpx.AsyncClient(timeout=15) as c:
            acc  = await c.get("https://api.telegra.ph/createAccount",
                               params={"short_name":"MediaInfoBot","author_name":"MediaInfo"})
            tok  = acc.json()["result"]["access_token"]
            page = await c.post("https://api.telegra.ph/createPage", json={
                "access_token": tok,
                "title":        f"MediaInfo: {filename}"[:256],
                "author_name":  "MediaInfo Bot",
                "content":      nodes,
            })
            return f"https://telegra.ph/{page.json()['result']['path']}"
    except Exception:
        return None


# ── /sample ────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("sample"))
async def cmd_sample(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")
    allowed, used, limit = await db.check_and_consume(u.id, "media")
    if err := _limit_msg(allowed, used, limit):
        return await message.reply(err)

    args = message.command[1:]
    if args and args[0].startswith("http"):
        msg   = await message.reply("⏳ Generating sample from URL…")
        src   = await _ydl_stream_url(args[0])
        label = args[0].split("?")[0].split("/")[-1][:40] or "video"
        return await _make_sample(message, msg, src, label)

    replied = message.reply_to_message
    if not replied:
        return await message.reply("↩️ Reply to a video with `/sample`, or:\n`/sample <url>`")

    file_id, name, size, mime = _get_media(replied)
    if not file_id:
        return await message.reply("❌ Reply to a video or video file.")

    msg = await message.reply("⏳ Generating sample clip…")
    src = _internal_stream_url(file_id, size, mime, name)
    await _make_sample(message, msg, src, os.path.splitext(name)[0])


async def _make_sample(message: Message, msg, src: str, label: str):
    with tempfile.TemporaryDirectory() as tmp:
        out  = os.path.join(tmp, "sample.mp4")
        data = await asyncio.to_thread(_probe, src)
        dur  = _dur(data)
        if dur < 10:
            return await msg.edit("❌ Video is too short.")

        start = max(10.0, dur * 0.15)
        try:
            await _run_ff(
                ffmpeg.input(src, ss=start, t=30)
                .output(out, **{"c:v":"libx264","preset":"fast","crf":"28",
                                "c:a":"aac","b:a":"128k","movflags":"+faststart"})
            )
        except Exception as e:
            return await msg.edit(f"❌ Failed:\n`{e}`")

        if not os.path.exists(out):
            return await msg.edit("❌ Sample generation failed.")

        await msg.delete()
        await message.reply_video(
            video=out,
            caption=f"🎬 **[#Sample]** `{label}`",
            supports_streaming=True,
        )


# ── /screenshot ────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("screenshot"))
async def cmd_screenshot(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")
    allowed, used, limit = await db.check_and_consume(u.id, "media")
    if err := _limit_msg(allowed, used, limit):
        return await message.reply(err)

    args = message.command[1:]
    if args and args[0].startswith("http"):
        msg   = await message.reply("⏳ Taking screenshots from URL…")
        src   = await _ydl_stream_url(args[0])
        label = args[0].split("?")[0].split("/")[-1][:40] or "video"
        return await _take_screenshots(message, msg, src, label)

    replied = message.reply_to_message
    if not replied:
        return await message.reply("↩️ Reply to a video with `/screenshot`, or:\n`/screenshot <url>`")

    file_id, name, size, mime = _get_media(replied)
    if not file_id:
        return await message.reply("❌ Reply to a video or video file.")

    msg = await message.reply(f"⏳ Taking {SCREENSHOT_COUNT} screenshots…")
    src = _internal_stream_url(file_id, size, mime, name)
    await _take_screenshots(message, msg, src, os.path.splitext(name)[0])


async def _take_screenshots(message: Message, msg, src: str, label: str):
    with tempfile.TemporaryDirectory() as tmp:
        data   = await asyncio.to_thread(_probe, src)
        dur    = _dur(data)
        if dur < 10:
            return await msg.edit("❌ Video is too short.")

        margin = dur * 0.05
        step   = (dur - 2 * margin) / SCREENSHOT_COUNT
        times  = [margin + step * i for i in range(SCREENSHOT_COUNT)]
        labels = [f"{int(t)}s" for t in times]

        async def _shot(i: int, t: float) -> str | None:
            path = os.path.join(tmp, f"shot_{i:02d}.jpg")
            try:
                await _run_ff(ffmpeg.input(src, ss=t).output(path, vframes=1, **{"q:v":"2"}))
                return path if os.path.exists(path) else None
            except Exception:
                return None

        shots = await asyncio.gather(*[_shot(i, t) for i, t in enumerate(times)])
        shots = [s for s in shots if s]
        if not shots:
            return await msg.edit("❌ Could not generate screenshots.")

        caption = f"📸 **[#Screenshot]** At {', '.join(labels)}\n`{label}`"
        media   = [InputMediaPhoto(s, caption=caption if i == 0 else "")
                   for i, s in enumerate(shots)]
        await msg.delete()
        await message.reply_media_group(media)
