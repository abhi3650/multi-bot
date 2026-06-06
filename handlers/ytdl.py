import asyncio
import hashlib
import io
import os
import re
import tempfile

import httpx
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                             InlineKeyboardMarkup, Message)

import database as db

_sessions: dict = {}   # {key: {url, meta}}

YT_RE = re.compile(r"^(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/)|youtu\.be/)[\w\-]+")
COBALT = "https://api.cobalt.tools/"
COBALT_HDR = {"Accept":"application/json","Content-Type":"application/json"}

QUALITY_OPTIONS = [
    ("🎥 4K  (2160p)", "2160"),
    ("🎥 2K  (1440p)", "1440"),
    ("🎥 1080p FHD",   "1080"),
    ("🎥 720p  HD",    "720"),
    ("🎥 480p",        "480"),
    ("🎥 360p",        "360"),
]


def _key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:10]


def _is_yt(url: str) -> bool:
    return bool(YT_RE.match(url.strip()))


def _fmt(s: int) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _max_height(formats: list) -> int:
    return max((f.get("height",0) for f in formats if f.get("vcodec","none")!="none" and f.get("height")), default=720)


async def _cobalt_link(url: str, quality: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r    = await c.post(COBALT, json={"url":url,"videoQuality":quality,"downloadMode":"auto","filenameStyle":"pretty"}, headers=COBALT_HDR)
            data = r.json()
        if data.get("status") in ("stream","redirect","tunnel"):
            return data.get("url")
        if data.get("status") == "picker":
            picks = data.get("picker",[])
            if picks: return picks[0].get("url")
    except Exception:
        pass
    return None


def _find_mp3(d: str) -> str | None:
    for f in os.listdir(d):
        if f.lower().endswith(".mp3"): return os.path.join(d,f)
    return None


@Client.on_message(filters.command("yt"))
async def cmd_yt(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")

    args = message.command[1:]
    if not args:
        return await message.reply(
            "📥 **YouTube Downloader**\n\n"
            "Usage: `/yt <youtube_url>`\n\n"
            "🎥 Video qualities → direct download link\n"
            "🎵 MP3 Audio → bot downloads & sends the file"
        )

    url = args[0].strip()
    if not _is_yt(url):
        return await message.reply("❌ Only YouTube links are supported.")

    msg = await message.reply("⏳ Fetching video info…")

    def _info():
        with yt_dlp.YoutubeDL({
            "quiet":True,"skip_download":True,"noplaylist":True,
            "extractor_args":{"youtube":{"player_client":["android"]}},
        }) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_info)
    except Exception as e:
        return await msg.edit(f"❌ Could not fetch info:\n`{e}`")

    title     = info.get("title","Unknown")
    uploader  = info.get("uploader","Unknown")
    duration  = int(info.get("duration") or 0)
    views     = f"{info.get('view_count',0):,}"
    thumb     = info.get("thumbnail","")
    video_id  = info.get("id","")
    clean_url = f"https://www.youtube.com/watch?v={video_id}"
    max_h     = _max_height(info.get("formats",[]))

    k = _key(clean_url)
    _sessions[k] = {"url": clean_url, "meta": {"title":title,"uploader":uploader,"duration":duration,"thumbnail":thumb,"video_id":video_id}}

    rows, pair = [], []
    for label, q in QUALITY_OPTIONS:
        if int(q) <= max_h:
            pair.append(InlineKeyboardButton(label, callback_data=f"ytq|{k}|{q}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
    if pair: rows.append(pair)
    rows.append([InlineKeyboardButton("🎵 MP3 Audio (send file)", callback_data=f"ytq|{k}|audio")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="ytq_cancel")])

    caption = (
        f"🎬 **{title}**\n"
        f"👤 `{uploader}`\n"
        f"⏱ `{_fmt(duration)}`  •  👁 `{views} views`\n\n"
        "🎥 Video → direct link\n🎵 Audio → bot sends MP3"
    )
    await msg.delete()
    if thumb:
        await message.reply_photo(photo=thumb, caption=caption, reply_markup=InlineKeyboardMarkup(rows))
    else:
        await message.reply(caption, reply_markup=InlineKeyboardMarkup(rows))


@Client.on_callback_query(filters.regex("^ytq"))
async def yt_callback(client: Client, cq: CallbackQuery):
    await cq.answer()

    if cq.data == "ytq_cancel":
        return await cq.message.delete()

    _, k, quality = cq.data.split("|", 2)
    session = _sessions.get(k)
    if not session:
        return await cq.message.reply("❌ Session expired. Send the link again.")

    url  = session["url"]
    meta = session["meta"]

    if quality == "audio":
        await _send_audio(cq, url, meta)
    else:
        await _send_video_link(cq, url, quality)


async def _send_video_link(cq: CallbackQuery, url: str, quality: str):
    orig = cq.message.caption or ""
    try:
        await cq.message.edit_caption(orig + f"\n\n⏳ Getting **{quality}p** link…")
    except Exception:
        pass

    link = await _cobalt_link(url, quality)

    try:
        await cq.message.edit_caption(orig, reply_markup=cq.message.reply_markup)
    except Exception:
        pass

    if not link:
        return await cq.message.reply(f"❌ Could not get link for **{quality}p**.\nTry a different quality.")

    await cq.message.reply(
        f"✅ **{quality}p — Download Link**\n\n[⬇️ Tap here to download]({link})\n\n⏳ _Link is temporary — download soon!_"
    )


async def _send_audio(cq: CallbackQuery, url: str, meta: dict):
    title     = meta.get("title","Unknown")
    uploader  = meta.get("uploader","Unknown")
    duration  = int(meta.get("duration") or 0)
    thumb_url = meta.get("thumbnail","")
    video_id  = meta.get("video_id", _key(url))

    try:
        await cq.message.edit_caption((cq.message.caption or "") + "\n\n⏳ Downloading MP3…")
    except Exception:
        pass

    # Download thumb
    thumb_data = None
    if thumb_url:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(thumb_url)
                if r.status_code == 200:
                    thumb_data = r.content
        except Exception:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        ydl_opts = {
            "format":"bestaudio/best",
            "outtmpl": os.path.join(tmp,"%(id)s.%(ext)s"),
            "quiet":True,
            "extractor_args":{"youtube":{"player_client":["android"]}},
            "postprocessors":[
                {"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"},
                {"key":"FFmpegMetadata","add_metadata":True},
            ],
        }
        try:
            await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))
        except Exception as e:
            return await cq.message.reply(f"❌ Download failed:\n`{e}`")

        mp3 = os.path.join(tmp,f"{video_id}.mp3")
        if not os.path.exists(mp3):
            mp3 = _find_mp3(tmp)
        if not mp3:
            return await cq.message.reply("❌ Could not find downloaded audio.")

        size_mb = os.path.getsize(mp3)/1024/1024
        if size_mb > 50:
            return await cq.message.reply(f"❌ File too large ({size_mb:.1f} MB).")

        # Save thumb for Telegram
        thumb_path = None
        if thumb_data:
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(thumb_data)).convert("RGB")
                thumb_path = os.path.join(tmp,"thumb.jpg")
                img.save(thumb_path,"JPEG")
            except Exception:
                thumb_path = None

        caption = f"🎵 **{title}**\n👤 `{uploader}`\n⏱ `{_fmt(duration)}`  •  💾 `{size_mb:.1f} MB`"
        await cq.message.delete()
        await cq.message.reply_audio(
            audio=mp3, title=title, performer=uploader,
            duration=duration, thumb=thumb_path, caption=caption,
        )
