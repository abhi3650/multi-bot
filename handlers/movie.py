"""
handlers/movie.py
Commands: /imdb  /ott  /posters

OTT source priority:
  1. JustWatch GraphQL (if JUSTWATCH_API env is set)
  2. TMDB Watch Providers (free, always available)
"""

import logging

import httpx
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

import database as db
from config import TMDB_API_KEY, TMDB_BASE, TMDB_IMG, JUSTWATCH_API

log = logging.getLogger(__name__)

# ── TMDB helpers ──────────────────────────────────────────────────────────────

async def _tmdb_search(query: str, client: httpx.AsyncClient) -> dict | None:
    r = await client.get(
        f"{TMDB_BASE}/search/multi",
        params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "false"},
    )
    results = r.json().get("results", [])
    return next((x for x in results if x.get("media_type") in ("movie", "tv")), None)


async def _tmdb_detail(mtype: str, tmdb_id: int, client: httpx.AsyncClient) -> dict:
    r = await client.get(
        f"{TMDB_BASE}/{mtype}/{tmdb_id}",
        params={"api_key": TMDB_API_KEY, "append_to_response": "credits,external_ids,images"},
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


async def _ott_justwatch(client: httpx.AsyncClient, title: str, year: str) -> dict | None:
    if not JUSTWATCH_API:
        return None
    try:
        resp = await client.post(
            _JW_GQL,
            json={"query": _JW_Q, "variables": {"searchQuery": title, "country": "IN", "language": "en"}},
            headers={
                "Content-Type": "application/json",
                "Accept-Language": "en",
                "Authorization": f"Bearer {JUSTWATCH_API}",
                "x-api-key": JUSTWATCH_API,
                "Origin": "https://www.justwatch.com",
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
            pname = (offer.get("provider") or {}).get("clearName")
            if pname:
                providers.setdefault(mtype, [])
                if pname not in providers[mtype]:
                    providers[mtype].append(pname)
        content = chosen.get("content") or {}
        return {
            "title": content.get("title", title),
            "year": str(content.get("originalReleaseYear", year or "")),
            "providers": providers,
            "source": "JustWatch",
        }
    except Exception as exc:
        log.warning("[ott/jw] %s — falling back to TMDB", exc)
        return None


async def _ott_tmdb(client: httpx.AsyncClient, mtype: str, tmdb_id: int,
                    title: str, year: str) -> dict:
    r = await client.get(
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
    return {"title": title, "year": year, "providers": providers, "source": "TMDB", "jw_link": jw_link}


# ── Handler registration ──────────────────────────────────────────────────────

def register(app: Client):

    # ── /imdb ─────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("imdb"))
    async def cmd_imdb(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)

        args = message.command[1:]
        if not args:
            await message.reply(
                "🎬 *IMDB Info Lookup*\n\nUsage: `/imdb <movie or series name>`\nExample: `/imdb Interstellar`"
            )
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Searching *{query}*…")

        async with httpx.AsyncClient(timeout=15) as hx:
            hit = await _tmdb_search(query, hx)
            if not hit:
                await wait.edit("❌ No results found. Try a different name.")
                return
            mtype  = hit["media_type"]
            detail = await _tmdb_detail(mtype, hit["id"], hx)

        title    = detail.get("title") or detail.get("name", "Unknown")
        year     = (detail.get("release_date") or detail.get("first_air_date", "N/A"))[:4]
        rating   = detail.get("vote_average", 0)
        votes    = detail.get("vote_count", 0)
        runtime  = detail.get("runtime") or (detail.get("episode_run_time") or [0])[0]
        genres   = ", ".join(g["name"] for g in detail.get("genres", [])[:4]) or "N/A"
        overview = (detail.get("overview") or "No overview.")[:600]
        lang     = (detail.get("original_language") or "").upper()
        status   = detail.get("status", "N/A")
        cast     = detail.get("credits", {}).get("cast", [])[:5]
        cast_str = ", ".join(c["name"] for c in cast) or "N/A"
        imdb_id  = detail.get("external_ids", {}).get("imdb_id", "")
        poster   = detail.get("poster_path", "")

        text = (
            f"🎬 *{title}* ({year})\n"
            f"⭐ Rating : `{rating:.1f}/10` ({votes:,} votes)\n"
            f"🎭 Genre  : `{genres}`\n"
            f"⏱ Runtime : `{runtime} min`\n"
            f"🌐 Language: `{lang}`\n"
            f"📌 Status  : `{status}`\n"
            f"👥 Cast    : `{cast_str}`\n\n"
            f"📖 *Overview:*\n{overview}"
        )
        btns = []
        if imdb_id:
            btns.append(InlineKeyboardButton("🎬 IMDB", url=f"https://www.imdb.com/title/{imdb_id}"))
        btns.append(InlineKeyboardButton("🎞 TMDB", url=f"https://www.themoviedb.org/{mtype}/{detail['id']}"))
        keyboard = InlineKeyboardMarkup([btns])

        await wait.delete()
        if poster:
            await message.reply_photo(f"{TMDB_IMG}{poster}", caption=text, reply_markup=keyboard)
        else:
            await message.reply(text, reply_markup=keyboard)

    # ── /ott ──────────────────────────────────────────────────────────────────

    @app.on_message(filters.command("ott"))
    async def cmd_ott(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)

        args = message.command[1:]
        if not args:
            await message.reply(
                "📺 *OTT Availability*\n\nUsage: `/ott <movie or series name>`\nExample: `/ott Pushpa 2`"
            )
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Checking OTT for *{query}*…")

        async with httpx.AsyncClient(timeout=20) as hx:
            hit = await _tmdb_search(query, hx)
            if not hit:
                await wait.edit(f"❌ Could not find *{query}* on TMDB.")
                return
            mtype       = hit["media_type"]
            tmdb_id     = hit["id"]
            clean_title = hit.get("title") or hit.get("name") or query
            year        = (hit.get("release_date") or hit.get("first_air_date") or "")[:4]
            poster      = hit.get("poster_path", "")

            result = None
            if JUSTWATCH_API:
                result = await _ott_justwatch(hx, clean_title, year)
            if result is None:
                result = await _ott_tmdb(hx, mtype, tmdb_id, clean_title, year)

        title_str = result["title"]
        year_str  = result["year"]
        providers = result["providers"]
        source    = result["source"]

        lines = [f"🎬 *{title_str}* ({year_str})\n\n🔥 *Availability :*\n"]
        found = False
        for key, (emoji, label) in _OTT_LABELS.items():
            names = providers.get(key, [])
            if names:
                lines.append(f"{emoji} {label}: {', '.join(names)}")
                found = True
        if not found:
            lines.append("_Not available on any streaming platform in your region yet._")
        lines.append(f"\n_Source: {source}_")

        jw_link  = result.get("jw_link") or f"https://www.justwatch.com/in/search?q={query.replace(' ', '+')}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔍 View on JustWatch", url=jw_link)]])
        text     = "\n".join(lines)

        await wait.delete()
        if poster:
            await message.reply_photo(f"{TMDB_IMG}{poster}", caption=text, reply_markup=keyboard)
        else:
            await message.reply(text, reply_markup=keyboard)

    # ── /posters ──────────────────────────────────────────────────────────────

    # Store poster sessions per chat: {chat_id: {posters, index, title, tmdb_id, mtype}}
    _poster_sessions: dict[int, dict] = {}

    @app.on_message(filters.command("posters"))
    async def cmd_posters(client: Client, message: Message):
        u = message.from_user
        await db.ensure_user(u.id, u.username, u.full_name)

        allowed, used, limit = await db.check_and_consume(u.id, "poster")
        if not allowed:
            await message.reply(
                f"⚠️ You've used {used}/{limit} poster searches this month.\n"
                "Upgrade to Premium for unlimited access!"
            )
            return

        args = message.command[1:]
        if not args:
            await message.reply("🖼 *Movie Posters*\n\nUsage: `/posters <movie name>`\nExample: `/posters RRR`")
            return

        query = " ".join(args)
        wait  = await message.reply(f"🔍 Fetching posters for *{query}*…")

        async with httpx.AsyncClient(timeout=15) as hx:
            hit = await _tmdb_search(query, hx)
            if not hit:
                await wait.edit("❌ No results found.")
                return
            detail = await _tmdb_detail(hit["media_type"], hit["id"], hx)

        images  = detail.get("images", {})
        posters = images.get("posters", []) + images.get("backdrops", [])
        if not posters:
            await wait.edit("❌ No posters found for this title.")
            return

        title = detail.get("title") or detail.get("name", "")
        year  = (detail.get("release_date") or detail.get("first_air_date", ""))[:4]

        _poster_sessions[message.chat.id] = {
            "posters": posters,
            "index":   0,
            "title":   f"{title} ({year})",
            "tmdb_id": detail["id"],
            "mtype":   hit["media_type"],
        }

        await wait.delete()
        await _send_poster(client, message.chat.id, message.chat.id, reply_msg=message)

    async def _send_poster(client: Client, chat_id: int, session_key: int,
                           reply_msg=None, edit_msg=None):
        s      = _poster_sessions.get(session_key)
        if not s:
            return
        p      = s["posters"][s["index"]]
        total  = len(s["posters"])
        idx    = s["index"]
        ptype  = "Portrait" if p.get("aspect_ratio", 1) < 1 else "Landscape"
        lang   = (p.get("iso_639_1") or "N/A").upper()
        w, h   = p.get("width", 0), p.get("height", 0)
        url    = f"https://image.tmdb.org/t/p/original{p['file_path']}"

        caption = (
            f"🎬 *{s['title']}*\n"
            f"• TMDB : https://www.themoviedb.org/{s['mtype']}/{s['tmdb_id']}\n"
            f"• Type : `{ptype}`\n"
            f"• Language: `{lang}`\n"
            f"• Width: `{w}`, Height: `{h}`\n"
            f"• [Full size]({url})"
        )
        nav = [
            InlineKeyboardButton("⏮", callback_data="poster_first"),
            InlineKeyboardButton("◀",  callback_data="poster_prev"),
            InlineKeyboardButton(f"{idx+1}/{total}", callback_data="poster_noop"),
            InlineKeyboardButton("▶",  callback_data="poster_next"),
            InlineKeyboardButton("⏭", callback_data="poster_last"),
        ]
        ctrl    = [InlineKeyboardButton("❌ Close", callback_data="poster_close")]
        keyboard = InlineKeyboardMarkup([nav, ctrl])

        if edit_msg:
            await edit_msg.delete()
        await client.send_photo(chat_id, url, caption=caption, reply_markup=keyboard)

    @app.on_callback_query(filters.regex(r"^poster_"))
    async def poster_cb(client: Client, query: CallbackQuery):
        action = query.data
        cid    = query.message.chat.id
        s      = _poster_sessions.get(cid)
        await query.answer()

        if action == "poster_close":
            await query.message.delete()
            return
        if not s:
            await query.answer("Session expired. Use /posters again.", show_alert=True)
            return

        total = len(s["posters"])
        if   action == "poster_next":  s["index"] = (s["index"] + 1) % total
        elif action == "poster_prev":  s["index"] = (s["index"] - 1) % total
        elif action == "poster_first": s["index"] = 0
        elif action == "poster_last":  s["index"] = total - 1
        elif action == "poster_noop":  return

        await _send_poster(client, cid, cid, edit_msg=query.message)
