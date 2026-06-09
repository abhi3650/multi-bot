"""
logger.py — Activity logger

Sends structured log messages to LOG_CHANNEL for every major bot action.
Each log includes:
  • Action type (emoji tag)
  • User name + ID (clickable mention)
  • Relevant details (file name, query, etc.)

Usage:
    from logger import log_action
    await log_action(client, message, "🎵 Song", "Blinding Lights — Charlie Brown")
"""

import logging
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message

from config import LOG_CHANNEL, BOT_USERNAME

_log = logging.getLogger(__name__)


def _user_line(message: Message) -> str:
    u = message.from_user
    if not u:
        return "Unknown User"
    name = (u.first_name or "") + (" " + u.last_name if u.last_name else "")
    name = name.strip() or "Unknown"
    return f"[{name}](tg://user?id={u.id}) (`{u.id}`)"


async def log_action(client: Client, message: Message, action: str, detail: str = "") -> None:
    """
    Send an activity log to LOG_CHANNEL.

    Args:
        client:  Pyrogram bot client
        message: The triggering Message (for user info)
        action:  Short label e.g. "🎵 Song Download", "📸 Screenshot"
        detail:  Additional context e.g. song title, file name, query
    """
    if not LOG_CHANNEL:
        return

    user_line = _user_line(message)
    lines = [
        f"**#{action.replace(' ', '_').upper()}**",
        f"",
        f"👤 **User** : {user_line}",
    ]
    if detail:
        lines.append(f"📌 **Detail**: {detail}")

    text = "\n".join(lines)

    try:
        await client.send_message(
            LOG_CHANNEL,
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception as e:
        _log.warning("[logger] Failed to send log: %s", e)
