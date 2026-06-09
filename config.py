import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN       = os.getenv("BOT_TOKEN", "")
API_ID          = int(os.getenv("API_ID", "0") or "0")
API_HASH        = os.getenv("API_HASH", "")
TMDB_API_KEY    = os.getenv("TMDB_API_KEY", "")

# JustWatch Content Partner API token (from your contract with JustWatch)
# Endpoint: https://apis.justwatch.com/contentpartner/v2/content/...?token=TOKEN
JUSTWATCH_TOKEN = os.getenv("JUSTWATCH_TOKEN", "")

STREAM_BASE_URL = os.getenv("STREAM_BASE_URL", "").rstrip("/")
STREAM_PORT     = int(os.getenv("STREAM_PORT", "8080"))

ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
BOT_USERNAME    = os.getenv("BOT_USERNAME", "YourBot").lstrip("@")

# MongoDB
MONGO_URI       = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB        = os.getenv("MONGO_DB",  "multipurpose_bot")

# Channels (use integer IDs like -1001234567890)
# LOG_CHANNEL  — all bot activity is sent here with user info
# DUMP_CHANNEL — files are stored here for permanent /link generation
LOG_CHANNEL     = int(os.getenv("LOG_CHANNEL",  "0") or "0")
DUMP_CHANNEL    = int(os.getenv("DUMP_CHANNEL", "0") or "0")

# Premium / billing
PREMIUM_PRICE   = os.getenv("PREMIUM_PRICE", "35")
PREMIUM_DAYS    = int(os.getenv("PREMIUM_DAYS", "30"))
UPI_ID          = os.getenv("UPI_ID", "")

# Free usage limits (per 30 days)
FREE_MEDIA_LIMIT  = int(os.getenv("FREE_MEDIA_LIMIT",  "20"))
FREE_POSTER_LIMIT = int(os.getenv("FREE_POSTER_LIMIT", "10"))

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p/w500"
