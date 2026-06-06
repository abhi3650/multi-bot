import time
import aiosqlite
from config import DB_PATH, FREE_MEDIA_LIMIT, FREE_POSTER_LIMIT, PREMIUM_DAYS


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                full_name      TEXT,
                is_premium     INTEGER DEFAULT 0,
                premium_expiry INTEGER DEFAULT 0,
                media_usage    INTEGER DEFAULT 0,
                poster_usage   INTEGER DEFAULT 0,
                last_reset     INTEGER DEFAULT 0,
                joined_at      INTEGER DEFAULT 0
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_premium (
                user_id      INTEGER PRIMARY KEY,
                utr          TEXT,
                requested_at INTEGER
            )""")
        await db.commit()


async def ensure_user(user_id: int, username: str, full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id,username,full_name,last_reset,joined_at) VALUES (?,?,?,?,?)",
            (user_id, username or "", full_name or "", int(time.time()), int(time.time()))
        )
        await db.execute(
            "UPDATE users SET username=?,full_name=? WHERE user_id=?",
            (username or "", full_name or "", user_id)
        )
        await db.commit()


async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_users() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            return [r[0] for r in await cur.fetchall()]


async def is_premium(user_id: int) -> bool:
    u = await get_user(user_id)
    return bool(u) and bool(u["is_premium"]) and u["premium_expiry"] > time.time()


async def check_and_consume(user_id: int, category: str) -> tuple[bool, int, int]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # Auto-reset monthly usage
        async with conn.execute("SELECT last_reset FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row and (time.time() - row[0]) > 30 * 86400:
            await conn.execute(
                "UPDATE users SET media_usage=0,poster_usage=0,last_reset=? WHERE user_id=?",
                (int(time.time()), user_id)
            )
            await conn.commit()

        async with conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            u = dict(await cur.fetchone())

        premium   = bool(u["is_premium"]) and u["premium_expiry"] > time.time()
        field     = f"{category}_usage"
        used      = u[field]
        limit     = FREE_MEDIA_LIMIT if category == "media" else FREE_POSTER_LIMIT

        if premium:
            await conn.execute(f"UPDATE users SET {field}={field}+1 WHERE user_id=?", (user_id,))
            await conn.commit()
            return True, used + 1, 999

        if used >= limit:
            return False, used, limit

        await conn.execute(f"UPDATE users SET {field}={field}+1 WHERE user_id=?", (user_id,))
        await conn.commit()
        return True, used + 1, limit


async def grant_premium(user_id: int, days: int = PREMIUM_DAYS):
    expiry = int(time.time()) + days * 86400
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_premium=1,premium_expiry=? WHERE user_id=?", (expiry, user_id)
        )
        await db.execute("DELETE FROM pending_premium WHERE user_id=?", (user_id,))
        await db.commit()


async def revoke_premium(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_premium=0,premium_expiry=0 WHERE user_id=?", (user_id,))
        await db.commit()


async def add_pending(user_id: int, utr: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_premium VALUES (?,?,?)",
            (user_id, utr, int(time.time()))
        )
        await db.commit()


async def get_pending() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM pending_premium ORDER BY requested_at") as cur:
            return [dict(r) for r in await cur.fetchall()]
