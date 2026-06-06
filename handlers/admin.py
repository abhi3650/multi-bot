"""
handlers/admin.py
Commands (admin-only): /restart  /addpremium  /removepremium
                       /pending  /broadcast  /stats
"""

import asyncio
import os
import sys
import time

from pyrogram import Client, filters
from pyrogram.types import Message

import database as db
from config import ADMIN_IDS, PREMIUM_DAYS


def _admin_filter(_, __, message: Message) -> bool:
    return (message.from_user is not None) and (message.from_user.id in ADMIN_IDS)

admin_only = filters.create(_admin_filter)


def register(app: Client):

    @app.on_message(filters.command("restart") & admin_only)
    async def cmd_restart(client: Client, message: Message):
        await message.reply("♻️ Restarting bot…")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @app.on_message(filters.command("addpremium") & admin_only)
    async def cmd_addpremium(client: Client, message: Message):
        args = message.command[1:]   # message.command[0] is the command name
        if not args:
            await message.reply("Usage: `/addpremium <user_id> [days]`")
            return
        try:
            uid  = int(args[0])
            days = int(args[1]) if len(args) > 1 else PREMIUM_DAYS
        except ValueError:
            await message.reply("❌ Invalid arguments.")
            return

        await db.grant_premium(uid, days)
        exp = time.strftime("%d %b %Y", time.localtime(time.time() + days * 86400))

        await message.reply(
            f"✅ Premium granted to `{uid}` for *{days} days* (expires {exp})."
        )
        try:
            await client.send_message(
                uid,
                f"🎉 *Premium Activated!*\n\n"
                f"Your Premium membership is active for *{days} days* (expires {exp}).\n\n"
                "Enjoy unlimited access! ⭐",
            )
        except Exception:
            pass

    @app.on_message(filters.command("removepremium") & admin_only)
    async def cmd_removepremium(client: Client, message: Message):
        args = message.command[1:]
        if not args:
            await message.reply("Usage: `/removepremium <user_id>`")
            return
        try:
            uid = int(args[0])
        except ValueError:
            await message.reply("❌ Invalid user ID.")
            return

        await db.revoke_premium(uid)
        await message.reply(f"✅ Premium removed from `{uid}`.")

    @app.on_message(filters.command("pending") & admin_only)
    async def cmd_pending(client: Client, message: Message):
        rows = await db.get_pending()
        if not rows:
            await message.reply("✅ No pending premium verifications.")
            return

        lines = ["📋 *Pending Premium Requests:*\n"]
        for r in rows:
            ts = time.strftime("%d %b %Y %H:%M", time.localtime(r["requested_at"]))
            lines.append(
                f"• User `{r['user_id']}` — UTR: `{r['utr']}`\n"
                f"  Requested: {ts}\n"
                f"  → `/addpremium {r['user_id']}` to approve"
            )
        await message.reply("\n".join(lines))

    @app.on_message(filters.command("stats") & admin_only)
    async def cmd_stats(client: Client, message: Message):
        all_ids       = await db.get_all_users()
        premium_count = sum(1 for uid in all_ids if await db.is_premium(uid))
        await message.reply(
            "📊 *Bot Statistics*\n\n"
            f"👥 Total Users  : `{len(all_ids)}`\n"
            f"⭐ Premium Users : `{premium_count}`\n"
            f"👤 Free Users   : `{len(all_ids) - premium_count}`"
        )

    @app.on_message(filters.command("broadcast") & admin_only)
    async def cmd_broadcast(client: Client, message: Message):
        args = message.command[1:]
        if not args:
            await message.reply("Usage: `/broadcast <your message>`")
            return

        text    = " ".join(args)
        all_ids = await db.get_all_users()
        status  = await message.reply(f"📤 Broadcasting to {len(all_ids)} users…")

        sent = failed = 0
        for uid in all_ids:
            try:
                await client.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

        await status.edit(
            f"✅ Broadcast done!\n"
            f"• Sent   : `{sent}`\n"
            f"• Failed : `{failed}`"
        )
