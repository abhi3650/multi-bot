"""
handlers/movie.py  —  /imdb  /ott  /posters

/posters UI (matches the screenshots exactly):
  Step 1 — Show movie title + "Select Poster Type" buttons:
              [Landscape (N)]  [Portrait (N)]
              [Clean Landscape (N)]
              [Back]  [Close]
  Step 2 — Browse within that type:
              [<<]  [<]  [1/N]  [>]  [>>]
              [Back]  [Close]

OTT source priority:
  1. JustWatch GraphQL  (if JUSTWATCH_API env is set)
  2. TMDB Watch Providers  (always available)
"""

import logging

import httpx
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from config import TMDB_API_KEY, TMDB_BASE, TMDB_IMG, JUSTWATCH_API

MD  = ParseMode.MARKDOWN
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


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
        params={"api_key": TMDB_API_KEY,
                "append_to_response": "credits,external_ids,images"},
    )
    return r.json()


# ── OTT helpers ───────────────────────────────────────────────────────────────

_OTT_LABELS = {
    "FLATRATE": ("✅", "Subscription"), "flatrate": ("✅", "Subscription"),
    "FREE":     ("🆓", "Free"),         "free":     ("🆓", "Free"),
    "ADS":      ("📢", "With Ads"),     "ads":      ("📢", "With Ads"),
    "BUY":      ("🛒", "Buy"),          "buy":      ("🛒", "Buy"),
    "RENT":     ("💰", "Rent"),         "rent":     ("💰", "Rent"),
}

_JW_GQL = "https://apis.justwatch.com/graphql"
_JW_Q   = """
query GetStreamingOffers($searchQuery: String!, $country: Country!, $language: Language!) {
  searchTitles(
    searchTitlesFilter: { searchQuery: $searchQuery }
    country: $country
    language: $language
    first: 5
  ) {
    edges {
      node {
        content(country: $country, language: $language) {
          title
          originalReleaseYear
        }
        offers(country: $country, platform: WEB) {
          monetizationType
          provider { clearName }
        }
      }
    }
  }
}
"""


async def _ott_justwatch(hx: httpx.AsyncClient, title: str, year: str) -> dict | None:
    if not JUSTWATCH_API:
        return None
    try:
        resp = await hx.post(
            _JW_GQL,
            json={"query": _JW_Q, "variables": {
                "searchQuery": title, "country": "IN", "language": "en",
            }},
            headers={
                "Content-Type":    "application/json",
                "Accept-Language": "en",
                "Authorization":   f"Bearer {JUSTWATCH_API}",
                "x-api-key":       JUSTWATCH_API,
                "Origin":          "https://www.justwatch.com",
            },
            timeout=12,
        )
        if resp.status_code != 200:
            log.warning("[ott/jw] HTTP %s — falling back to TMDB", resp.status_code)
            return None
        edges = resp.json().get("data", {}).get("searchTitles", {}).get("edges", [])
        if not edges:
            return None
        chosen = next(
            (e["node"] for e in edges
             if title.lower() in (e["node"].get("content") or {}).get("title", "").lower()),
            edges[0]["node"],
        )
        providers: dict[str, list] = {}
        for offer in chosen.get("offers") or []:
            mtype = offer.get("monetizationType", "UNKNOWN")
            name  = (offer.get("provider") or {}).get("clearName")
            if name:
                providers.setdefault(mtype, [])
                if name not in providers[mtype]:
                    providers[mtype].append(name)
        content = chosen.get("content") or {}
        return {
            "title":     content.get("title", title),
            "year":      str(content.get("originalReleaseYear", year or "")),
            "providers": providers,
            "source":    "JustWatch",
        }
    except Exception as exc:
        log.warning("[ott/jw] %s — falling back to TMDB", exc)
        return None


async def _ott_tmdb(hx: httpx.AsyncClient, mtype: str, tmdb_id: int,
                    title: str, year: str) -> dict:
    r = await hx.get(
        f"{TMDB_BASE}/{mtype}/{tmdb_id}/watch/providers",
        params={"api_key": TMDB_API_KEY},
    )
    prov_data   = r.json().get("results", {})
    region_data = prov_data.get("IN") or prov_data.get("US") or {}
    jw_link     = region_data.get("link", "")
    providers: dict[str, list] = {}
    for key in ("flatrate", "free", "ads", "rent", "buy"):
        for p in region_data.get(key, []):
            name = p.get("provider_name", "")
            if name:
                providers.setdefault(key, [])
                if name not in providers[key]:
                    providers[key].append(name)
    return {"title": title, "year": year, "providers": providers,
            "source": "TMDB", "jw_link": jw_link}


# ── Poster session helpers ────────────────────────────────────────────────────

def _classify_posters(images: dict) -> dict[str, list]:
    """
    Split TMDB images into three buckets matching the UI:
      portrait        → aspect_ratio < 1   (tall movie posters)
      landscape       → aspect_ratio >= 1 AND iso_639_1 set  (stylised backdrops)
      clean_landscape → aspect_ratio >= 1 AND iso_639_1 is None/empty (logo-free)
    """
    portrait        = []
    landscape       = []
    clean_landscape = []

    for p in images.get("posters", []):
        if p.get("aspect_ratio", 1) < 1:
            portrait.append(p)

    for b in images.get("backdrops", []):
        lang = b.get("iso_639_1") or ""
        if lang:
            landscape.append(b)
        else:
            clean_landscape.append(b)

    return {
        "portrait":        portrait,
        "landscape":       landscape,
        "clean_landscape": clean_landscape,
    }


def _type_label(key: str, count: int) -> str:
    labels = {
        "portrait":        "Portrait",
        "landscape":       "Landscape",
        "clean_landscape": "Clean Landscape",
    }
    return f"{labels[key]} ({count})"


# Per-chat poster sessions
_poster_sessions: dict[int, dict] = {}


# ── Handler registration ──────────────────────────────────────────────────────

def register(app: Client):

    # ── /imdb ─────────────────────────────────────────────────────────────────
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

        btns_row1 = []
        if imdb_id:
            btns_row1.append(InlineKeyboardButton("🎬 IMDB",  url=f"https://www.imdb.com/title/{imdb_id}"))
        btns_row1.append(InlineKeyboardButton("🎞 TMDB", url=f"https://www.themoviedb.org/{mtype}/{detail['id']}"))
        btns_row2 = [InlineKeyboardButton(
            "📺 Check OTT",
            callback_data=f"check_ott|{mtype}|{detail['id']}|{title[:30]}|{year}",
        )]
        keyboard = InlineKeyboardMarkup([btns_row1, btns_row2])

        await wait.delete()
        if poster:
            await message.reply_photo(
                f"{TMDB_IMG}{poster}", caption=text, parse_mode=MD, reply_markup=keyboard,
            )
        else:
            await message.reply(text, parse_mode=MD, reply_markup=keyboard)

    # ── /ott ──────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("ott") & filters.private)
    async def cmd_ott(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, _full_name(u))

        args = message.command[1:]
        if not args:
            await message.reply(
                "📺 **OTT Availability**\n\n"
                "**Usage:** `/ott <movie or series name>`\n"
                "**Example:** `/ott Pushpa 2`",
                parse_mode=MD,
            )
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Checking OTT for **{query}**…", parse_mode=MD)

        async with httpx.AsyncClient(timeout=20) as hx:
            hit = await _tmdb_search(query, hx)
            if not hit:
                await wait.edit(f"❌ Could not find **{query}** on TMDB.", parse_mode=MD)
                return
            mtype       = hit["media_type"]
            tmdb_id     = hit["id"]
            clean_title = hit.get("title") or hit.get("name") or query
            year        = (hit.get("release_date") or hit.get("first_air_date") or "")[:4]
            poster      = hit.get("poster_path", "")

            result = await _ott_justwatch(hx, clean_title, year) if JUSTWATCH_API else None
            if result is None:
                result = await _ott_tmdb(hx, mtype, tmdb_id, clean_title, year)

        await wait.delete()
        await _send_ott_result(message, result, query, poster, reply=True)

    @app.on_callback_query(filters.regex(r"^check_ott\|"))
    async def ott_inline_cb(client: Client, query: CallbackQuery):
        await query.answer("🔍 Fetching OTT data…")
        parts   = query.data.split("|", 4)
        mtype   = parts[1]
        tmdb_id = int(parts[2])
        title   = parts[3]
        year    = parts[4]

        async with httpx.AsyncClient(timeout=20) as hx:
            result = await _ott_justwatch(hx, title, year) if JUSTWATCH_API else None
            if result is None:
                result = await _ott_tmdb(hx, mtype, tmdb_id, title, year)

        await _send_ott_result(query.message, result, title, poster=None, reply=True)

    async def _send_ott_result(target, result: dict, query: str,
                                poster: str | None, reply: bool):
        title_str = result["title"]
        year_str  = result["year"]
        providers = result["providers"]
        source    = result["source"]

        lines = [f"📺 **{title_str}** ({year_str})\n\n🔥 **Streaming Availability:**\n"]
        found = False
        for key, (emoji, label) in _OTT_LABELS.items():
            names = providers.get(key, [])
            if names:
                lines.append(f"{emoji} **{label}:** {', '.join(names)}")
                found = True
        if not found:
            lines.append("_Not available on any streaming platform in your region yet._")
        lines.append(f"\n_Source: {source}_")

        jw_link  = result.get("jw_link") or \
                   f"https://www.justwatch.com/in/search?q={query.replace(' ', '+')}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 View on JustWatch", url=jw_link)
        ]])
        text = "\n".join(lines)

        if poster and reply:
            await target.reply_photo(f"{TMDB_IMG}{poster}", caption=text,
                                     parse_mode=MD, reply_markup=keyboard)
        elif reply:
            await target.reply(text, parse_mode=MD, reply_markup=keyboard)
        else:
            await target.edit(text, parse_mode=MD, reply_markup=keyboard)

    # ── /posters — Step 1: search & type picker ───────────────────────────────
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

        # Check we have at least one image
        total_imgs = sum(len(v) for v in buckets.values())
        if total_imgs == 0:
            await wait.edit("❌ No posters found for this title.")
            return

        title   = detail.get("title") or detail.get("name", "")
        year    = (detail.get("release_date") or detail.get("first_air_date", ""))[:4]
        mtype   = hit["media_type"]
        tmdb_id = detail["id"]
        tmdb_url = f"https://www.themoviedb.org/{mtype}/{tmdb_id}"

        # Store in session
        _poster_sessions[message.chat.id] = {
            "buckets":  buckets,
            "type":     None,      # selected bucket key
            "index":    0,
            "title":    f"{title} ({year})",
            "mtype":    mtype,
            "tmdb_id":  tmdb_id,
            "tmdb_url": tmdb_url,
        }

        await wait.delete()

        # Build type picker keyboard — only show non-empty types
        rows = []
        pair = []
        for key in ("landscape", "portrait", "clean_landscape"):
            count = len(buckets[key])
            if count == 0:
                continue
            label = _type_label(key, count)
            btn   = InlineKeyboardButton(label, callback_data=f"pt_type|{key}")
            pair.append(btn)
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([
            InlineKeyboardButton("🔙 Back",  callback_data="pt_close"),
            InlineKeyboardButton("❌ Close", callback_data="pt_close"),
        ])

        await message.reply(
            f"**{title} ({year})**\n\n"
            f"TMDB : {tmdb_url}\n\n"
            "**Select Poster Type :-**",
            parse_mode=MD,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # ── /posters — Step 2: type selected → show first poster ─────────────────
    @app.on_callback_query(filters.regex(r"^pt_type\|"))
    async def poster_type_cb(client: Client, query: CallbackQuery):
        await query.answer()
        cid     = query.message.chat.id
        s       = _poster_sessions.get(cid)
        if not s:
            await query.answer("Session expired. Use /posters again.", show_alert=True)
            return

        chosen_type         = query.data.split("|", 1)[1]
        s["type"]           = chosen_type
        s["index"]          = 0
        await _send_poster_view(client, query.message, cid, edit=True)

    # ── /posters — Navigation callbacks ──────────────────────────────────────
    @app.on_callback_query(filters.regex(r"^pt_(prev|next|first|last|back|close)$"))
    async def poster_nav_cb(client: Client, query: CallbackQuery):
        action = query.data.split("_", 1)[1]
        cid    = query.message.chat.id
        s      = _poster_sessions.get(cid)

        await query.answer()

        if action == "close":
            await query.message.delete()
            _poster_sessions.pop(cid, None)
            return

        if action == "back":
            # Go back to type picker
            if not s:
                return
            buckets  = s["buckets"]
            rows     = []
            pair     = []
            for key in ("landscape", "portrait", "clean_landscape"):
                count = len(buckets[key])
                if count == 0:
                    continue
                label = _type_label(key, count)
                btn   = InlineKeyboardButton(label, callback_data=f"pt_type|{key}")
                pair.append(btn)
                if len(pair) == 2:
                    rows.append(pair)
                    pair = []
            if pair:
                rows.append(pair)
            rows.append([
                InlineKeyboardButton("🔙 Back",  callback_data="pt_close"),
                InlineKeyboardButton("❌ Close", callback_data="pt_close"),
            ])
            try:
                await query.message.edit(
                    f"**{s['title']}**\n\n"
                    f"TMDB : {s['tmdb_url']}\n\n"
                    "**Select Poster Type :-**",
                    parse_mode=MD,
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            except Exception:
                pass
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

        await _send_poster_view(client, query.message, cid, edit=True)

    async def _send_poster_view(client: Client, msg, cid: int, edit: bool = False):
        s     = _poster_sessions.get(cid)
        if not s or not s.get("type"):
            return

        items = s["buckets"].get(s["type"], [])
        idx   = s["index"]
        total = len(items)
        p     = items[idx]

        # Poster metadata
        ar    = p.get("aspect_ratio", 1)
        ptype = {
            "portrait":        "Portrait",
            "landscape":       "Landscape",
            "clean_landscape": "Clean Landscape",
        }.get(s["type"], "Unknown")
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

        # Navigation: <<  <  idx/total  >  >>
        nav = [
            InlineKeyboardButton("<<",  callback_data="pt_first"),
            InlineKeyboardButton("<",   callback_data="pt_prev"),
            InlineKeyboardButton(f"{idx+1}/{total}", callback_data="pt_noop"),
            InlineKeyboardButton(">",   callback_data="pt_next"),
            InlineKeyboardButton(">>",  callback_data="pt_last"),
        ]
        ctrl = [
            InlineKeyboardButton("🔙 Back",  callback_data="pt_back"),
            InlineKeyboardButton("❌ Close", callback_data="pt_close"),
        ]
        keyboard = InlineKeyboardMarkup([nav, ctrl])

        if edit:
            # Delete old message and send new photo (edit_media on photo is cleaner)
            try:
                await msg.delete()
            except Exception:
                pass
            await client.send_photo(
                cid, url, caption=caption, parse_mode=MD, reply_markup=keyboard,
            )
        else:
            await client.send_photo(
                cid, url, caption=caption, parse_mode=MD, reply_markup=keyboard,
            )

    # Handle the noop button (counter display)
    @app.on_callback_query(filters.regex(r"^pt_noop$"))
    async def poster_noop_cb(client: Client, query: CallbackQuery):
        await query.answer()
