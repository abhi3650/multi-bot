"""
database.py — MongoDB backend via Motor (async)

Collections:
  users           — one doc per Telegram user
  pending_premium — UTR payment verifications waiting for admin approval
  config          — bot-wide key/value settings (e.g. YouTube cookies)
"""

import time
from typing import Optional

import motor.motor_asyncio
from config import MONGO_URI, MONGO_DB, FREE_MEDIA_LIMIT, FREE_POSTER_LIMIT, PREMIUM_DAYS

# ── Connection ────────────────────────────────────────────────────────────────

_client:  Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
_db       = None
_users    = None
_pending  = None
_config   = None


def _get_db():
    global _client, _db, _users, _pending, _config
    if _client is None:
        _client  = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        _db      = _client[MONGO_DB]
        _users   = _db["users"]
        _pending = _db["pending_premium"]
        _config  = _db["config"]
    return _users, _pending


def _cfg():
    _get_db()
    return _config


async def init_db():
    """Create indexes on first run."""
    users, pending = _get_db()
    await users.create_index("user_id", unique=True)
    await pending.create_index("user_id", unique=True)
    await _cfg().create_index("key", unique=True)


# ── Cookie storage ────────────────────────────────────────────────────────────
# Stores the raw Netscape cookies.txt content in MongoDB.
# yt-dlp reads it via a temp file written before each download.

async def save_cookies(content: str) -> None:
    """Upsert cookies.txt content into the config collection."""
    await _cfg().update_one(
        {"key": "yt_cookies"},
        {"$set": {
            "key":        "yt_cookies",
            "value":      content,
            "updated_at": int(time.time()),
            "size":       len(content.encode()),
        }},
        upsert=True,
    )


async def get_cookies() -> str | None:
    """Return the stored cookies.txt content, or None if not uploaded yet."""
    doc = await _cfg().find_one({"key": "yt_cookies"})
    return doc["value"] if doc else None


async def delete_cookies() -> bool:
    """Delete stored cookies. Returns True if something was actually deleted."""
    r = await _cfg().delete_one({"key": "yt_cookies"})
    return r.deleted_count > 0


async def get_cookies_meta() -> dict | None:
    """Return metadata (updated_at, size) without the full content."""
    return await _cfg().find_one({"key": "yt_cookies"}, {"value": 0, "_id": 0})


# ── User management ───────────────────────────────────────────────────────────

async def ensure_user(user_id: int, username: str, full_name: str):
    users, _ = _get_db()
    now = int(time.time())
    await users.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "user_id":        user_id,
                "is_premium":     False,
                "premium_expiry": 0,
                "media_usage":    0,
                "poster_usage":   0,
                "last_reset":     now,
                "joined_at":      now,
            },
            "$set": {
                "username":  username or "",
                "full_name": full_name or "",
            },
        },
        upsert=True,
    )


async def get_user(user_id: int) -> dict | None:
    users, _ = _get_db()
    return await users.find_one({"user_id": user_id})


async def get_all_users() -> list[int]:
    users, _ = _get_db()
    return [doc["user_id"] async for doc in users.find({}, {"user_id": 1})]


# ── Usage tracking ────────────────────────────────────────────────────────────

async def _auto_reset(user_id: int):
    users, _ = _get_db()
    doc = await users.find_one({"user_id": user_id}, {"last_reset": 1})
    if doc and (time.time() - doc.get("last_reset", 0)) > 30 * 86400:
        await users.update_one(
            {"user_id": user_id},
            {"$set": {"media_usage": 0, "poster_usage": 0, "last_reset": int(time.time())}},
        )


async def check_and_consume(user_id: int, category: str) -> tuple[bool, int, int]:
    """category: 'media' | 'poster'. Returns (allowed, used_after, limit)."""
    users, _ = _get_db()
    await _auto_reset(user_id)

    doc = await users.find_one({"user_id": user_id})
    if not doc:
        return False, 0, 0

    is_prem = bool(doc.get("is_premium")) and doc.get("premium_expiry", 0) > time.time()
    field   = f"{category}_usage"
    used    = doc.get(field, 0)
    limit   = FREE_MEDIA_LIMIT if category == "media" else FREE_POSTER_LIMIT

    if is_prem:
        await users.update_one({"user_id": user_id}, {"$inc": {field: 1}})
        return True, used + 1, 999

    if used >= limit:
        return False, used, limit

    await users.update_one({"user_id": user_id}, {"$inc": {field: 1}})
    return True, used + 1, limit


# ── Premium management ────────────────────────────────────────────────────────

async def grant_premium(user_id: int, days: int = PREMIUM_DAYS):
    users, pending = _get_db()
    expiry = int(time.time()) + days * 86400
    await users.update_one(
        {"user_id": user_id},
        {"$set": {"is_premium": True, "premium_expiry": expiry}},
    )
    await pending.delete_one({"user_id": user_id})


async def revoke_premium(user_id: int):
    users, _ = _get_db()
    await users.update_one(
        {"user_id": user_id},
        {"$set": {"is_premium": False, "premium_expiry": 0}},
    )


async def add_pending(user_id: int, utr: str):
    _, pending = _get_db()
    await pending.update_one(
        {"user_id": user_id},
        {"$set": {"utr": utr, "requested_at": int(time.time())}},
        upsert=True,
    )


async def get_pending() -> list[dict]:
    _, pending = _get_db()
    return [doc async for doc in pending.find({}, {"_id": 0}).sort("requested_at", 1)]


async def is_premium(user_id: int) -> bool:
    doc = await get_user(user_id)
    if not doc:
        return False
    return bool(doc.get("is_premium")) and doc.get("premium_expiry", 0) > time.time()
