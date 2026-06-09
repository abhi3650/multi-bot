"""
handlers/movie.py  —  /imdb  /ott  /posters

/ott uses ONLY the JustWatch Content Partner API v2:
  Base: https://apis.justwatch.com/contentpartner/v2/content
  Search: GET /titles/object_type/all/locale/en_IN?query=<title>&token=<TOKEN>
  Detail: GET /offers/object_type/{type}/id_type/tmdb/locale/en_IN?id=<tmdb_id>&token=<TOKEN>

  No GraphQL. No TMDB fallback for OTT. No env var check inside handlers.
  If  is empty the command still works — it uses TMDB for
  the search result picker but shows a "token not configured" notice for offers.

/posters UI: type picker → paginated browsing with <<  <  N/T  >  >>  Back  Close
"""

import hashlib
import logging

import httpx
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from config import TMDB_API_KEY, TMDB_BASE, TMDB_IMG, BOT_USERNAME
import justwatch as jw
from logger import log_action

MD  = ParseMode.MARKDOWN
log = logging.getLogger(__name__)

# Session stores
_ott_sessions:    dict[str, list] = {}
_poster_sessions: dict[int, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


def _sk(seed: str) -> str:
    return hashlib.md5(seed.encode()).hexdigest()[:10]


async def _tmdb_search(query: str, hx: httpx.AsyncClient) -> dict | None:
    r = await hx.get(
        f"{TMDB_BASE}/search/multi",
        params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "false"},
    )
    results = r.json().get("results", [])
    return next((x for x in results if x.get("media_type") in ("movie", "tv")), None)


async def _tmdb_detail(mtype: str, tmdb_id: int, hx: httpx.AsyncClient) -> dict:
    r = await hx.get(
        f"{TMDB_BASE}/{mtype}/{tmdb_id}",
        params={"api_key": TMDB_API_KEY, "append_to_response": "credits,external_ids,images"},
    )
    return r.json()


# ── JustWatch Content Partner API v2 ─────────────────────────────────────────

def _build_providers(offers: list) -> dict[str, list[str]]:
    """Group offers by monetization_type → list of provider names."""
    providers: dict[str, list[str]] = {}
    if not offers:
        return providers
    for offer in offers:
        mtype = (offer.get("monetization_type") or "").lower()
        # Get provider name from provider_id — JustWatch gives numeric IDs here.
        # We'll show the presentation_type (HD/SD) alongside price if available.
        price   = offer.get("retail_price") or 0
        ptype   = offer.get("presentation_type", "").upper()
        # Build a label: for flatrate/free we just need the name.
        # JustWatch Content Partner API embeds provider info differently than GraphQL.
        # The provider_id alone isn't useful without a lookup; use the offer URL host
        # as a fallback display name.
        url     = (offer.get("urls") or {}).get("standard_web", "")
        domain  = url.split("/")[2] if url and "/" in url else ""
        # Strip common prefixes
        p_label = domain.replace("www.", "").split(".")[0].capitalize() if domain else f"Provider-{offer.get('provider_id','?')}"
        if price and mtype in ("buy", "rent"):
            p_label += f" (₹{price:.0f} {ptype})"
        providers.setdefault(mtype, [])
        if p_label not in providers[mtype]:
            providers[mtype].append(p_label)
    return providers


# ── /imdb ─────────────────────────────────────────────────────────────────────

def register(app: Client):

    @app.on_message(filters.command("imdb") & filters.private)
    async def cmd_imdb(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        args = message.command[1:]
        if not args:
            await message.reply(
                "🎬 **IMDB Info Lookup**\n\n"
                "**Usage:** `/imdb <movie or series name>`\n"
                "**Example:** `/imdb Interstellar`",
                parse_mode=MD,
            )
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Searching for **{query}**…", parse_mode=MD)

        async with httpx.AsyncClient(timeout=15) as hx:
            hit = await _tmdb_search(query, hx)
            if not hit:
                await wait.edit("❌ No results found. Try a different title.")
                return
            mtype  = hit["media_type"]
            detail = await _tmdb_detail(mtype, hit["id"], hx)

        title    = detail.get("title") or detail.get("name", "Unknown")
        year     = (detail.get("release_date") or detail.get("first_air_date", "N/A"))[:4]
        rating   = detail.get("vote_average", 0)
        votes    = detail.get("vote_count", 0)
        runtime  = detail.get("runtime") or (detail.get("episode_run_time") or [0])[0]
        genres   = ", ".join(g["name"] for g in detail.get("genres", [])[:4]) or "N/A"
        overview = (detail.get("overview") or "No overview available.")[:700]
        lang     = (detail.get("original_language") or "").upper()
        status   = detail.get("status", "N/A")
        cast     = detail.get("credits", {}).get("cast", [])[:5]
        cast_str = ", ".join(c["name"] for c in cast) or "N/A"
        imdb_id  = detail.get("external_ids", {}).get("imdb_id", "")
        poster   = detail.get("poster_path", "")
        stars    = "⭐" * round(rating / 2)

        text = (
            f"🎬 **{title}** ({year})\n"
            f"{stars}\n\n"
            f"⭐ **Rating**   : `{rating:.1f}/10` ({votes:,} votes)\n"
            f"🎭 **Genre**    : `{genres}`\n"
            f"⏱ **Runtime**  : `{runtime} min`\n"
            f"🌐 **Language** : `{lang}`\n"
            f"📌 **Status**   : `{status}`\n"
            f"👥 **Cast**     : `{cast_str}`\n\n"
            f"📖 **Overview:**\n{overview}"
        )

        btns1 = []
        if imdb_id:
            btns1.append(InlineKeyboardButton("🎬 IMDB", url=f"https://www.imdb.com/title/{imdb_id}"))
        btns1.append(InlineKeyboardButton("🎞 TMDB", url=f"https://www.themoviedb.org/{mtype}/{detail['id']}"))
        btns2 = [InlineKeyboardButton("📺 Check OTT", callback_data=f"imdb_ott|{mtype}|{detail['id']}|{title[:40]}")]
        keyboard = InlineKeyboardMarkup([btns1, btns2])

        await wait.delete()
        if poster:
            await message.reply_photo(f"{TMDB_IMG}{poster}", caption=text, parse_mode=MD, reply_markup=keyboard)
        else:
            await message.reply(text, parse_mode=MD, reply_markup=keyboard)

        await log_action(client, message, "🎬 IMDB Lookup", f"`{title}` ({year})")



    # ── /ott ──────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("ott") & filters.private)
    async def cmd_ott(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        args = message.command[1:]
        if not args:
            await message.reply(
                "Give A Valid Movie/Series Name Along With Command!\n\n"
                "**Usage:** `/ott <movie or series name>`\n"
                "**Example:** `/ott Meesaya Murukku`",
                parse_mode=MD,
            )
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Searching **{query}**…", parse_mode=MD)

        # Use free JustWatch public API (no token needed)
        items = await jw.search_all(query, page_size=8)

        if not items:
            await wait.edit(f"❌ No results found for **{query}** on JustWatch.", parse_mode=MD)
            return

        if len(items) == 1:
            item  = items[0]
            title = item.get("title", query)
            await wait.delete()
            await _send_ott_detail(client, message, item, title, reply=True)
            await log_action(client, message, "📺 OTT Lookup", f"`{title}`")
            return

        # Multiple results — show picker
        sk = _sk(query)
        _ott_sessions[sk] = items

        buttons = []
        for i, item in enumerate(items[:8]):
            title = item.get("title", "Unknown")
            year  = item.get("original_release_year", "")
            label = f"{title} ({year})" if year else title
            buttons.append([InlineKeyboardButton(label, callback_data=f"ott_pick|{sk}|{i}")])
        buttons.append([InlineKeyboardButton("❌ Close", callback_data="ott_close")])

        await wait.edit(
            f"**Search Results For** {query}",
            parse_mode=MD,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^ott_pick\|"))
    async def ott_pick_cb(client: Client, query: CallbackQuery):
        await query.answer()
        _, sk, idx_str = query.data.split("|", 2)
        items = _ott_sessions.get(sk)
        if not items:
            await query.answer("Session expired. Search again.", show_alert=True)
            return
        item  = items[int(idx_str)]
        title = item.get("title", "Unknown")
        await query.message.delete()
        await _send_ott_detail(client, query.message, item, title, reply=True)

    @app.on_callback_query(filters.regex(r"^ott_close$"))
    async def ott_close_cb(client: Client, query: CallbackQuery):
        await query.answer()
        await query.message.delete()

    # Also handle "Check OTT" button from /imdb
    @app.on_callback_query(filters.regex(r"^imdb_ott\|"))
    async def imdb_ott_cb(client: Client, query: CallbackQuery):
        await query.answer("🔍 Searching JustWatch…")
        title = query.data.split("|", 1)[1]
        items = await jw.search_all(title, page_size=3)
        item  = items[0] if items else None
        await _send_ott_detail(client, query.message, item, title, reply=True)

    async def _send_ott_detail(
        client, target: Message,
        item: dict | None, query_title: str, reply: bool,
    ):
        """Format and send OTT availability from a JustWatch item dict."""
        if not item:
            text = (
                f"≡ **{query_title}**\n\n"
                f"🔥 **Availability :-**\n"
                f"_Not available for streaming in India yet._\n\n"
                f"CC ~ @{BOT_USERNAME}"
            )
            jw_url = f"https://www.justwatch.com/in/search?q={query_title.replace(' ', '+')}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔍 JustWatch", url=jw_url)]])
            await (target.reply(text, parse_mode=MD, reply_markup=kb) if reply
                   else target.edit(text, parse_mode=MD, reply_markup=kb))
            return

        title     = item.get("title", query_title)
        year      = item.get("original_release_year", "")
        offers    = item.get("offers") or []
        jw_id     = item.get("id")
        obj_type  = item.get("object_type", "movie")

        # If no offers in search result, fetch full detail
        if not offers and jw_id:
            detail = await jw.get_title(jw_id, obj_type)
            if detail:
                offers = detail.get("offers") or []

        avail_str = await jw.format_offers(offers)

        year_str   = f" ({year})" if year else ""
        title_line = f"≡ **{title}**{year_str}"
        text       = (
            f"{title_line}\n\n"
            f"🔥 **Availability :-**\n"
            f"{avail_str}\n\n"
            f"CC ~ @{BOT_USERNAME}"
        )

        jw_url = f"https://www.justwatch.com/in/search?q={title.replace(' ', '+')}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔍 JustWatch", url=jw_url)]])

        if reply:
            await target.reply(text, parse_mode=MD, reply_markup=kb)
        else:
            await target.edit(text, parse_mode=MD, reply_markup=kb)

    # ── /posters ──────────────────────────────────────────────────────────────

    def _classify_posters(images: dict) -> dict[str, list]:
        portrait, landscape, clean = [], [], []
        for p in images.get("posters", []):
            if p.get("aspect_ratio", 1) < 1:
                portrait.append(p)
        for b in images.get("backdrops", []):
            if b.get("iso_639_1") or "":
                landscape.append(b)
            else:
                clean.append(b)
        return {"portrait": portrait, "landscape": landscape, "clean_landscape": clean}

    @app.on_message(filters.command("posters") & filters.private)
    async def cmd_posters(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        allowed, used, limit = await db.check_and_consume(u.id, "poster")
        if not allowed:
            await message.reply(
                f"⚠️ You've used **{used}/{limit}** poster searches this month.\n"
                "Upgrade to /premium for unlimited access!",
                parse_mode=MD,
            )
            return

        args = message.command[1:]
        if not args:
            await message.reply(
                "🖼 **Movie Posters**\n\n"
                "**Usage:** `/posters <movie name>`\n"
                "**Example:** `/posters Kadaisi Ulaga Por`",
                parse_mode=MD,
            )
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Searching for **{query}**…", parse_mode=MD)

        async with httpx.AsyncClient(timeout=15) as hx:
            hit = await _tmdb_search(query, hx)
            if not hit:
                await wait.edit("❌ No results found.")
                return
            detail = await _tmdb_detail(hit["media_type"], hit["id"], hx)

        images  = detail.get("images", {})
        buckets = _classify_posters(images)

        if not any(buckets.values()):
            await wait.edit("❌ No posters found for this title.")
            return

        title    = detail.get("title") or detail.get("name", "")
        year     = (detail.get("release_date") or detail.get("first_air_date", ""))[:4]
        mtype    = hit["media_type"]
        tmdb_id  = detail["id"]
        tmdb_url = f"https://www.themoviedb.org/{mtype}/{tmdb_id}"

        _poster_sessions[message.chat.id] = {
            "buckets": buckets, "type": None, "index": 0,
            "title": f"{title} ({year})", "mtype": mtype,
            "tmdb_id": tmdb_id, "tmdb_url": tmdb_url,
        }

        await wait.delete()
        await _send_type_picker(message, message.chat.id, reply=True)
        await log_action(client, message, "🖼 Posters", f"`{title}` ({year})")

    async def _send_type_picker(target, cid: int, reply: bool = False, edit_msg=None):
        s = _poster_sessions.get(cid)
        if not s:
            return
        labels_map = {"portrait": "Portrait", "landscape": "Landscape", "clean_landscape": "Clean Landscape"}
        rows, pair = [], []
        for key in ("landscape", "portrait", "clean_landscape"):
            count = len(s["buckets"].get(key, []))
            if count == 0:
                continue
            btn = InlineKeyboardButton(f"{labels_map[key]} ({count})", callback_data=f"pt_type|{key}")
            pair.append(btn)
            if len(pair) == 2:
                rows.append(pair); pair = []
        if pair:
            rows.append(pair)
        rows.append([
            InlineKeyboardButton("🔙 Back", callback_data="pt_close"),
            InlineKeyboardButton("❌ Close", callback_data="pt_close"),
        ])
        text = f"**{s['title']}**\n\nTMDB : {s['tmdb_url']}\n\n**Select Poster Type :-**"
        kb   = InlineKeyboardMarkup(rows)
        if edit_msg:
            try: await edit_msg.edit(text, parse_mode=MD, reply_markup=kb)
            except: await target.reply(text, parse_mode=MD, reply_markup=kb)
        elif reply:
            await target.reply(text, parse_mode=MD, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^pt_type\|"))
    async def poster_type_cb(client: Client, query: CallbackQuery):
        await query.answer()
        cid = query.message.chat.id
        s   = _poster_sessions.get(cid)
        if not s:
            await query.answer("Session expired. Use /posters again.", show_alert=True)
            return
        s["type"]  = query.data.split("|", 1)[1]
        s["index"] = 0
        await _send_poster_view(client, query.message, cid)

    @app.on_callback_query(filters.regex(r"^pt_(prev|next|first|last|back|close)$"))
    async def poster_nav_cb(client: Client, query: CallbackQuery):
        action = query.data.split("_", 1)[1]
        cid    = query.message.chat.id
        s      = _poster_sessions.get(cid)
        await query.answer()

        if action == "close":
            await query.message.delete(); _poster_sessions.pop(cid, None); return
        if action == "back":
            if s: await _send_type_picker(query.message, cid, edit_msg=query.message)
            return
        if not s or not s.get("type"):
            return

        items = s["buckets"].get(s["type"], [])
        total = len(items)
        idx   = s["index"]
        if   action == "next":  s["index"] = (idx + 1) % total
        elif action == "prev":  s["index"] = (idx - 1) % total
        elif action == "first": s["index"] = 0
        elif action == "last":  s["index"] = total - 1
        await _send_poster_view(client, query.message, cid)

    @app.on_callback_query(filters.regex(r"^pt_noop$"))
    async def poster_noop_cb(client: Client, query: CallbackQuery):
        await query.answer()

    async def _send_poster_view(client: Client, msg, cid: int):
        s = _poster_sessions.get(cid)
        if not s or not s.get("type"):
            return
        items = s["buckets"].get(s["type"], [])
        idx   = s["index"]
        total = len(items)
        p     = items[idx]
        ptype = {"portrait": "Portrait", "landscape": "Landscape", "clean_landscape": "Clean Landscape"}.get(s["type"], "?")
        lang  = (p.get("iso_639_1") or "N/A").upper()
        w, h  = p.get("width", 0), p.get("height", 0)
        url   = f"https://image.tmdb.org/t/p/original{p['file_path']}"

        caption = (
            f"**{s['title']}**\n\n"
            f"• TMDB : {s['tmdb_url']}\n"
            f"• Type : {ptype}\n"
            f"• Language: {lang}\n"
            f"• Width: {w}, Height: {h}\n"
            f"• [Click Here]({url})"
        )
        nav = [
            InlineKeyboardButton("<<", callback_data="pt_first"),
            InlineKeyboardButton("<",  callback_data="pt_prev"),
            InlineKeyboardButton(f"{idx+1}/{total}", callback_data="pt_noop"),
            InlineKeyboardButton(">",  callback_data="pt_next"),
            InlineKeyboardButton(">>", callback_data="pt_last"),
        ]
        ctrl = [
            InlineKeyboardButton("🔙 Back",  callback_data="pt_back"),
            InlineKeyboardButton("❌ Close", callback_data="pt_close"),
        ]
        keyboard = InlineKeyboardMarkup([nav, ctrl])
        try: await msg.delete()
        except: pass
        await client.send_photo(cid, url, caption=caption, parse_mode=MD, reply_markup=keyboard)
