import os
from dotenv import load_dotenv

load_dotenv()

# ── Required ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID    = int(os.getenv("API_ID",   "0") or "0")
API_HASH  = os.getenv("API_HASH",  "")

# ── Optional APIs ─────────────────────────────────────────────────────────────
TMDB_API_KEY   = os.getenv("TMDB_API_KEY",  "")
JUSTWATCH_API  = os.getenv("JUSTWATCH_API", "")

# ── Admin ─────────────────────────────────────────────────────────────────────
ADMIN_IDS    = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
BOT_USERNAME = os.getenv("BOT_USERNAME", "YourBot")

# ── Premium ───────────────────────────────────────────────────────────────────
PREMIUM_PRICE = os.getenv("PREMIUM_PRICE", "35")
PREMIUM_DAYS  = int(os.getenv("PREMIUM_DAYS", "30"))
UPI_ID        = os.getenv("UPI_ID", "")

# ── Free usage limits (per 30 days) ───────────────────────────────────────────
FREE_MEDIA_LIMIT  = int(os.getenv("FREE_MEDIA_LIMIT",  "20"))
FREE_POSTER_LIMIT = int(os.getenv("FREE_POSTER_LIMIT", "10"))

# ── Streaming server (/link command) ─────────────────────────────────────────
STREAM_PORT     = int(os.getenv("STREAM_PORT", "8080"))
STREAM_BASE_URL = os.getenv("STREAM_BASE_URL", "").rstrip("/")

# ── Internals ─────────────────────────────────────────────────────────────────
DB_PATH   = os.getenv("DB_PATH", "bot.db")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p/w500"
