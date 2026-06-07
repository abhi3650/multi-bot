"""
handlers/user.py
Commands: /start  /help  /usage  /premium
"""

import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from config import (
    PREMIUM_PRICE, PREMIUM_DAYS, UPI_ID, BOT_USERNAME,
    FREE_MEDIA_LIMIT, FREE_POSTER_LIMIT,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_awaiting_utr: set[int] = set()


def _full_name(user) -> str:
    """Safely build full name from Pyrogram User object."""
    parts = [user.first_name or "", user.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


def md(text: str) -> dict:
    """Shorthand kwargs for Markdown parse mode."""
    return {"parse_mode": ParseMode.MARKDOWN}


# ── Texts ─────────────────────────────────────────────────────────────────────

HELP_TEXT = (
    "📖 **Command Reference**\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🎬 **Movie & OTT**\n\n"
    "`/imdb` `<title>` — Detailed info from TMDB/IMDB\n"
    "`/ott` `<title>` — Check streaming availability\n"
    "`/posters` `<title>` — Browse movie posters\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🎞 **Video Tools**\n\n"
    "`/mediainfo` — Codec, resolution, bitrate _(reply to video)_\n"
    "`/sample` — 30-second preview clip _(reply to video)_\n"
    "`/screenshot` — 10 screenshots _(reply to video)_\n"
    "`/yt` `<url>` — YouTube quality picker + direct link\n"
    "`/song` `<name>` — Search YouTube Music, get MP3\n"
    "`/link` — Watch Online + Download link _(reply to file)_\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📊 **Account**\n\n"
    "`/usage` — See your usage stats\n"
    "`/premium` — Upgrade for unlimited access\n"
    "`/help` — This message\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "_For issues, contact the bot admin._"
)

START_TEXT = (
    "👋 Hello, **{name}**!\n\n"
    "Welcome to **@{bot}** — your all-in-one Telegram utility bot.\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📽 **Movie & OTT**\n"
    "`/imdb` — Movie/Series info\n"
    "`/ott` — OTT availability\n"
    "`/posters` — Movie posters\n\n"
    "🎞 **Video Tools**\n"
    "`/mediainfo` — Technical media info\n"
    "`/sample` — Generate 30s sample clip\n"
    "`/screenshot` — Take 10 screenshots\n"
    "`/link` — Generate Watch + Download link\n"
    "`/yt` — YouTube download link\n"
    "`/song` — Search & download song as MP3\n\n"
    "📊 **Account**\n"
    "`/usage` — Your usage stats\n"
    "`/premium` — Upgrade to Premium\n"
    "`/help` — Full command list\n"
    "━━━━━━━━━━━━━━━━━━"
)


def register(app: Client):

    # ── /start ────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("start") & filters.private)
    async def cmd_start(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⭐ Get Premium", callback_data="premium_info"),
                InlineKeyboardButton("❓ Help",        callback_data="show_help"),
            ]
        ])
        await message.reply(
            START_TEXT.format(name=u.first_name, bot=BOT_USERNAME),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

    # ── /help ─────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("help"))
    async def cmd_help(client: Client, message: Message):
        await message.reply(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    # ── /usage ────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("usage") & filters.private)
    async def cmd_usage(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        row = await db.get_user(u.id)
        if not row:
            await message.reply("❌ Something went wrong. Try again.")
            return

        is_prem = bool(row["is_premium"]) and row["premium_expiry"] > time.time()
        status  = "⭐ Premium User" if is_prem else "👤 Free User"

        if is_prem:
            exp        = time.strftime("%d %b %Y", time.localtime(row["premium_expiry"]))
            status    += f" (expires {exp})"
            media_str  = f"{row['media_usage']} / ∞"
            poster_str = f"{row['poster_usage']} / ∞"
        else:
            media_str  = f"{row['media_usage']}/{FREE_MEDIA_LIMIT}"
            poster_str = f"{row['poster_usage']}/{FREE_POSTER_LIMIT}"

        text = (
            f"📊 **Your Usage** (`#{u.id}`)\n\n"
            f"Status  : `{status}`\n\n"
            f"🎞 Video Tools   : `{media_str}`\n"
            f"🖼 Poster Search : `{poster_str}`\n\n"
            "_Limits reset every 30 days._"
        )

        if not is_prem:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="premium_info")
            ]])
            await message.reply(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        else:
            await message.reply(text, parse_mode=ParseMode.MARKDOWN)

    # ── /premium ──────────────────────────────────────────────────────────────

    @app.on_message(filters.command("premium") & filters.private)
    async def cmd_premium(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))
        await _send_premium_info(message)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^(premium_info|premium_verify|premium_cancel|show_help)$"))
    async def on_premium_cb(client: Client, query: CallbackQuery):
        action = query.data
        await query.answer()

        if action == "premium_info":
            await _send_premium_info(query.message, edit=True)

        elif action == "premium_verify":
            _awaiting_utr.add(query.from_user.id)
            await query.message.edit(
                "📤 Please send your **UTR / Transaction ID** now.\n\n"
                "_Your payment will be verified and Premium activated within a few minutes._",
                parse_mode=ParseMode.MARKDOWN,
            )

        elif action == "premium_cancel":
            await query.message.delete()

        elif action == "show_help":
            await query.message.edit(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    # ── UTR capture (plain text in private) ───────────────────────────────────

    @app.on_message(filters.text & filters.private & ~filters.command(""))
    async def handle_utr(client: Client, message: Message):
        uid = message.from_user.id
        if uid not in _awaiting_utr:
            return

        utr = message.text.strip()
        if len(utr) < 6:
            await message.reply("❌ That doesn't look like a valid UTR. Please try again.")
            return

        await db.add_pending(uid, utr)
        _awaiting_utr.discard(uid)

        await message.reply(
            f"✅ UTR `{utr}` received!\n\n"
            "Your payment is being verified. "
            "Premium will be activated within a few minutes.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def _send_premium_info(target, edit: bool = False):
    text = (
        "⭐ **Premium Membership**\n\n"
        f"➠ Price  : **₹{PREMIUM_PRICE} / Month**\n"
        f"➠ Period : {PREMIUM_DAYS} Days\n\n"
        "✅ **Benefits:**\n"
        "• Unlimited video tools (mediainfo, sample, screenshots)\n"
        "• Unlimited poster searches\n"
        "• Priority support\n\n"
        "💳 **How To Buy:**\n"
        f"1. Send ₹{PREMIUM_PRICE} to UPI: `{UPI_ID}`\n"
        "2. Wait 30 seconds\n"
        "3. Click **Verify** below\n"
        "4. Send your UTR/Transaction ID\n\n"
        "_For queries, contact the admin._"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Payment", callback_data="premium_verify")],
        [InlineKeyboardButton("❌ Cancel",         callback_data="premium_cancel")],
    ])
    if edit:
        await target.edit(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    else:
        await target.reply(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
