import asyncio
import os
import sys
import time

from pyrogram import Client, filters
from pyrogram.types import Message

import database as db
from config import ADMIN_IDS, PREMIUM_DAYS

admin_filter = filters.user(ADMIN_IDS) if ADMIN_IDS else filters.user([])


@Client.on_message(filters.command("restart") & admin_filter)
async def cmd_restart(client: Client, message: Message):
    await message.reply("♻️ Restarting…")
    os.execv(sys.executable, [sys.executable] + sys.argv)


@Client.on_message(filters.command("addpremium") & admin_filter)
async def cmd_addpremium(client: Client, message: Message):
    args = message.command[1:]
    if not args:
        return await message.reply("Usage: `/addpremium <user_id> [days]`")
    try:
        uid  = int(args[0])
        days = int(args[1]) if len(args) > 1 else PREMIUM_DAYS
    except ValueError:
        return await message.reply("❌ Invalid arguments.")

    await db.grant_premium(uid, days)
    exp = time.strftime("%d %b %Y", time.localtime(time.time() + days * 86400))
    await message.reply(f"✅ Premium granted to `{uid}` for **{days} days** (expires {exp}).")
    try:
        await client.send_message(uid,
            f"🎉 **Premium Activated!**\n\nYour membership is now active for **{days} days** (expires {exp}).\nEnjoy unlimited access! ⭐")
    except Exception:
        pass


@Client.on_message(filters.command("removepremium") & admin_filter)
async def cmd_removepremium(client: Client, message: Message):
    args = message.command[1:]
    if not args:
        return await message.reply("Usage: `/removepremium <user_id>`")
    try:
        uid = int(args[0])
    except ValueError:
        return await message.reply("❌ Invalid user ID.")
    await db.revoke_premium(uid)
    await message.reply(f"✅ Premium removed from `{uid}`.")


@Client.on_message(filters.command("pending") & admin_filter)
async def cmd_pending(client: Client, message: Message):
    rows = await db.get_pending()
    if not rows:
        return await message.reply("✅ No pending premium verifications.")
    lines = ["📋 **Pending Premium Requests:**\n"]
    for r in rows:
        ts = time.strftime("%d %b %Y %H:%M", time.localtime(r["requested_at"]))
        lines.append(f"• User `{r['user_id']}` — UTR: `{r['utr']}`\n  Requested: {ts}\n  → `/addpremium {r['user_id']}` to approve")
    await message.reply("\n".join(lines))


@Client.on_message(filters.command("stats") & admin_filter)
async def cmd_stats(client: Client, message: Message):
    all_ids = await db.get_all_users()
    premium = sum(1 for uid in all_ids if await db.is_premium(uid))
    await message.reply(
        f"📊 **Bot Statistics**\n\n"
        f"👥 Total Users  : `{len(all_ids)}`\n"
        f"⭐ Premium Users: `{premium}`\n"
        f"👤 Free Users   : `{len(all_ids) - premium}`"
    )


@Client.on_message(filters.command("broadcast") & admin_filter)
async def cmd_broadcast(client: Client, message: Message):
    args = message.command[1:]
    if not args:
        return await message.reply("Usage: `/broadcast <message>`\n_Supports Markdown formatting._")

    text    = " ".join(args)
    all_ids = await db.get_all_users()
    msg     = await message.reply(f"📤 Broadcasting to {len(all_ids)} users…")
    sent = failed = 0

    for uid in all_ids:
        try:
            await client.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await msg.edit(
        f"✅ **Broadcast done!**\n"
        f"• Sent   : `{sent}`\n"
        f"• Failed : `{failed}`"
    )
