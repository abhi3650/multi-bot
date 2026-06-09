"""
handlers/admin.py  —  Admin-only commands

Public commands (shown in bot menu):
  /addpremium  /removepremium  /pending  /stats  /broadcast  /restart

Secret commands (NOT in bot menu, only admins know):
  /cook  — upload / view / delete YouTube cookies.txt
           This command is intentionally omitted from setMyCommands so it
           doesn't appear in the command list for anyone.
"""

import asyncio
import os
import sys
import time

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, Document,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from config import ADMIN_IDS, PREMIUM_DAYS

MD = ParseMode.MARKDOWN


def _admin_filter(_, __, message: Message) -> bool:
    return (message.from_user is not None) and (message.from_user.id in ADMIN_IDS)

admin_only = filters.create(_admin_filter)

# Track admins who have typed /cook and are now expected to send a file
_awaiting_cookie_upload: set[int] = set()


def register(app: Client):

    # ── /restart ──────────────────────────────────────────────────────────────
    @app.on_message(filters.command("restart") & admin_only & filters.private)
    async def cmd_restart(client: Client, message: Message):
        await message.reply("♻️ Restarting bot…")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── /addpremium ───────────────────────────────────────────────────────────
    @app.on_message(filters.command("addpremium") & admin_only & filters.private)
    async def cmd_addpremium(client: Client, message: Message):
        args = message.command[1:]
        if not args:
            await message.reply(
                "**Usage:** `/addpremium <user_id> [days]`\n"
                "**Example:** `/addpremium 123456789 30`",
                parse_mode=MD,
            )
            return
        try:
            uid  = int(args[0])
            days = int(args[1]) if len(args) > 1 else PREMIUM_DAYS
        except ValueError:
            await message.reply("❌ Invalid arguments. User ID must be a number.")
            return

        await db.grant_premium(uid, days)
        exp = time.strftime("%d %b %Y", time.localtime(time.time() + days * 86400))

        await message.reply(
            f"✅ **Premium granted!**\n\n"
            f"👤 User   : `{uid}`\n"
            f"📅 Period : **{days} days**\n"
            f"⏳ Expires: {exp}",
            parse_mode=MD,
        )
        try:
            await client.send_message(
                uid,
                f"🎉 **Premium Activated!**\n\n"
                f"Your Premium membership is now active for **{days} days** "
                f"(expires {exp}).\n\nEnjoy unlimited access! ⭐",
                parse_mode=MD,
            )
        except Exception:
            pass

    # ── /removepremium ────────────────────────────────────────────────────────
    @app.on_message(filters.command("removepremium") & admin_only & filters.private)
    async def cmd_removepremium(client: Client, message: Message):
        args = message.command[1:]
        if not args:
            await message.reply("**Usage:** `/removepremium <user_id>`", parse_mode=MD)
            return
        try:
            uid = int(args[0])
        except ValueError:
            await message.reply("❌ Invalid user ID. Must be a number.")
            return

        await db.revoke_premium(uid)
        await message.reply(f"✅ Premium removed from `{uid}`.", parse_mode=MD)

    # ── /pending ──────────────────────────────────────────────────────────────
    @app.on_message(filters.command("pending") & admin_only & filters.private)
    async def cmd_pending(client: Client, message: Message):
        rows = await db.get_pending()
        if not rows:
            await message.reply("✅ No pending premium verifications.")
            return

        lines = ["📋 **Pending Premium Requests**\n"]
        for r in rows:
            ts = time.strftime("%d %b %Y %H:%M", time.localtime(r["requested_at"]))
            lines.append(
                f"👤 User `{r['user_id']}`\n"
                f"   UTR: `{r['utr']}`\n"
                f"   At : {ts}\n"
                f"   ✅ `/addpremium {r['user_id']}` to approve\n"
            )
        await message.reply("\n".join(lines), parse_mode=MD)

    # ── /stats ────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("stats") & admin_only & filters.private)
    async def cmd_stats(client: Client, message: Message):
        all_ids = await db.get_all_users()
        prem    = 0
        for uid in all_ids:
            if await db.is_premium(uid):
                prem += 1
        free    = len(all_ids) - prem
        meta    = await db.get_cookies_meta()
        cookie_status = "✅ Loaded" if meta else "❌ Not uploaded"
        if meta:
            updated = time.strftime("%d %b %Y %H:%M", time.localtime(meta.get("updated_at", 0)))
            size_kb = meta.get("size", 0) / 1024
            cookie_status += f" ({size_kb:.1f} KB, updated {updated})"

        await message.reply(
            "📊 **Bot Statistics**\n\n"
            f"👥 Total Users   : `{len(all_ids)}`\n"
            f"⭐ Premium Users  : `{prem}`\n"
            f"👤 Free Users    : `{free}`\n\n"
            f"🍪 YT Cookies    : {cookie_status}",
            parse_mode=MD,
        )

    # ── /broadcast ────────────────────────────────────────────────────────────
    @app.on_message(filters.command("broadcast") & admin_only & filters.private)
    async def cmd_broadcast(client: Client, message: Message):
        args = message.command[1:]
        if not args:
            await message.reply(
                "**Usage:** `/broadcast <message>`\n_Sends to every user._",
                parse_mode=MD,
            )
            return

        text    = " ".join(args)
        all_ids = await db.get_all_users()
        status  = await message.reply(
            f"📤 Broadcasting to **{len(all_ids)}** users…", parse_mode=MD
        )

        sent = failed = 0
        for uid in all_ids:
            try:
                await client.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)

        await status.edit(
            "✅ **Broadcast complete!**\n\n"
            f"📨 Sent   : `{sent}`\n"
            f"❌ Failed : `{failed}`",
            parse_mode=MD,
        )

    # ── /cook — SECRET cookie management command ───────────────────────────────
    # Not registered in setMyCommands — only admins who know the command can use it.

    @app.on_message(filters.command("cook") & admin_only & filters.private)
    async def cmd_cook(client: Client, message: Message):
        """
        /cook          — show current cookie status + action buttons
        /cook delete   — delete stored cookies immediately
        """
        args = message.command[1:]

        if args and args[0].lower() == "delete":
            deleted = await db.delete_cookies()
            if deleted:
                await message.reply(
                    "🗑 **YouTube cookies deleted.**\n\n"
                    "_Bot will use cookieless mode until you upload new ones._",
                    parse_mode=MD,
                )
            else:
                await message.reply("ℹ️ No cookies were stored.")
            return

        # Show status + upload instructions
        meta = await db.get_cookies_meta()

        if meta:
            updated    = time.strftime("%d %b %Y %H:%M", time.localtime(meta.get("updated_at", 0)))
            size_kb    = meta.get("size", 0) / 1024
            status_txt = (
                f"🍪 **YouTube Cookies — Active**\n\n"
                f"📦 Size    : `{size_kb:.1f} KB`\n"
                f"🕐 Updated : `{updated}`\n\n"
                "To replace: send a new `cookies.txt` file now.\n"
                "To delete : `/cook delete`"
            )
        else:
            status_txt = (
                "🍪 **YouTube Cookies — Not Set**\n\n"
                "Send your `cookies.txt` file now to upload it.\n\n"
                "**How to get cookies.txt:**\n"
                "1. Install _\"Get cookies.txt LOCALLY\"_ in Chrome\n"
                "2. Log into YouTube\n"
                "3. Click the extension → Export → Save as `cookies.txt`\n"
                "4. Send that file here"
            )

        _awaiting_cookie_upload.add(message.from_user.id)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Delete Cookies", callback_data="cook_delete"),
            InlineKeyboardButton("❌ Cancel",         callback_data="cook_cancel"),
        ]])

        await message.reply(status_txt, parse_mode=MD, reply_markup=keyboard)

    # Inline buttons for /cook
    @app.on_callback_query(filters.regex(r"^cook_(delete|cancel)$") & filters.create(
        lambda _, __, q: q.from_user.id in ADMIN_IDS
    ))
    async def cook_cb(client: Client, query):
        await query.answer()
        action = query.data.split("_")[1]

        if action == "delete":
            deleted = await db.delete_cookies()
            _awaiting_cookie_upload.discard(query.from_user.id)
            if deleted:
                await query.message.edit(
                    "🗑 **Cookies deleted.**\n"
                    "_Bot is now in cookieless mode._",
                    parse_mode=MD,
                )
            else:
                await query.message.edit("ℹ️ No cookies were stored.")

        elif action == "cancel":
            _awaiting_cookie_upload.discard(query.from_user.id)
            await query.message.delete()

    # Document handler — captures the cookies.txt file sent after /cook
    def _is_cookie_upload(_, __, message: Message) -> bool:
        """Fire only when an admin is in cookie-upload mode AND sends a document."""
        if not message.from_user or message.from_user.id not in ADMIN_IDS:
            return False
        if message.from_user.id not in _awaiting_cookie_upload:
            return False
        return message.document is not None

    cookie_upload_filter = filters.create(_is_cookie_upload)

    @app.on_message(filters.private & cookie_upload_filter)
    async def handle_cookie_upload(client: Client, message: Message):
        uid  = message.from_user.id
        doc  = message.document

        # Basic validation
        fname = doc.file_name or ""
        if not (fname.endswith(".txt") or "cookie" in fname.lower()):
            await message.reply(
                "❌ That doesn't look like a cookies.txt file.\n"
                "Please send the Netscape-format cookies file exported from your browser.",
            )
            return

        if doc.file_size and doc.file_size > 5 * 1024 * 1024:   # 5 MB sanity cap
            await message.reply("❌ File too large. A valid cookies.txt should be well under 1 MB.")
            return

        wait = await message.reply("⏳ Processing cookies file…")

        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as tmp:
            path = _os.path.join(tmp, "cookies.txt")
            await client.download_media(message.document, file_name=path)

            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                await wait.edit(f"❌ Could not read file:\n`{e}`", parse_mode=MD)
                return

        # Validate Netscape cookie format
        lines = content.strip().splitlines()
        if not lines:
            await wait.edit("❌ File is empty.")
            return

        has_header = any("Netscape HTTP Cookie File" in l for l in lines[:3])
        has_data   = any(
            not l.startswith("#") and len(l.split("\t")) >= 6
            for l in lines
        )
        if not has_header and not has_data:
            await wait.edit(
                "❌ This doesn't look like a valid Netscape cookies file.\n\n"
                "Make sure you export from the _\"Get cookies.txt LOCALLY\"_ extension.",
                parse_mode=MD,
            )
            return

        await db.save_cookies(content)
        _awaiting_cookie_upload.discard(uid)

        # Count cookie entries for feedback
        cookie_count = sum(
            1 for l in lines
            if l.strip() and not l.startswith("#") and len(l.split("\t")) >= 6
        )
        size_kb = len(content.encode()) / 1024

        # Delete the wait message and the uploaded file message for clean chat
        import asyncio as _asyncio
        await wait.delete()
        try:
            await message.delete()
        except Exception:
            pass

        # Send confirmation that auto-deletes after 10 seconds
        confirm = await message.reply(
            "✅ **YouTube cookies uploaded successfully!**\n\n"
            f"🍪 Cookies : `{cookie_count}` entries\n"
            f"📦 Size    : `{size_kb:.1f} KB`\n\n"
            "_This message will be deleted in 10 seconds._",
            parse_mode=MD,
        )
        await _asyncio.sleep(10)
        try:
            await confirm.delete()
        except Exception:
            pass
