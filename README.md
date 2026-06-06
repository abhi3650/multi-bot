# Multipurpose Telegram Bot (Pyrogram)

A fully-featured Telegram bot built on **Pyrogram** (MTProto) — no Bot API file-size limits.

---

## Features

| Command | Description |
|---|---|
| `/imdb <title>` | Movie/series info from TMDB + IMDB |
| `/ott <title>` | OTT streaming availability (JustWatch → TMDB fallback) |
| `/posters <title>` | Browse movie posters with navigation |
| `/mediainfo` | Codec, resolution, bitrate — reply to video or pass URL |
| `/sample` | Generate a 30-second preview clip |
| `/screenshot` | Take 10 screenshots from a video |
| `/link` | Generate Watch Online + Download link (any file size) |
| `/yt <url>` | YouTube quality picker + direct download link |
| `/song <name>` | Search YouTube Music → pick from 8 results → get MP3 with album art |
| `/usage` | View your monthly usage |
| `/premium` | Upgrade for unlimited access |

---

## Setup

### 1. Clone / extract the project

```bash
cd multipurpose_bot
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> Also install system dependencies:
> ```bash
> apt install ffmpeg mediainfo   # Debian/Ubuntu
> ```

### 3. Configure `.env`

```bash
cp .env.example .env
nano .env
```

**Required fields:**

| Key | Where to get |
|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `API_ID` | [my.telegram.org/apps](https://my.telegram.org/apps) |
| `API_HASH` | Same as above |
| `TMDB_API_KEY` | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) |

**Optional:**

| Key | Purpose |
|---|---|
| `STREAM_BASE_URL` | Public URL for `/link` watch/download pages (e.g. `http://YOUR_IP:8080`) |
| `JUSTWATCH_API` | JustWatch API key — `/ott` uses JustWatch first, falls back to TMDB |
| `UPI_ID` | Your UPI ID for premium payments |

### 4. Run

```bash
python bot.py
```

---

## Large File Support

With `API_ID` + `API_HASH` configured, the bot uses **Pyrogram's MTProto** to:
- Download files of any size (up to 4 GB) for `/mediainfo`, `/sample`, `/screenshot`
- Stream files of any size via `/link` using the built-in aiohttp server

Without these credentials, file operations are limited to **20 MB** (Telegram Bot API cap).

---

## Admin Commands

| Command | Description |
|---|---|
| `/addpremium <uid> [days]` | Grant premium to a user |
| `/removepremium <uid>` | Revoke premium |
| `/pending` | View pending payment verifications |
| `/stats` | Bot usage statistics |
| `/broadcast <text>` | Send message to all users |
| `/restart` | Restart the bot process |

Set `ADMIN_IDS=your_telegram_id` in `.env`.
