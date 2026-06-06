import time
from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
import database as db
from config import PREMIUM_PRICE, PREMIUM_DAYS, UPI_ID, FREE_MEDIA_LIMIT, FREE_POSTER_LIMIT

_awaiting_utr: set[int] = set()

HELP_TEXT = """
**📖 Command Reference**

━━━━━━━━━━━━━━━━━━
**🎬 Movie & OTT**
`/imdb <title>` — Detailed info from TMDB/IMDB
`/ott <title>` — Check streaming availability
`/posters <title>` — Browse movie posters

━━━━━━━━━━━━━━━━━━
**🎞 Video Tools** _(reply to video or pass URL)_
`/mediainfo` — Codec, resolution, bitrate
`/mediainfo <url>` — From any video URL
`/sample` — 30-second preview clip
`/sample <url>` — From any video URL
`/screenshot` — 10 evenly-spaced screenshots
`/screenshot <url>` — From any video URL
`/link` — Watch Online + Download link

━━━━━━━━━━━━━━━━━━
**🎵 Download**
`/yt <url>` — YouTube quality picker
`/song <name>` — Search & download MP3

━━━━━━━━━━━━━━━━━━
**📊 Account**
`/usage` — Monthly usage stats
`/premium` — Upgrade to Premium
"""


@Client.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")

    text = (
        f"👋 Hello, **{u.first_name}**!\n\n"
        "Your all-in-one Telegram utility bot.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "**🎬 Movie & OTT:** `/imdb` `/ott` `/posters`\n\n"
        "**🎞 Video Tools:** `/mediainfo` `/sample` `/screenshot` `/link`\n\n"
        "**🎵 Download:** `/yt` `/song`\n\n"
        "**📊 Account:** `/usage` `/premium` `/help`\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ Get Premium", callback_data="premium_info"),
        InlineKeyboardButton("❓ Help",        callback_data="show_help"),
    ]])
    await message.reply(text, reply_markup=kb)


@Client.on_message(filters.command("help"))
async def cmd_help(client: Client, message: Message):
    await message.reply(HELP_TEXT)


@Client.on_message(filters.command("usage"))
async def cmd_usage(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")
    user = await db.get_user(u.id)
    if not user:
        return await message.reply("❌ Error. Try again.")

    premium = bool(user["is_premium"]) and user["premium_expiry"] > time.time()
    status  = "⭐ Premium User" if premium else "👤 Free User"
    if premium:
        exp        = time.strftime("%d %b %Y", time.localtime(user["premium_expiry"]))
        status    += f" (expires {exp})"
        media_str  = f"{user['media_usage']} / ∞"
        poster_str = f"{user['poster_usage']} / ∞"
    else:
        media_str  = f"{user['media_usage']}/{FREE_MEDIA_LIMIT}"
        poster_str = f"{user['poster_usage']}/{FREE_POSTER_LIMIT}"

    text = (
        f"**📊 Your Usage** (`#{u.id}`)\n\n"
        f"Status : `{status}`\n\n"
        f"🎞 Video Tools  : `{media_str}`\n"
        f"🖼 Poster Search: `{poster_str}`\n\n"
        "_Limits reset every 30 days._"
    )
    kb = None
    if not premium:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="premium_info")
        ]])
    await message.reply(text, reply_markup=kb)


@Client.on_message(filters.command("premium"))
async def cmd_premium(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")
    await _show_premium(message)


async def _show_premium(msg_or_query):
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
        "3. Tap **Verify Payment** below\n"
        "4. Send your UTR/Transaction ID"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify Payment", callback_data="premium_verify")],
        [InlineKeyboardButton("❌ Cancel",          callback_data="premium_cancel")],
    ])
    if isinstance(msg_or_query, Message):
        await msg_or_query.reply(text, reply_markup=kb)
    else:
        await msg_or_query.message.edit(text, reply_markup=kb)


@Client.on_callback_query(filters.regex("^(premium_info|premium_verify|premium_cancel|show_help)$"))
async def premium_callback(client: Client, cq: CallbackQuery):
    await cq.answer()
    action = cq.data

    if action == "show_help":
        await cq.message.edit(HELP_TEXT, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 Back", callback_data="premium_cancel")
        ]]))

    elif action == "premium_info":
        await _show_premium(cq)

    elif action == "premium_verify":
        _awaiting_utr.add(cq.from_user.id)
        await cq.message.edit(
            "📤 Please send your **UTR / Transaction ID** now.\n\n"
            "_Your payment will be verified and Premium activated shortly._"
        )

    elif action == "premium_cancel":
        _awaiting_utr.discard(cq.from_user.id)
        await cq.message.delete()


@Client.on_message(filters.private & filters.text & filters.regex(r"^(?!/)"))
async def text_handler(client: Client, message: Message):
    """Captures plain text messages — used to collect UTR codes for premium payment."""
    uid = message.from_user.id
    if uid not in _awaiting_utr:
        return
    utr = message.text.strip()
    if len(utr) < 6:
        return await message.reply("❌ That doesn't look like a valid UTR. Please try again.")
    await db.add_pending(uid, utr)
    _awaiting_utr.discard(uid)
    await message.reply(
        f"✅ UTR `{utr}` received!\n\nYour payment is being verified. "
        "Premium will be activated within a few minutes.",
    )
