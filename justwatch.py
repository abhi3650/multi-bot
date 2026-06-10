"""
justwatch.py — Async JustWatch wrapper

Uses the FREE public API (apis.justwatch.com/content/) — no token, no contract.
Based on the JustWatchAPI open-source library (github.com/dawoudt/JustWatchAPI).

Key endpoints:
  GET  /locales/state                          — country → locale mapping
  POST /titles/{locale}/popular                — search (query in payload)
  GET  /providers/locale/{locale}              — provider id → name
  GET  /titles/{type}/{id}/locale/{locale}     — title detail with offers
"""

import asyncio
import logging
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_HEADER = {
    "User-Agent": "JustWatch client (github.com/dawoudt/JustWatchAPI)",
}
_BASE = "https://apis.justwatch.com/content"

# India locale — set at module level, resolved once
_LOCALE: str = "en_IN"

# Provider id → clear name cache (loaded once per process)
_provider_cache: dict[int, str] = {}


# ── Locale resolution ─────────────────────────────────────────────────────────

async def resolve_locale(country: str = "IN") -> str:
    """
    Resolve country code → full locale string using the JustWatch locales API.
    Defaults to 'en_IN' if the call fails.
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=_HEADER) as hx:
            r = await hx.get(f"{_BASE}/locales/state")
            if r.status_code == 200:
                for item in r.json():
                    if item.get("iso_3166_2") == country or item.get("country") == country:
                        return item["full_locale"]
    except Exception as exc:
        log.debug("[jw/locale] %s", exc)
    return "en_IN"


# ── Provider cache ────────────────────────────────────────────────────────────

async def _ensure_providers(locale: str = _LOCALE) -> dict[int, str]:
    global _provider_cache
    if _provider_cache:
        return _provider_cache
    try:
        async with httpx.AsyncClient(timeout=10, headers=_HEADER) as hx:
            r = await hx.get(f"{_BASE}/providers/locale/{locale}")
            if r.status_code == 200:
                for p in r.json():
                    _provider_cache[int(p["id"])] = p.get("clear_name") or p.get("short_name", str(p["id"]))
                log.info("[jw/providers] Loaded %d providers", len(_provider_cache))
    except Exception as exc:
        log.warning("[jw/providers] %s", exc)
    return _provider_cache


# ── Search ────────────────────────────────────────────────────────────────────

async def search(query: str, locale: str = _LOCALE, page_size: int = 8) -> list[dict]:
    """
    Search JustWatch for titles matching query.
    Returns list of item dicts — each has: id, title, object_type, offers, original_release_year.
    No token required.
    """
    url = f"{_BASE}/titles/{locale}/popular"
    payload = {
        "query":     query,
        "page_size": page_size,
        "page":      1,
    }
    try:
        async with httpx.AsyncClient(timeout=15, headers=_HEADER) as hx:
            r = await hx.post(url, json=payload)
            if r.status_code == 200:
                return r.json().get("items", [])
            log.warning("[jw/search] HTTP %s for %r", r.status_code, query)
    except Exception as exc:
        log.warning("[jw/search] %s", exc)
    return []


# ── Title detail ──────────────────────────────────────────────────────────────

async def get_title(title_id: int, content_type: str = "movie",
                    locale: str = _LOCALE) -> dict | None:
    """
    Fetch full title detail including all offers.
    content_type: 'movie' or 'show'
    """
    url = f"{_BASE}/titles/{content_type}/{title_id}/locale/{locale}"
    try:
        async with httpx.AsyncClient(timeout=15, headers=_HEADER) as hx:
            r = await hx.get(url)
            if r.status_code == 200:
                return r.json()
            log.warning("[jw/title] HTTP %s id=%s", r.status_code, title_id)
    except Exception as exc:
        log.warning("[jw/title] %s", exc)
    return None


# ── Offer formatting ──────────────────────────────────────────────────────────

_MTYPE_ORDER = ["flatrate", "free", "ads", "buy", "rent"]
_MTYPE_LABEL = {
    "flatrate": ("✅", "Subscription"),
    "free":     ("🆓", "Free"),
    "ads":      ("📢", "With Ads"),
    "buy":      ("🛒", "Buy"),
    "rent":     ("💰", "Rent"),
}


async def format_offers(offers: list, locale: str = _LOCALE) -> str:
    """
    Convert list of offer dicts → formatted availability text.
    Resolves provider IDs to human-readable names.
    Returns markdown string.
    """
    if not offers:
        return "_Not available on any streaming platform yet._"

    providers = await _ensure_providers(locale)
    grouped: dict[str, list[str]] = {}

    for offer in offers:
        mtype = (offer.get("monetization_type") or "").lower()
        pid   = offer.get("provider_id")
        name  = providers.get(int(pid), f"Provider {pid}") if pid else "Unknown"
        if mtype in _MTYPE_LABEL:
            grouped.setdefault(mtype, [])
            if name not in grouped[mtype]:
                grouped[mtype].append(name)

    if not grouped:
        return "_Not available on any streaming platform yet._"

    lines = []
    for mtype in _MTYPE_ORDER:
        names = grouped.get(mtype, [])
        if names:
            emoji, label = _MTYPE_LABEL[mtype]
            lines.append(f"{emoji} **{label}:** {', '.join(names)}")

    return "\n".join(lines) if lines else "_Not available on any streaming platform yet._"

# Alias — search_all is the same as search (kept for backward compatibility)
search_all = search
