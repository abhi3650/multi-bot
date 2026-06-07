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

MD = ParseMode.MARKDOWN

# ── State ─────────────────────────────────────────────────────────────────────
# Users who have clicked "Verify Payment" and are expected to send their UTR next
_awaiting_utr: set[int] = set()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


# ── Static texts ──────────────────────────────────────────────────────────────

HELP_TEXT = (
    "📖 **Command Reference**\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🎬 **Movie & OTT**\n\n"
    "`/imdb <title>` — Detailed info from TMDB/IMDB\n"
    "`/ott <title>` — Check streaming availability\n"
    "`/posters <title>` — Browse movie posters\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🎞 **Video Tools**\n\n"
    "`/mediainfo` — Codec, resolution, bitrate _(reply to video)_\n"
    "`/sample` — 30-second preview clip _(reply to video)_\n"
    "`/screenshot` — 10 screenshots _(reply to video)_\n"
    "`/yt <url>` — YouTube quality picker + direct link\n"
    "`/song <name>` — Search YouTube Music, get MP3\n"
    "`/link` — Watch Online + Download link _(reply to file)_\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📊 **Account**\n\n"
    "`/usage` — See your usage stats\n"
    "`/premium` — Upgrade for unlimited access\n"
    "`/help` — This message\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "_For issues, contact the bot admin._"
)


def register(app: Client):

    # ── /start ────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("start") & filters.private)
    async def cmd_start(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        text = (
            f"👋 Hello, **{u.first_name}**!\n\n"
            f"Welcome to **@{BOT_USERNAME}** — your all-in-one Telegram utility bot.\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📽 **Movie & OTT**\n"
            "`/imdb` — Movie/Series info\n"
            "`/ott` — OTT availability\n"
            "`/posters` — Movie posters\n\n"
            "🎞 **Video Tools**\n"
            "`/mediainfo` — Technical media info\n"
            "`/sample` — 30s sample clip\n"
            "`/screenshot` — 10 screenshots\n"
            "`/link` — Watch + Download link\n"
            "`/yt` — YouTube download\n"
            "`/song` — Search & download MP3\n\n"
            "📊 **Account**\n"
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
        await message.reply(text, parse_mode=MD, reply_markup=keyboard)

    # ── /help ─────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("help") & filters.private)
    async def cmd_help(client: Client, message: Message):
        await message.reply(HELP_TEXT, parse_mode=MD)

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

        if is_prem:
            exp        = time.strftime("%d %b %Y", time.localtime(row["premium_expiry"]))
            status     = f"⭐ Premium User _(expires {exp})_"
            media_str  = f"{row['media_usage']} / ∞"
            poster_str = f"{row['poster_usage']} / ∞"
        else:
            status     = "👤 Free User"
            media_str  = f"{row['media_usage']}/{FREE_MEDIA_LIMIT}"
            poster_str = f"{row['poster_usage']}/{FREE_POSTER_LIMIT}"

        text = (
            f"📊 **Your Usage** (`#{u.id}`)\n\n"
            f"Status   : {status}\n\n"
            f"🎞 Video Tools   : `{media_str}`\n"
            f"🖼 Poster Search : `{poster_str}`\n\n"
            "_Limits reset every 30 days._"
        )

        if not is_prem:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="premium_info")
            ]])
            await message.reply(text, parse_mode=MD, reply_markup=keyboard)
        else:
            await message.reply(text, parse_mode=MD)

    # ── /premium ──────────────────────────────────────────────────────────────
    @app.on_message(filters.command("premium") & filters.private)
    async def cmd_premium(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))
        await _send_premium_info(message)

    # ── Callback queries ───────────────────────────────────────────────────────
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
                parse_mode=MD,
            )

        elif action == "premium_cancel":
            await query.message.delete()

        elif action == "show_help":
            await query.message.edit(HELP_TEXT, parse_mode=MD)

    # ── UTR capture ───────────────────────────────────────────────────────────
    # CRITICAL: Use a custom filter function — NOT ~filters.command("")
    # which is broken in Pyrogram. We check explicitly that the message
    # does NOT start with "/" so commands are never intercepted here.
    def _is_utr_message(_, __, message: Message) -> bool:
        if not message.text:
            return False
        if message.text.startswith("/"):   # never intercept commands
            return False
        if message.from_user is None:
            return False
        return message.from_user.id in _awaiting_utr

    utr_filter = filters.create(_is_utr_message)

    @app.on_message(filters.private & filters.text & utr_filter)
    async def handle_utr(client: Client, message: Message):
        uid = message.from_user.id
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
            parse_mode=MD,
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
        "3. Click **Verify** below and send your UTR/Transaction ID\n\n"
        "_For queries, contact the admin._"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Payment", callback_data="premium_verify")],
        [InlineKeyboardButton("❌ Cancel",         callback_data="premium_cancel")],
    ])
    if edit:
        await target.edit(text, parse_mode=MD, reply_markup=keyboard)
    else:
        await target.reply(text, parse_mode=MD, reply_markup=keyboard)
