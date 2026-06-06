# Multipurpose Telegram Bot

A clean, feature-rich Telegram bot for small groups and friends.

---

## Commands

### 🎬 Movie & OTT
| Command | Description |
|---|---|
| `/imdb <title>` | Movie/Series info — rating, cast, genres, poster |
| `/ott <title>` | OTT streaming availability via JustWatch |
| `/posters <title>` | Browse movie posters with pagination |

### 🎞 Video Tools
| Command | Description |
|---|---|
| `/mediainfo` | Codec, resolution, bitrate, audio info _(reply to video)_ |
| `/sample` | Generate 30-second preview clip _(reply to video)_ |
| `/screenshot` | Take 10 evenly-spaced screenshots _(reply to video)_ |
| `/yt <url>` | YouTube quality picker → direct download link (no upload) |

### 📊 Account
| Command | Description |
|---|---|
| `/start` | Welcome message + command list |
| `/help` | Full command reference |
| `/usage` | Your monthly usage stats |
| `/premium` | Upgrade to Premium (unlimited access) |

### 🔐 Admin Only
| Command | Description |
|---|---|
| `/restart` | Restart the bot |
| `/addpremium <user_id> [days]` | Grant premium to a user |
| `/removepremium <user_id>` | Revoke premium |
| `/pending` | View pending payment verifications |
| `/stats` | Total users & premium count |
| `/broadcast <message>` | Send message to all users |

---

## Setup

### 1. Prerequisites
- Python 3.11+
- FFmpeg: `sudo apt install ffmpeg`
- Bot token from [@BotFather](https://t.me/BotFather)
- Free TMDB API key from [themoviedb.org](https://www.themoviedb.org/settings/api)

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure
```bash
cp .env.example .env
nano .env
```

Fill in:
```env
BOT_TOKEN=your_token_here
TMDB_API_KEY=your_tmdb_key_here
ADMIN_IDS=your_telegram_id
UPI_ID=yourname@upi
```

### 4. Run
```bash
python bot.py
```

### 5. Run as systemd service (keep alive 24/7)
```bash
sudo nano /etc/systemd/system/tgbot.service
```
```ini
[Unit]
Description=Multipurpose Telegram Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/multipurpose_bot
ExecStart=/usr/bin/python3 /home/ubuntu/multipurpose_bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable tgbot
sudo systemctl start tgbot
sudo systemctl status tgbot
```

---

## Premium System

1. User sends `/premium` — sees price + UPI ID
2. User pays and taps **Verify**
3. User sends their UTR/Transaction ID
4. Admin runs `/pending` to see all requests
5. Admin runs `/addpremium <user_id>` to activate
6. User gets notified automatically

---

## Usage Limits (Free Tier)

| Feature | Free | Premium |
|---|---|---|
| Video tools (mediainfo/sample/screenshot) | 20/month | Unlimited |
| Poster searches | 10/month | Unlimited |
| /yt, /imdb, /ott | Unlimited | Unlimited |

---

## How /yt Works

```
User: /yt https://youtube.com/watch?v=xxx
         ↓
Bot fetches video info (yt-dlp, no download)
         ↓
Sends thumbnail + quality buttons:
  [🎥 1080p]  [🎥 720p]
  [🎥 480p]   [🎥 360p]
  [🎵 MP3 Audio]
         ↓
User taps quality
         ↓
Bot calls cobalt.tools API → gets direct link
         ↓
Sends: ⬇️ Tap here to download
```
The bot never downloads or stores the video. The link goes directly to the user.

---

## File Structure

```
multipurpose_bot/
├── bot.py              ← Entry point
├── config.py           ← Settings (reads .env)
├── database.py         ← SQLite: users, usage, premium
├── handlers/
│   ├── movie.py        ← /imdb /ott /posters
│   ├── media.py        ← /mediainfo /sample /screenshot
│   ├── ytdl.py         ← /yt
│   ├── user.py         ← /start /help /usage /premium
│   └── admin.py        ← admin commands
├── requirements.txt
├── .env.example
└── README.md
```
