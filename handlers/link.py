"""
handlers/link.py  —  /link command
Reply to any file/video/audio to get Watch Online + Download links.

BUTTON_URL_INVALID fix:
  Telegram rejects localhost/private URLs in inline buttons.
  If STREAM_BASE_URL is not a public URL we skip the buttons and
  send the raw file info instead.
"""

from mimetypes import guess_type

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

import database as db
import pyrogram_helper as pyro
from config import BOT_TOKEN, STREAM_BASE_URL
from stream_server import LINK_TTL, create_token

MD = ParseMode.MARKDOWN


def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _is_public_url(url: str) -> bool:
    """Check if the URL is publicly reachable (not localhost/private IP)."""
    if not url:
        return False
    private = ("localhost", "127.", "192.168.", "10.", "172.16.", "172.17.",
                "172.18.", "172.19.", "172.2", "172.3", "0.0.0.0")
    lower = url.lower()
    return not any(p in lower for p in private)


def register(app: Client):

    @app.on_message(filters.command("link") & filters.private)
    async def cmd_link(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        r = message.reply_to_message
        if not r:
            await message.reply(
                "↩️ **How to use /link**\n\n"
                "Reply to any **video**, **document**, or **audio** file with `/link`\n"
                "to get a **Watch Online** + **Download** link.\n\n"
                "📌 _Links expire after 1 hour._",
                parse_mode=MD,
            )
            return

        # ── Identify media ─────────────────────────────────────────────────────
        media_obj = filename = mime = None
        size = 0

        if r.video:
            media_obj = r.video
            filename  = r.video.file_name or "video.mp4"
            size      = r.video.file_size or 0
            mime      = r.video.mime_type or "video/mp4"
        elif r.document:
            media_obj = r.document
            filename  = r.document.file_name or "file"
            size      = r.document.file_size or 0
            mime      = (r.document.mime_type
                         or guess_type(r.document.file_name or "")[0]
                         or "application/octet-stream")
        elif r.audio:
            media_obj = r.audio
            filename  = r.audio.file_name or "audio.mp3"
            size      = r.audio.file_size or 0
            mime      = r.audio.mime_type or "audio/mpeg"

        if not media_obj:
            await message.reply(
                "❌ Please reply to a **video**, **document**, or **audio** file.",
                parse_mode=MD,
            )
            return

        wait     = await message.reply("⏳ Generating link…")
        size_str = _human_size(size)
        use_stream = _is_public_url(STREAM_BASE_URL)

        # ── Path A: Pyrogram MTProto — unlimited size ──────────────────────────
        if pyro.is_available():
            if use_stream:
                token     = create_token(filename, size, mime, tg_file_id=media_obj.file_id)
                base      = STREAM_BASE_URL.rstrip("/")
                watch_url = f"{base}/watch/{token}"
                dl_url    = f"{base}/dl/{token}"

                text = (
                    f"🔗 **{filename}**\n\n"
                    f"💾 Size    : `{size_str}`\n"
                    f"📁 Type    : `{mime}`\n"
                    f"🚀 Backend : `Pyrogram MTProto`\n\n"
                    f"⏳ _Links expire in {LINK_TTL // 3600}h_"
                )
                buttons = [[
                    InlineKeyboardButton("▶️ Watch Online", url=watch_url),
                    InlineKeyboardButton("⬇️ Download",     url=dl_url),
                ]]
                await wait.delete()
                await message.reply(
                    text, parse_mode=MD, reply_markup=InlineKeyboardMarkup(buttons)
                )
            else:
                # STREAM_BASE_URL is localhost or missing — can't make public buttons
                await wait.edit(
                    f"🔗 **{filename}**\n"
                    f"💾 Size: `{size_str}` | 🚀 Pyrogram: ✅\n\n"
                    "⚠️ **Set a public `STREAM_BASE_URL`** in your `.env` to generate\n"
                    "Watch/Download links that Telegram can open.\n\n"
                    "Example:\n`STREAM_BASE_URL=https://your-domain.com`\n"
                    "_`localhost` URLs are rejected by Telegram._",
                    parse_mode=MD,
                )
            return

        # ── Path B: Bot API CDN proxy — ≤ 20 MB ───────────────────────────────
        try:
            tg_file = await client.get_file(media_obj.file_id)
            cdn_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
        except Exception:
            await wait.edit(
                f"❌ **File too large** ({size_str}) for the Bot API 20 MB limit.\n\n"
                "Add Telegram API credentials to `.env` for unlimited size:\n"
                "`API_ID=your_api_id`\n`API_HASH=your_api_hash`\n\n"
                "Get them free from https://my.telegram.org/apps",
                parse_mode=MD,
            )
            return

        if use_stream:
            token     = create_token(filename, size, mime, cdn_url=cdn_url)
            base      = STREAM_BASE_URL.rstrip("/")
            watch_url = f"{base}/watch/{token}"
            dl_url    = f"{base}/dl/{token}"

            text = (
                f"🔗 **{filename}**\n\n"
                f"💾 Size    : `{size_str}`\n"
                f"📁 Type    : `{mime}`\n"
                f"🔌 Backend : `CDN Proxy`\n\n"
                f"⏳ _Links expire in {LINK_TTL // 3600}h_"
            )
            buttons = [[
                InlineKeyboardButton("▶️ Watch Online", url=watch_url),
                InlineKeyboardButton("⬇️ Download",     url=dl_url),
            ]]
            await wait.delete()
            await message.reply(
                text, parse_mode=MD, reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await wait.edit(
                f"🔗 **{filename}** • `{size_str}`\n\n"
                f"⬇️ Direct CDN link _(expires ~1h)_:\n`{cdn_url}`\n\n"
                "_Set a public `STREAM_BASE_URL` for Watch/Download pages._",
                parse_mode=MD,
            )
