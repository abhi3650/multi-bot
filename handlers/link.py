from mimetypes import guess_type
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
import database as db
from config import STREAM_BASE_URL, STREAM_PORT
from stream_server import create_token, LINK_TTL


def _human_size(n: int) -> str:
    for u in ("B","KB","MB","GB"):
        if n < 1024: return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _get_file(message: Message):
    if message.video:
        v = message.video
        return v.file_id, (v.file_name or "video.mp4"), (v.file_size or 0), (v.mime_type or "video/mp4")
    if message.document:
        d = message.document
        return d.file_id, (d.file_name or "file"), (d.file_size or 0), (d.mime_type or guess_type(d.file_name or "")[0] or "application/octet-stream")
    if message.audio:
        a = message.audio
        return a.file_id, (a.file_name or "audio.mp3"), (a.file_size or 0), (a.mime_type or "audio/mpeg")
    return None, None, 0, None


@Client.on_message(filters.command("link"))
async def cmd_link(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")

    replied = message.reply_to_message
    if not replied:
        return await message.reply(
            "↩️ Reply to any file, video or audio with `/link` to generate stream + download links."
        )

    file_id, name, size, mime = _get_file(replied)
    if not file_id:
        return await message.reply("❌ Reply to a video, document or audio file.")

    proc = await message.reply("⏳ Generating link…")

    token     = create_token(name, size, mime, tg_file_id=file_id)
    size_str  = _human_size(size)

    if STREAM_BASE_URL:
        base      = STREAM_BASE_URL.rstrip("/")
        watch_url = f"{base}/watch/{token}"
        dl_url    = f"{base}/dl/{token}"

        text = (
            f"🔗 **{name}**\n\n"
            f"💾 Size : `{size_str}`\n"
            f"📁 Type : `{mime}`\n"
            f"🚀 Backend : `Pyrogram MTProto`\n\n"
            f"⏳ _Links expire in {LINK_TTL // 3600}h_"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Watch Online", url=watch_url),
            InlineKeyboardButton("⬇️ Download",     url=dl_url),
        ]])
        await proc.delete()
        await message.reply(text, reply_markup=kb)
    else:
        # Stream server running on localhost only — use internal URL for now
        dl_url = f"http://127.0.0.1:{STREAM_PORT}/dl/{token}"
        await proc.edit(
            f"🔗 **{name}**\n"
            f"💾 Size: `{size_str}` | Backend: `Pyrogram MTProto ✅`\n\n"
            "⚠️ Set `STREAM_BASE_URL` in `.env` to generate public Watch/Download links.\n\n"
            "Example:\n`STREAM_BASE_URL=http://YOUR_VPS_IP:8080`"
        )
