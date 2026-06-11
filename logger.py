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
    Silently skipped if LOG_CHANNEL is 0 or not configured.
    """
    if not LOG_CHANNEL:
        return

    # Validate the channel ID looks usable before trying
    if LOG_CHANNEL > 0 or LOG_CHANNEL > -100:
        _log.debug("[logger] LOG_CHANNEL=%d looks invalid, skipping", LOG_CHANNEL)
        return

    user_line = _user_line(message)
    tag       = action.replace(" ", "_").upper().replace("-", "_")
    lines     = [
        f"**#{tag}**",
        "",
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
        # Log warning but never crash the handler
        _log.debug("[logger] Could not send log: %s", e)
