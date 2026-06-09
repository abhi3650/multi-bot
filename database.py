"""
database.py — MongoDB backend (Motor async)

Collections:
  users           — Telegram users + usage tracking
  pending_premium — UTR payment verifications
  config          — Bot-wide key/value (e.g. YouTube cookies)
  link_files      — Permanent file store for /link command
"""

import time
from typing import Optional

import motor.motor_asyncio
from bson import ObjectId
from bson.errors import InvalidId

from config import MONGO_URI, MONGO_DB, FREE_MEDIA_LIMIT, FREE_POSTER_LIMIT, PREMIUM_DAYS

_client:  Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
_db       = None
_users    = None
_pending  = None
_config   = None
_files    = None   # permanent link file store


def _get_db():
    global _client, _db, _users, _pending, _config, _files
    if _client is None:
        _client  = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        _db      = _client[MONGO_DB]
        _users   = _db["users"]
        _pending = _db["pending_premium"]
        _config  = _db["config"]
        _files   = _db["link_files"]
    return _users, _pending


def _cfg():
    _get_db()
    return _config


def _fdb():
    _get_db()
    return _files


async def init_db():
    users, pending = _get_db()
    await users.create_index("user_id", unique=True)
    await pending.create_index("user_id", unique=True)
    await _cfg().create_index("key", unique=True)
    await _fdb().create_index("tg_file_unique_id")


# ── Cookie storage ────────────────────────────────────────────────────────────

async def save_cookies(content: str) -> None:
    await _cfg().update_one(
        {"key": "yt_cookies"},
        {"$set": {"key": "yt_cookies", "value": content,
                  "updated_at": int(time.time()), "size": len(content.encode())}},
        upsert=True,
    )


async def get_cookies() -> str | None:
    doc = await _cfg().find_one({"key": "yt_cookies"})
    return doc["value"] if doc else None


async def delete_cookies() -> bool:
    r = await _cfg().delete_one({"key": "yt_cookies"})
    return r.deleted_count > 0


async def get_cookies_meta() -> dict | None:
    return await _cfg().find_one({"key": "yt_cookies"}, {"value": 0, "_id": 0})


# ── Permanent link file store ─────────────────────────────────────────────────

async def add_link_file(
    tg_file_id: str,
    tg_file_unique_id: str,
    file_name: str,
    file_size: int,
    mime_type: str,
    dump_msg_id: int,
) -> str:
    """
    Store file metadata in MongoDB. Returns the MongoDB _id as a string.
    If the file_unique_id already exists, returns the existing record's id.
    """
    existing = await _fdb().find_one({"tg_file_unique_id": tg_file_unique_id})
    if existing:
        return str(existing["_id"])

    result = await _fdb().insert_one({
        "tg_file_id":        tg_file_id,
        "tg_file_unique_id": tg_file_unique_id,
        "file_name":         file_name,
        "file_size":         file_size,
        "mime_type":         mime_type,
        "dump_msg_id":       dump_msg_id,
        "created_at":        int(time.time()),
    })
    return str(result.inserted_id)


async def get_link_file(mongo_id: str) -> dict | None:
    """Retrieve file metadata by MongoDB _id string."""
    try:
        doc = await _fdb().find_one({"_id": ObjectId(mongo_id)})
        return doc
    except (InvalidId, Exception):
        return None


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
            "$set": {"username": username or "", "full_name": full_name or ""},
        },
        upsert=True,
    )


async def get_user(user_id: int) -> dict | None:
    users, _ = _get_db()
    return await users.find_one({"user_id": user_id})


async def get_all_users() -> list[int]:
    users, _ = _get_db()
    return [doc["user_id"] async for doc in users.find({}, {"user_id": 1})]


async def _auto_reset(user_id: int):
    users, _ = _get_db()
    doc = await users.find_one({"user_id": user_id}, {"last_reset": 1})
    if doc and (time.time() - doc.get("last_reset", 0)) > 30 * 86400:
        await users.update_one(
            {"user_id": user_id},
            {"$set": {"media_usage": 0, "poster_usage": 0, "last_reset": int(time.time())}},
        )


async def check_and_consume(user_id: int, category: str) -> tuple[bool, int, int]:
    users, _ = _get_db()
    await _auto_reset(user_id)
    doc   = await users.find_one({"user_id": user_id})
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
    await users.update_one({"user_id": user_id}, {"$set": {"is_premium": True, "premium_expiry": expiry}})
    await pending.delete_one({"user_id": user_id})


async def revoke_premium(user_id: int):
    users, _ = _get_db()
    await users.update_one({"user_id": user_id}, {"$set": {"is_premium": False, "premium_expiry": 0}})


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
