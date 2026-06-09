import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
API_ID          = int(os.getenv("API_ID", "0") or "0")
API_HASH        = os.getenv("API_HASH", "")
TMDB_API_KEY    = os.getenv("TMDB_API_KEY", "")


STREAM_BASE_URL = os.getenv("STREAM_BASE_URL", "").rstrip("/")
STREAM_PORT     = int(os.getenv("STREAM_PORT", "8080"))

ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
BOT_USERNAME    = os.getenv("BOT_USERNAME", "YourBot").lstrip("@")

# MongoDB
MONGO_URI       = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB        = os.getenv("MONGO_DB",  "multipurpose_bot")

# Channels — use integer IDs: -1001234567890
# The bot must be ADMIN in both channels.
# LOG_CHANNEL  — all bot activity is logged here
# DUMP_CHANNEL — files forwarded here for permanent /link storage
def _chan(val: str) -> int:
    """Parse a channel ID env var safely. Handles missing -100 prefix."""
    val = (val or "").strip()
    if not val or val == "0":
        return 0
    try:
        n = int(val)
        # Koyeb sometimes strips the minus — restore it for supergroups/channels
        if n > 0 and n > 1_000_000_000:
            n = -int(f"100{n}")
        return n
    except ValueError:
        return 0

LOG_CHANNEL     = _chan(os.getenv("LOG_CHANNEL",  "0"))
DUMP_CHANNEL    = _chan(os.getenv("DUMP_CHANNEL", "0"))

# Premium / billing
PREMIUM_PRICE   = os.getenv("PREMIUM_PRICE", "35")
PREMIUM_DAYS    = int(os.getenv("PREMIUM_DAYS", "30"))
UPI_ID          = os.getenv("UPI_ID", "")

# Free usage limits (per 30 days)
FREE_MEDIA_LIMIT  = int(os.getenv("FREE_MEDIA_LIMIT",  "20"))
FREE_POSTER_LIMIT = int(os.getenv("FREE_POSTER_LIMIT", "10"))

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p/w500"
