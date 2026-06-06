"""
handlers/user.py
Commands: /start  /help  /usage  /premium
"""

import time

from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from config import (
    PREMIUM_PRICE, PREMIUM_DAYS, UPI_ID, BOT_USERNAME,
    FREE_MEDIA_LIMIT, FREE_POSTER_LIMIT,
)

# ── State store: users waiting to send UTR ────────────────────────────────────
_awaiting_utr: set[int] = set()

HELP_TEXT = """
📖 *Command Reference*

━━━━━━━━━━━━━━━━━━
🎬 *Movie & OTT*

`/imdb <title>` — Detailed info from TMDB/IMDB
`/ott <title>` — Check streaming availability
`/posters <title>` — Browse movie posters

━━━━━━━━━━━━━━━━━━
🎞 *Video Tools*

`/mediainfo` — Codec, resolution, bitrate _(reply to video)_
`/sample` — 30-second preview clip _(reply to video)_
`/screenshot` — 10 screenshots _(reply to video)_
`/yt <url>` — YouTube quality picker + direct link
`/song <name>` — Search YouTube Music → pick → get MP3
`/link` — Watch Online + Download link _(reply to file)_

━━━━━━━━━━━━━━━━━━
📊 *Account*

`/usage` — See your usage stats
`/premium` — Upgrade for unlimited access
`/help` — This message
━━━━━━━━━━━━━━━━━━
_For issues, contact the bot admin._
"""


def register(app: Client):

    # ── /start ────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("start") & filters.private)
    async def cmd_start(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)

        text = (
            f"👋 Hello, *{u.first_name}*!\n\n"
            f"Welcome to *@{BOT_USERNAME}* — your all-in-one Telegram utility bot.\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📽 *Movie & OTT*\n"
            "`/imdb` — Movie/Series info\n"
            "`/ott` — OTT availability\n"
            "`/posters` — Movie posters\n\n"
            "🎞 *Video Tools*\n"
            "`/mediainfo` — Technical media info\n"
            "`/sample` — Generate 30s sample clip\n"
            "`/screenshot` — Take 10 screenshots\n"
            "`/link` — Generate Watch + Download link\n"
            "`/yt` — YouTube download link\n"
            "`/song` — Search & download song as MP3\n\n"
            "📊 *Account*\n"
            "`/usage` — Your usage stats\n"
            "`/premium` — Upgrade to Premium\n"
            "`/help` — Full command list\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⭐ Get Premium", callback_data="premium_info"),
                InlineKeyboardButton("❓ Help",        callback_data="show_help"),
            ]
        ])
        await message.reply(text, reply_markup=keyboard)

    # ── /help ─────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("help"))
    async def cmd_help(client: Client, message: Message):
        await message.reply(HELP_TEXT)

    # ── /usage ────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("usage") & filters.private)
    async def cmd_usage(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)

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
            f"📊 *Your Usage* (`#{u.id}`)\n\n"
            f"Status : `{status}`\n\n"
            f"🎞 Video Tools  : `{media_str}`\n"
            f"🖼 Poster Search: `{poster_str}`\n\n"
            "_Limits reset every 30 days._"
        )

        if not is_prem:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="premium_info")
            ]])
            await message.reply(text, reply_markup=keyboard)
        else:
            await message.reply(text)

    # ── /premium ──────────────────────────────────────────────────────────────

    @app.on_message(filters.command("premium") & filters.private)
    async def cmd_premium(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)
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
                "📤 Please send your *UTR / Transaction ID* now.\n\n"
                "_Your payment will be verified and Premium activated within a few minutes._"
            )

        elif action == "premium_cancel":
            await query.message.delete()

        elif action == "show_help":
            await query.message.edit(HELP_TEXT)

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
        )


async def _send_premium_info(target, edit: bool = False):
    text = (
        "⭐ *Premium Membership*\n\n"
        f"➠ Price : *₹{PREMIUM_PRICE} / Month*\n"
        f"➠ Period: {PREMIUM_DAYS} Days\n\n"
        "✅ *Benefits:*\n"
        "• Unlimited video tools (mediainfo, sample, screenshots)\n"
        "• Unlimited poster searches\n"
        "• Priority support\n\n"
        "💳 *How To Buy:*\n"
        f"1. Send ₹{PREMIUM_PRICE} to UPI: `{UPI_ID}`\n"
        "2. Wait 30 seconds\n"
        "3. Click *Verify* below\n"
        "4. Send your UTR/Transaction ID\n\n"
        "_For queries, contact the admin._"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Payment", callback_data="premium_verify")],
        [InlineKeyboardButton("❌ Cancel",         callback_data="premium_cancel")],
    ])
    if edit:
        await target.edit(text, reply_markup=keyboard)
    else:
        await target.reply(text, reply_markup=keyboard)
