"""
justwatch.py — OTT availability via TMDB Watch Providers

JustWatch's public API (apis.justwatch.com/content/) returns 403 from cloud
servers due to Cloudflare bot protection. TMDB Watch Providers is the reliable
server-side alternative — it sources data from JustWatch and always works.

Endpoints:
  GET /3/{type}/{id}/watch/providers  → streaming availability by country
  GET /3/search/multi                 → title search to resolve TMDB ID
"""

import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_TMDB_BASE = "https://api.themoviedb.org/3"


_MTYPE_ORDER = ["flatrate", "free", "ads", "buy", "rent"]
_MTYPE_LABEL = {
    "flatrate": ("✅", "Subscription"),
    "free":     ("🆓", "Free"),
    "ads":      ("📢", "With Ads"),
    "buy":      ("🛒", "Buy"),
    "rent":     ("💰", "Rent"),
}


async def search(query: str, api_key: str, page_size: int = 8) -> list[dict]:
    """
    Search TMDB for titles matching query.
    Returns list of dicts with: id, media_type, title/name, release_date/first_air_date
    """
    try:
        async with httpx.AsyncClient(timeout=15) as hx:
            r = await hx.get(
                f"{_TMDB_BASE}/search/multi",
                params={"api_key": api_key, "query": query,
                        "include_adult": "false", "page": 1},
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
                return [x for x in results if x.get("media_type") in ("movie", "tv")][:page_size]
            log.warning("[ott/search] TMDB HTTP %s", r.status_code)
    except Exception as exc:
        log.warning("[ott/search] %s", exc)
    return []


# Alias kept for backward compatibility
search_all = search


async def get_providers(
    tmdb_id: int,
    media_type: str,   # "movie" or "tv"
    api_key: str,
    country: str = "IN",
) -> dict:
    """
    Fetch streaming providers for a title from TMDB.
    Returns dict with 'providers' (grouped), 'link' (JustWatch URL), 'title', 'year'.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as hx:
            r = await hx.get(
                f"{_TMDB_BASE}/{media_type}/{tmdb_id}/watch/providers",
                params={"api_key": api_key},
            )
            if r.status_code == 200:
                results = r.json().get("results", {})
                # Prefer India, fall back to US
                region = results.get(country) or results.get("US") or {}
                return {
                    "providers": region,
                    "link":      region.get("link", ""),
                    "all":       results,
                }
    except Exception as exc:
        log.warning("[ott/providers] %s", exc)
    return {"providers": {}, "link": "", "all": {}}


async def format_providers(providers: dict, title: str) -> str:
    """
    Format TMDB watch providers dict into readable text.
    providers: the country-specific sub-dict from TMDB watch/providers response
    """
    if not providers:
        return "_Not available on any streaming platform yet._"

    lines = []
    for key in _MTYPE_ORDER:
        items = providers.get(key, [])
        if items:
            emoji, label = _MTYPE_LABEL[key]
            names = [p.get("provider_name", "?") for p in items]
            lines.append(f"{emoji} **{label}:** {', '.join(names)}")

    return "\n".join(lines) if lines else "_Not available on any streaming platform yet._"


async def get_title(tmdb_id: int, content_type: str = "movie",
                    api_key: str = "", country: str = "IN") -> dict | None:
    """Fetch full title detail — kept for API compatibility."""
    data = await get_providers(tmdb_id, content_type, api_key, country)
    return {"offers": data.get("providers", {})} if data else None


async def format_offers(offers: dict, locale: str = "en_IN") -> str:
    """Alias — offers here is the providers sub-dict."""
    return await format_providers(offers, "")
