"""
handlers/movie.py  —  /imdb  /ott  /posters

/ott uses ONLY the JustWatch Content Partner API v2:
  Base: https://apis.justwatch.com/contentpartner/v2/content
  Search: GET /titles/object_type/all/locale/en_IN?query=<title>&token=<TOKEN>
  Detail: GET /offers/object_type/{type}/id_type/tmdb/locale/en_IN?id=<tmdb_id>&token=<TOKEN>

  No GraphQL. No TMDB fallback for OTT. No env var check inside handlers.
  If JUSTWATCH_TOKEN is empty the command still works — it uses TMDB for
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
from config import TMDB_API_KEY, TMDB_BASE, TMDB_IMG, JUSTWATCH_TOKEN, BOT_USERNAME
from logger import log_action

MD  = ParseMode.MARKDOWN
log = logging.getLogger(__name__)

# JustWatch Content Partner API v2
JW_BASE   = "https://apis.justwatch.com/contentpartner/v2/content"
JW_LOCALE = "en_IN"   # India locale

_OTT_LABELS = {
    "flatrate": ("✅", "Subscription"),
    "free":     ("🆓", "Free"),
    "ads":      ("📢", "With Ads"),
    "buy":      ("🛒", "Buy"),
    "rent":     ("💰", "Rent"),
}

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

async def _jw_search_titles(hx: httpx.AsyncClient, query: str) -> list[dict]:
    """
    Search JustWatch for titles matching query.
    Returns list of title items (each has title, year, object_type, justwatch_id, offers, full_path).
    """
    if not JUSTWATCH_TOKEN:
        return []
    try:
        r = await hx.get(
            f"{JW_BASE}/titles/object_type/all/locale/{JW_LOCALE}",
            params={"query": query, "token": JUSTWATCH_TOKEN},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("[jw/search] HTTP %s for %r", r.status_code, query)
            return []
        return r.json().get("items", [])
    except Exception as exc:
        log.warning("[jw/search] %s", exc)
        return []


async def _jw_offers_by_tmdb(
    hx: httpx.AsyncClient,
    object_type: str,   # "movie" or "show"
    tmdb_id: int,
) -> dict | None:
    """
    Fetch offers for a specific title using TMDB ID.
    Returns the full response dict (title, offers, full_path, etc.) or None.
    """
    if not JUSTWATCH_TOKEN:
        return None
    try:
        r = await hx.get(
            f"{JW_BASE}/offers/object_type/{object_type}/id_type/tmdb/locale/{JW_LOCALE}",
            params={"id": tmdb_id, "token": JUSTWATCH_TOKEN},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("[jw/offers] HTTP %s for tmdb=%s", r.status_code, tmdb_id)
            return None
        return r.json()
    except Exception as exc:
        log.warning("[jw/offers] %s", exc)
        return None


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

    @app.on_callback_query(filters.regex(r"^imdb_ott\|"))
    async def imdb_ott_cb(client: Client, query: CallbackQuery):
        await query.answer("🔍 Fetching OTT data…")
        _, mtype, tmdb_id_str, title = query.data.split("|", 3)
        await _fetch_and_send_ott(client, query.message, mtype, int(tmdb_id_str), title, reply=True)

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

        async with httpx.AsyncClient(timeout=20) as hx:
            # Step 1: use JustWatch search to get candidates
            items = await _jw_search_titles(hx, query)

        if not items:
            # JustWatch returned nothing (or token not configured)
            # Fall back to TMDB search just for the title/year picker
            async with httpx.AsyncClient(timeout=15) as hx:
                hit = await _tmdb_search(query, hx)
            if not hit:
                await wait.edit(f"❌ No results found for **{query}**.", parse_mode=MD)
                return
            # Go directly to detail using TMDB ID
            mtype   = hit["media_type"]
            tmdb_id = hit["id"]
            title   = hit.get("title") or hit.get("name") or query
            await wait.delete()
            await _fetch_and_send_ott(client, message, mtype, tmdb_id, title, reply=True)
            await log_action(client, message, "📺 OTT Lookup", f"`{title}`")
            return

        if len(items) == 1:
            # Only one result — resolve TMDB ID and go straight to detail
            item    = items[0]
            tmdb_id = item.get("tmdb_id") or item.get("imdb_id") or 0
            otype   = item.get("object_type", "movie")
            title   = item.get("title", query)
            await wait.delete()
            await _fetch_and_send_ott(client, message, otype, tmdb_id, title, reply=True, jw_item=item)
            await log_action(client, message, "📺 OTT Lookup", f"`{title}`")
            return

        # Multiple results → show picker
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
        item    = items[int(idx_str)]
        tmdb_id = item.get("tmdb_id") or 0
        otype   = item.get("object_type", "movie")
        title   = item.get("title", "Unknown")
        await query.message.delete()
        await _fetch_and_send_ott(client, query.message, otype, tmdb_id, title, reply=True, jw_item=item)

    @app.on_callback_query(filters.regex(r"^ott_close$"))
    async def ott_close_cb(client: Client, query: CallbackQuery):
        await query.answer()
        await query.message.delete()

    async def _fetch_and_send_ott(
        client, target: Message,
        mtype: str, tmdb_id: int, title: str,
        reply: bool,
        jw_item: dict | None = None,
    ):
        """
        Fetch offers from JustWatch Content Partner API and format the result.
        jw_item: pre-fetched JustWatch search result (has offers embedded).
        """
        offers   = []
        full_path = ""

        if jw_item and jw_item.get("offers") is not None:
            # Offers already embedded in the search result
            offers    = jw_item.get("offers") or []
            full_path = jw_item.get("full_path", "")
            year      = jw_item.get("original_release_year", "")
        else:
            # Fetch separately using TMDB ID
            if JUSTWATCH_TOKEN and tmdb_id:
                async with httpx.AsyncClient(timeout=15) as hx:
                    data = await _jw_offers_by_tmdb(hx, mtype, tmdb_id)
                if data:
                    offers    = data.get("offers") or []
                    full_path = data.get("full_path", "")
                    year      = data.get("original_release_year", "")
                else:
                    year = ""
            else:
                year = ""

        providers = _build_providers(offers)

        # Build JustWatch page URL
        if full_path:
            jw_url = f"https://www.justwatch.com{full_path}"
        else:
            jw_url = f"https://www.justwatch.com/in/search?q={title.replace(' ', '+')}"

        # Format the message
        year_str   = f" ({year})" if year else ""
        title_line = f"≡ **{title}**{year_str}"
        avail_line = "🔥 **Availability :-**\n"

        if not JUSTWATCH_TOKEN:
            avail_line += "_⚠️ JustWatch token not configured — add `JUSTWATCH_TOKEN` to `.env`_"
        elif providers:
            # Show subscription first, then free, then ads, then buy/rent
            for key in ("flatrate", "free", "ads", "buy", "rent"):
                names = providers.get(key, [])
                if names:
                    emoji, label = _OTT_LABELS[key]
                    avail_line += f"{emoji} **{label}:** {', '.join(names)}\n"
        else:
            avail_line += "_Not available for streaming in India yet._"

        credit = f"\nCC ~ @{BOT_USERNAME}"
        text   = f"{title_line}\n\n{avail_line}{credit}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 JustWatch", url=jw_url)
        ]])

        if reply:
            await target.reply(text, parse_mode=MD, reply_markup=keyboard)
        else:
            await target.edit(text, parse_mode=MD, reply_markup=keyboard)

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
