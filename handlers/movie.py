"""
handlers/movie.py  —  /imdb  /ott  /posters

/ott UI (matches screenshots exactly):
  Step 1 — "Search Results For <query>":
              [Meesaya Murukku (2017)]
              [Close]
  Step 2 — Show availability:
              ≡ Title (Year)
              🔥 Availability :-
              • Platform1 •
              CC ~ @BotName
              [JustWatch preview card]

  JustWatch only — no TMDB fallback (removed as requested).
  If JUSTWATCH_API is not set, shows a config message.

/posters UI (matches screenshots):
  Step 1 — Type picker: [Landscape (N)] [Portrait (N)] [Clean Landscape (N)]
  Step 2 — Paginated: [<<] [<] [1/N] [>] [>>] + [Back] [Close]
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
from config import TMDB_API_KEY, TMDB_BASE, TMDB_IMG, JUSTWATCH_API, BOT_USERNAME

MD  = ParseMode.MARKDOWN
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _full_name(u) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    return " ".join(p for p in parts if p).strip() or "Unknown"


def _sk(seed: str) -> str:
    return hashlib.md5(seed.encode()).hexdigest()[:10]


async def _tmdb_search_multi(query: str, hx: httpx.AsyncClient) -> list[dict]:
    """Return up to 5 movie/tv results from TMDB."""
    r = await hx.get(
        f"{TMDB_BASE}/search/multi",
        params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "false"},
    )
    results = r.json().get("results", [])
    return [x for x in results if x.get("media_type") in ("movie", "tv")][:5]


async def _tmdb_search(query: str, hx: httpx.AsyncClient) -> dict | None:
    results = await _tmdb_search_multi(query, hx)
    return results[0] if results else None


async def _tmdb_detail(mtype: str, tmdb_id: int, hx: httpx.AsyncClient) -> dict:
    r = await hx.get(
        f"{TMDB_BASE}/{mtype}/{tmdb_id}",
        params={"api_key": TMDB_API_KEY,
                "append_to_response": "credits,external_ids,images"},
    )
    return r.json()


# ── JustWatch (only OTT source) ───────────────────────────────────────────────

_JW_GQL = "https://apis.justwatch.com/graphql"
_JW_Q   = """
query GetStreamingOffers($searchQuery: String!, $country: Country!, $language: Language!) {
  searchTitles(
    searchTitlesFilter: { searchQuery: $searchQuery }
    country: $country
    language: $language
    first: 8
  ) {
    edges {
      node {
        content(country: $country, language: $language) {
          title
          originalReleaseYear
          posterUrl(profile: S276)
          externalIds { imdbId }
        }
        offers(country: $country, platform: WEB) {
          monetizationType
          provider { clearName technicalName }
        }
      }
    }
  }
}
"""

_OTT_LABELS = {
    "FLATRATE": "Subscription",
    "FREE":     "Free",
    "ADS":      "With Ads",
    "BUY":      "Buy",
    "RENT":     "Rent",
}

# OTT search sessions: { session_key: [node, ...] }
_ott_sessions: dict[str, list] = {}


async def _jw_search(hx: httpx.AsyncClient, title: str) -> list:
    """Query JustWatch GraphQL. Returns list of nodes."""
    if not JUSTWATCH_API:
        return []
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
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("[jw] HTTP %s for %r", resp.status_code, title)
            return []
        return (
            resp.json()
            .get("data", {})
            .get("searchTitles", {})
            .get("edges", [])
        )
    except Exception as exc:
        log.warning("[jw] %s", exc)
        return []


def _providers_from_node(node: dict) -> dict[str, list]:
    providers: dict[str, list] = {}
    for offer in node.get("offers") or []:
        mtype = offer.get("monetizationType", "UNKNOWN")
        name  = (offer.get("provider") or {}).get("clearName")
        if name and mtype in _OTT_LABELS:
            providers.setdefault(mtype, [])
            if name not in providers[mtype]:
                providers[mtype].append(name)
    return providers


# ── Poster helpers ────────────────────────────────────────────────────────────

def _classify_posters(images: dict) -> dict[str, list]:
    portrait, landscape, clean_landscape = [], [], []
    for p in images.get("posters", []):
        if p.get("aspect_ratio", 1) < 1:
            portrait.append(p)
    for b in images.get("backdrops", []):
        lang = b.get("iso_639_1") or ""
        if lang:
            landscape.append(b)
        else:
            clean_landscape.append(b)
    return {"portrait": portrait, "landscape": landscape,
            "clean_landscape": clean_landscape}


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
            btns_row1.append(
                InlineKeyboardButton("🎬 IMDB",
                                     url=f"https://www.imdb.com/title/{imdb_id}"))
        btns_row1.append(
            InlineKeyboardButton("🎞 TMDB",
                                 url=f"https://www.themoviedb.org/{mtype}/{detail['id']}"))
        btns_row2 = [InlineKeyboardButton(
            "📺 Check OTT",
            callback_data=f"imdb_ott|{title[:40]}",
        )]
        keyboard = InlineKeyboardMarkup([btns_row1, btns_row2])

        await wait.delete()
        if poster:
            await message.reply_photo(
                f"{TMDB_IMG}{poster}", caption=text, parse_mode=MD, reply_markup=keyboard,
            )
        else:
            await message.reply(text, parse_mode=MD, reply_markup=keyboard)

    # Inline "Check OTT" from /imdb card
    @app.on_callback_query(filters.regex(r"^imdb_ott\|"))
    async def imdb_ott_cb(client: Client, query: CallbackQuery):
        await query.answer("🔍 Searching JustWatch…")
        title = query.data.split("|", 1)[1]
        await _ott_search_flow(client, query.message, title, from_callback=True)

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
        await _ott_search_flow(client, message, query, from_callback=False)

    async def _ott_search_flow(client: Client, target: Message, query: str,
                                from_callback: bool):
        """
        Step 1: search JustWatch, show results as buttons.
        If only one result → skip to step 2 directly.
        """
        if not JUSTWATCH_API:
            text = (
                "⚠️ **JustWatch API not configured.**\n\n"
                "Add your JustWatch API key to `.env`:\n"
                "`JUSTWATCH_API=your_key_here`\n\n"
                "_Get a key from https://rapidapi.com/search/justwatch_"
            )
            if from_callback:
                await target.reply(text, parse_mode=MD)
            else:
                await target.reply(text, parse_mode=MD)
            return

        wait = await target.reply(f"🔍 Searching **{query}**…", parse_mode=MD)

        async with httpx.AsyncClient(timeout=20) as hx:
            edges = await _jw_search(hx, query)

        if not edges:
            await wait.edit(
                f"❌ No results found for **{query}** on JustWatch.",
                parse_mode=MD,
            )
            return

        nodes = [e["node"] for e in edges if e.get("node")]

        if len(nodes) == 1:
            # Only one result — go directly to detail
            await wait.delete()
            await _send_ott_detail(target, nodes[0], query)
            return

        # Multiple results — show picker
        sk = _sk(query)
        _ott_sessions[sk] = nodes

        buttons = []
        for i, node in enumerate(nodes):
            content = node.get("content") or {}
            title   = content.get("title", "Unknown")
            year    = content.get("originalReleaseYear", "")
            label   = f"{title} ({year})" if year else title
            buttons.append([InlineKeyboardButton(
                label, callback_data=f"ott_pick|{sk}|{i}"
            )])
        buttons.append([InlineKeyboardButton("❌ Close", callback_data="ott_close")])

        await wait.edit(
            f"**Search Results For** {query}",
            parse_mode=MD,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # Callback: user picks a result from search list
    @app.on_callback_query(filters.regex(r"^ott_pick\|"))
    async def ott_pick_cb(client: Client, query: CallbackQuery):
        await query.answer()
        _, sk, idx_str = query.data.split("|", 2)
        nodes = _ott_sessions.get(sk)
        if not nodes:
            await query.answer("Session expired. Search again.", show_alert=True)
            return

        idx  = int(idx_str)
        node = nodes[idx]

        await query.message.delete()

        content = node.get("content") or {}
        title   = content.get("title", "Unknown")
        await _send_ott_detail(query.message, node, title)

    @app.on_callback_query(filters.regex(r"^ott_close$"))
    async def ott_close_cb(client: Client, query: CallbackQuery):
        await query.answer()
        await query.message.delete()

    async def _send_ott_detail(target: Message, node: dict, query: str):
        """
        Step 2 — Show availability matching the screenshot:
          ≡ Title (Year)
          🔥 Availability :-
          • Platform1 •
          CC ~ @BotName
        """
        content   = node.get("content") or {}
        title     = content.get("title", query)
        year      = content.get("originalReleaseYear", "")
        providers = _providers_from_node(node)

        # Build provider text — all platforms in one line separated by • like screenshot
        all_platforms = []
        for mtype in ("FLATRATE", "FREE", "ADS"):
            all_platforms.extend(providers.get(mtype, []))
        buy_rent = []
        for mtype in ("BUY", "RENT"):
            buy_rent.extend(providers.get(mtype, []))

        title_line = f"≡ **{title}** ({year})" if year else f"≡ **{title}**"
        avail_line = "🔥 **Availability :-**\n"

        if all_platforms:
            avail_line += "• " + " •\n• ".join(all_platforms) + " •"
        elif buy_rent:
            avail_line += "• " + " • ".join(buy_rent) + " •\n_(Buy/Rent only)_"
        else:
            avail_line += "_Not available for streaming in India yet._"

        jw_search = f"https://www.justwatch.com/in/search?q={query.replace(' ', '+')}"
        credit_line = f"\nCC ~ @{BOT_USERNAME}"

        text = f"{title_line}\n\n{avail_line}{credit_line}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔍 JustWatch",
                url=jw_search,
            )
        ]])

        await target.reply(text, parse_mode=MD, reply_markup=keyboard)

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

        total_imgs = sum(len(v) for v in buckets.values())
        if total_imgs == 0:
            await wait.edit("❌ No posters found for this title.")
            return

        title    = detail.get("title") or detail.get("name", "")
        year     = (detail.get("release_date") or detail.get("first_air_date", ""))[:4]
        mtype    = hit["media_type"]
        tmdb_id  = detail["id"]
        tmdb_url = f"https://www.themoviedb.org/{mtype}/{tmdb_id}"

        _poster_sessions[message.chat.id] = {
            "buckets":  buckets,
            "type":     None,
            "index":    0,
            "title":    f"{title} ({year})",
            "mtype":    mtype,
            "tmdb_id":  tmdb_id,
            "tmdb_url": tmdb_url,
        }

        await wait.delete()
        await _send_type_picker(message, message.chat.id, reply=True)

    async def _send_type_picker(target, cid: int, reply: bool = False,
                                 edit_msg=None):
        s       = _poster_sessions.get(cid)
        if not s:
            return
        buckets = s["buckets"]

        rows, pair = [], []
        key_order  = ("landscape", "portrait", "clean_landscape")
        labels_map = {"portrait": "Portrait", "landscape": "Landscape",
                      "clean_landscape": "Clean Landscape"}
        for key in key_order:
            count = len(buckets[key])
            if count == 0:
                continue
            btn = InlineKeyboardButton(
                f"{labels_map[key]} ({count})",
                callback_data=f"pt_type|{key}",
            )
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

        text = (
            f"**{s['title']}**\n\n"
            f"TMDB : {s['tmdb_url']}\n\n"
            "**Select Poster Type :-**"
        )
        kb = InlineKeyboardMarkup(rows)

        if edit_msg:
            try:
                await edit_msg.edit(text, parse_mode=MD, reply_markup=kb)
            except Exception:
                await target.reply(text, parse_mode=MD, reply_markup=kb)
        elif reply:
            await target.reply(text, parse_mode=MD, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^pt_type\|"))
    async def poster_type_cb(client: Client, query: CallbackQuery):
        await query.answer()
        cid  = query.message.chat.id
        s    = _poster_sessions.get(cid)
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
            await query.message.delete()
            _poster_sessions.pop(cid, None)
            return

        if action == "back":
            if not s:
                return
            await _send_type_picker(query.message, cid, edit_msg=query.message)
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
        s     = _poster_sessions.get(cid)
        if not s or not s.get("type"):
            return

        items = s["buckets"].get(s["type"], [])
        idx   = s["index"]
        total = len(items)
        p     = items[idx]

        ptype_map = {"portrait": "Portrait", "landscape": "Landscape",
                     "clean_landscape": "Clean Landscape"}
        ptype = ptype_map.get(s["type"], "Unknown")
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
        ctrl    = [
            InlineKeyboardButton("🔙 Back",  callback_data="pt_back"),
            InlineKeyboardButton("❌ Close", callback_data="pt_close"),
        ]
        keyboard = InlineKeyboardMarkup([nav, ctrl])

        try:
            await msg.delete()
        except Exception:
            pass
        await client.send_photo(
            cid, url, caption=caption, parse_mode=MD, reply_markup=keyboard,
        )
