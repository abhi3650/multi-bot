import httpx
from pyrogram import Client, filters
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                             InlineKeyboardMarkup, Message)

import database as db
from config import TMDB_API_KEY, TMDB_BASE, TMDB_IMG, JUSTWATCH_API

_poster_sessions: dict = {}   # {chat_id: {posters, index, title, tmdb_id, mtype}}

_OTT_LABELS = {
    "flatrate": ("✅","Subscription"),
    "free":     ("🆓","Free"),
    "ads":      ("📢","With Ads"),
    "rent":     ("💰","Rent"),
    "buy":      ("🛒","Buy"),
}

_JW_GQL = """
query OttSearch($country:Country!,$first:Int!,$filter:TitleFilter!,$language:Language!){
  popularTitles(country:$country,first:$first,filter:$filter){
    edges{node{
      ...on Movie{content(country:$country,language:$language){title originalReleaseYear}
        offers(country:$country,platform:WEB){monetizationType provider{clearName}}}
      ...on Show{content(country:$country,language:$language){title originalReleaseYear}
        offers(country:$country,platform:WEB){monetizationType provider{clearName}}}
    }}
  }
}
"""


async def _tmdb_search(query: str, client: httpx.AsyncClient) -> dict | None:
    r = await client.get(f"{TMDB_BASE}/search/multi",
                         params={"api_key":TMDB_API_KEY,"query":query,"include_adult":"false"})
    results = r.json().get("results",[])
    return next((x for x in results if x.get("media_type") in ("movie","tv")), None)


async def _tmdb_detail(mtype: str, tmdb_id: int, client: httpx.AsyncClient) -> dict:
    r = await client.get(f"{TMDB_BASE}/{mtype}/{tmdb_id}",
                         params={"api_key":TMDB_API_KEY,"append_to_response":"credits,external_ids,images"})
    return r.json()


# ── /imdb ──────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("imdb"))
async def cmd_imdb(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")

    args = message.command[1:]
    if not args:
        return await message.reply("🎬 Usage: `/imdb <movie or series name>`")

    query = " ".join(args)
    msg   = await message.reply(f"🔍 Searching **{query}**…")

    async with httpx.AsyncClient(timeout=15) as c:
        hit = await _tmdb_search(query, c)
        if not hit:
            return await msg.edit("❌ No results found.")
        mtype   = hit["media_type"]
        detail  = await _tmdb_detail(mtype, hit["id"], c)

    title   = detail.get("title") or detail.get("name","Unknown")
    year    = (detail.get("release_date") or detail.get("first_air_date",""))[:4]
    rating  = detail.get("vote_average",0)
    votes   = detail.get("vote_count",0)
    runtime = detail.get("runtime") or (detail.get("episode_run_time") or [0])[0]
    genres  = ", ".join(g["name"] for g in detail.get("genres",[])[:4]) or "N/A"
    overview = (detail.get("overview") or "No overview.")[:600]
    lang    = (detail.get("original_language") or "").upper()
    cast    = detail.get("credits",{}).get("cast",[])[:5]
    cast_str = ", ".join(c["name"] for c in cast) or "N/A"
    imdb_id  = detail.get("external_ids",{}).get("imdb_id","")
    poster   = detail.get("poster_path","")

    text = (
        f"🎬 **{title}** ({year})\n"
        f"⭐ Rating : `{rating:.1f}/10` ({votes:,} votes)\n"
        f"🎭 Genre  : `{genres}`\n"
        f"⏱ Runtime : `{runtime} min`\n"
        f"🌐 Language: `{lang}`\n"
        f"👥 Cast    : `{cast_str}`\n\n"
        f"📖 **Overview:**\n{overview}"
    )
    buttons = []
    if imdb_id:
        buttons.append(InlineKeyboardButton("🎬 IMDB", url=f"https://www.imdb.com/title/{imdb_id}"))
    buttons.append(InlineKeyboardButton("🎞 TMDB", url=f"https://www.themoviedb.org/{mtype}/{detail['id']}"))
    kb = InlineKeyboardMarkup([buttons])

    await msg.delete()
    if poster:
        await message.reply_photo(photo=f"{TMDB_IMG}{poster}", caption=text, reply_markup=kb)
    else:
        await message.reply(text, reply_markup=kb)


# ── /ott ───────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("ott"))
async def cmd_ott(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")

    args = message.command[1:]
    if not args:
        return await message.reply("📺 Usage: `/ott <movie or series name>`")

    query = " ".join(args)
    msg   = await message.reply(f"🔍 Checking OTT for **{query}**…")

    async with httpx.AsyncClient(timeout=15) as c:
        hit = await _tmdb_search(query, c)
        if not hit:
            return await msg.edit("❌ Could not find that title on TMDB.")
        mtype   = hit["media_type"]
        tmdb_id = hit["id"]
        title   = hit.get("title") or hit.get("name") or query
        year    = (hit.get("release_date") or hit.get("first_air_date",""))[:4]
        poster  = hit.get("poster_path","")

    # Try JustWatch if key is set
    ott_data = None
    if JUSTWATCH_API:
        ott_data = await _jw_lookup(title, JUSTWATCH_API)

    # Fallback: TMDB Watch Providers
    if not ott_data:
        ott_data = await _tmdb_providers(tmdb_id, mtype, title, year, query)

    text    = _ott_text(ott_data)
    buttons = [[InlineKeyboardButton("🔍 View on JustWatch", url=ott_data["link"])]]

    await msg.delete()
    if poster:
        await message.reply_photo(photo=f"{TMDB_IMG}{poster}", caption=text,
                                  reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))


async def _jw_lookup(title: str, api_key: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post("https://apis.justwatch.com/graphql",
                json={"query":_JW_GQL,"variables":{"country":"IN","language":"en","first":5,"filter":{"searchQuery":title}}},
                headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"})
        if r.status_code != 200: return None
        edges = r.json().get("data",{}).get("popularTitles",{}).get("edges",[])
        if not edges: return None
        node = next((e["node"] for e in edges if title.lower() in (e["node"].get("content",{}).get("title","")).lower()), edges[0]["node"])
        content = node.get("content",{})
        pm: dict[str, list] = {}
        for offer in node.get("offers",[]):
            m = offer.get("monetizationType","").lower()
            p = (offer.get("provider",{}) or {}).get("clearName","")
            if m and p:
                pm.setdefault(m,[])
                if p not in pm[m]: pm[m].append(p)
        return {"title":content.get("title",title),"year":content.get("originalReleaseYear",""),
                "provider_map":pm,"link":f"https://www.justwatch.com/in/search?q={title.replace(' ','+')}","source":"JustWatch"}
    except Exception:
        return None


async def _tmdb_providers(tmdb_id, mtype, title, year, query) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{TMDB_BASE}/{mtype}/{tmdb_id}/watch/providers", params={"api_key":TMDB_API_KEY})
    region = r.json().get("results",{}).get("IN") or r.json().get("results",{}).get("US") or {}
    pm: dict[str, list] = {}
    for key in ("flatrate","free","ads","rent","buy"):
        if ps := region.get(key,[]):
            pm[key] = [p["provider_name"] for p in ps]
    return {"title":title,"year":year,"provider_map":pm,
            "link":region.get("link") or f"https://www.justwatch.com/in/search?q={query.replace(' ','+')}","source":"TMDB"}


def _ott_text(data: dict) -> str:
    lines = [f"🎬 **{data['title']}** ({data['year']})\n\n🔥 **Availability:**\n"]
    found = False
    for key,(emoji,label) in _OTT_LABELS.items():
        if providers := data["provider_map"].get(key,[]):
            lines.append(f"{emoji} {label}: {', '.join(providers)}")
            found = True
    if not found:
        lines.append("_Not available on any streaming platform in your region._")
    lines.append(f"\n_Source: {data['source']}_")
    return "\n".join(lines)


# ── /posters ───────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("posters"))
async def cmd_posters(client: Client, message: Message):
    u = message.from_user
    await db.ensure_user(u.id, u.username, u.first_name or "")
    allowed, used, limit = await db.check_and_consume(u.id, "poster")
    if not allowed:
        return await message.reply(f"⚠️ You've used {used}/{limit} poster searches this month.\nUpgrade to Premium for unlimited access!")

    args = message.command[1:]
    if not args:
        return await message.reply("🖼 Usage: `/posters <movie name>`")

    query = " ".join(args)
    msg   = await message.reply(f"🔍 Fetching posters for **{query}**…")

    async with httpx.AsyncClient(timeout=15) as c:
        hit = await _tmdb_search(query, c)
        if not hit:
            return await msg.edit("❌ No results found.")
        detail = await _tmdb_detail(hit["media_type"], hit["id"], c)

    images  = detail.get("images",{})
    posters = images.get("posters",[]) + images.get("backdrops",[])
    if not posters:
        return await msg.edit("❌ No posters found for this title.")

    title = detail.get("title") or detail.get("name","")
    year  = (detail.get("release_date") or detail.get("first_air_date",""))[:4]
    chat_id = message.chat.id

    _poster_sessions[chat_id] = {
        "posters": posters, "index": 0,
        "title": f"{title} ({year})", "tmdb_id": detail["id"], "mtype": hit["media_type"]
    }

    await msg.delete()
    await _send_poster(message.chat, chat_id, is_new=True)


async def _send_poster(chat, chat_id: int, is_new: bool = False, cq: CallbackQuery = None):
    sess    = _poster_sessions.get(chat_id)
    if not sess: return
    posters = sess["posters"]
    idx     = sess["index"]
    total   = len(posters)
    p       = posters[idx]
    ptype   = "Portrait" if p.get("aspect_ratio",1) < 1 else "Landscape"
    lang    = (p.get("iso_639_1") or "N/A").upper()
    w, h    = p.get("width",0), p.get("height",0)
    url     = f"https://image.tmdb.org/t/p/original{p['file_path']}"

    caption = (
        f"🎬 **{sess['title']}**\n"
        f"• Type: `{ptype}` | Language: `{lang}`\n"
        f"• Width: `{w}`, Height: `{h}`\n"
        f"• [Click Here]({url})"
    )
    nav = [
        InlineKeyboardButton("⏮", callback_data="poster_first"),
        InlineKeyboardButton("◀",  callback_data="poster_prev"),
        InlineKeyboardButton(f"{idx+1}/{total}", callback_data="poster_noop"),
        InlineKeyboardButton("▶",  callback_data="poster_next"),
        InlineKeyboardButton("⏭", callback_data="poster_last"),
    ]
    ctrl = [
        InlineKeyboardButton("❌ Close", callback_data="poster_close"),
    ]
    kb = InlineKeyboardMarkup([nav, ctrl])

    if is_new:
        await chat.send_photo(url, caption=caption, reply_markup=kb)
    elif cq:
        await cq.message.delete()
        await cq.message.reply_photo(url, caption=caption, reply_markup=kb)


@Client.on_callback_query(filters.regex("^poster_"))
async def poster_callback(client: Client, cq: CallbackQuery):
    await cq.answer()
    action  = cq.data
    chat_id = cq.message.chat.id
    sess    = _poster_sessions.get(chat_id)
    if not sess:
        return await cq.message.delete()

    total = len(sess["posters"])
    idx   = sess["index"]

    if action == "poster_close":
        del _poster_sessions[chat_id]
        return await cq.message.delete()
    elif action == "poster_next":  sess["index"] = (idx + 1) % total
    elif action == "poster_prev":  sess["index"] = (idx - 1) % total
    elif action == "poster_first": sess["index"] = 0
    elif action == "poster_last":  sess["index"] = total - 1
    elif action == "poster_noop":  return

    await _send_poster(None, chat_id, cq=cq)
