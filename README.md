# 🇺🇿🇰🇷 Korea Ish E'lonlari — Telegram Job Aggregator Bot

> A production-grade Telegram userbot that monitors 50+ Uzbek community groups in South Korea in real time, filters job announcements using multi-language keyword detection and AI-powered halal screening, and instantly forwards them to a single target group — serving **2,300+ active users**.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org)
[![Telethon](https://img.shields.io/badge/Telethon-1.36.0-blue)](https://docs.telethon.dev)
[![aiogram](https://img.shields.io/badge/aiogram-3.13.0-blue)](https://aiogram.dev)
[![Deployed on Railway](https://img.shields.io/badge/Deployed%20on-Railway-blueviolet?logo=railway)](https://railway.app)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## Table of Contents

- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Features](#features)
- [Project Structure](#project-structure)
- [Environment Variables](#environment-variables)
- [Setup Guide](#setup-guide)
- [Deployment on Railway](#deployment-on-railway)
- [Admin Bot Commands](#admin-bot-commands)
- [Keyword Detection System](#keyword-detection-system)
- [AI Halal Filter](#ai-halal-filter)
- [Odam Olindi Feature](#odam-olindi-feature)
- [Message Format](#message-format)
- [FAQ & Troubleshooting](#faq--troubleshooting)
- [Contributing](#contributing)

---

## How It Works

The bot uses **two independent detection channels** to guarantee no job post is ever missed:

```
  Telegram Server
       │
       ▼
┌──────────────────────────────────────────────────┐
│  Dual Telethon Userbot (Account 1 + Account 2)  │
│  Dedicated accounts — source groups only         │
└────────────────────┬─────────────────────────────┘
                     │
          ┌──────────┴──────────┐
          │                     │
          ▼                     ▼
  ┌──────────────┐    ┌──────────────────────┐
  │   Event      │    │  Backup Polling      │
  │   Handler    │    │  Loop (every 15s)    │
  │ (real-time)  │    │ (catches MTProto     │
  │   ~1-3s      │    │  delivery gaps)      │
  └──────┬───────┘    └─────────┬────────────┘
         └──────────┬───────────┘
                    ▼
      ┌─────────────────────────────┐
      │     9-Gate Filter Pipeline  │
      │  1.   Source group gate     │
      │  2.   Text extraction       │
      │  2.5  Blocked users         │
      │  3.   Repost dedup          │
      │  4.   Processed dedup       │
      │  5.   Keyword filter        │
      │  5.1  Group metadata        │
      │  5.2  Keyword halal filter  │
      │  5.3  Sender resolve        │
      │  5.5  Content dedup         │
      └────────────┬────────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
        ▼                     ▼
  ┌───────────┐     ┌──────────────────┐
  │  Forward  │     │ Background Groq  │
  │  to       │     │ AI Halal Check   │
  │  Target   │     │ (async task)     │
  │  Group    │     │ haram → delete   │
  └───────────┘     └──────────────────┘
```

**Key design principle:** The event handler forwards immediately (~1-3s). The Groq AI halal check runs *after* the message is already posted — if flagged as haram, the post is deleted and sent to admin review. This guarantees real-time forwarding without blocking on AI inference.

---

## Architecture

### Dual Account System

| Account | Role | Groups |
|---------|------|--------|
| Account 1 | Primary — monitors majority of source groups | ~75% of groups |
| Account 2 | Backup/secondary — handles remaining groups | ~25% of groups |

Both accounts run concurrently via `asyncio.gather`. If Account 1 disconnects, Account 2 continues uninterrupted.

### Two-Channel Detection

| Channel | Mechanism | Latency |
|---------|-----------|---------|
| Event handler | Telethon `NewMessage` event, `create_task` for concurrency | 1–3 seconds |
| Backup polling | `get_messages()` called every 15s, all groups parallel via `asyncio.gather` | ≤ 15 seconds |

The polling loop exists because Telegram's MTProto push mechanism can silently delay updates by several minutes during server load peaks. The polling guarantees no job post is missed regardless of push delivery reliability.

### Concurrency Model

```python
async def _on_new_message(event):
    asyncio.create_task(_process_message(event))  # returns immediately
```

Every incoming message spawns an independent `asyncio.Task`. 15 messages arriving simultaneously are processed in parallel — no message waits for another.

---

## Features

| # | Feature | Details |
|---|---------|---------|
| 1 | **Dual Telethon userbot** | Two accounts run concurrently for redundancy and load distribution |
| 2 | **Real-time event + polling backup** | MTProto push for speed; `get_messages` polling every 15s for reliability |
| 3 | **Multi-language keyword detection** | Uzbek, Russian, Korean — two-tier: strong keywords + word combinations |
| 4 | **AI halal screening** | Groq `llama-3.3-70b-versatile` checks each post asynchronously in background |
| 5 | **Admin-toggleable AI filter** | Turn Groq on/off instantly from admin bot without restart |
| 6 | **Human review queue** | Flagged posts sent to admin with Approve/Reject inline buttons |
| 7 | **Odam Olindi detection** | Marks target post as "🔴 ODAM OLINDI" when reply/edit signals position is filled |
| 8 | **Content deduplication** | Two-tier: exact SHA-256 hash + fuzzy SequenceMatcher (0.85 threshold, 24h window) |
| 9 | **Repost deduplication** | Skips cross-group forwards of already-seen messages |
| 10 | **`chats=` filter** | Handlers registered with `chats=source_ids` — personal groups never reach the handler |
| 11 | **KST timestamps** | All logs and forwarded posts display Korea Standard Time (UTC+9) |
| 12 | **Rate limiter** | Minimum 3.1s gap between sends to prevent Telegram `FloodWait` |
| 13 | **FloodWait outside-lock handling** | Other sends wait for FloodWait expiry *outside* the lock (no pile-up delay) |
| 14 | **Auto-delete service messages** | Join/leave events in the target group are automatically deleted |
| 15 | **Admin control panel** | Full Telegram bot UI — add/remove groups, stats, test tools, block users |
| 16 | **Daily KST summary** | Automated 9 PM KST report with forwarding statistics |
| 17 | **Railway-ready** | Auto-detects Railway environment; persists DB on attached volume at `/data` |
| 18 | **2-hour staleness filter** | Polling only forwards messages newer than 2 hours |

---

## Project Structure

```
telegram_job_bot/
│
├── bot/
│   ├── __init__.py
│   ├── admin_bot.py       # aiogram admin control panel
│   ├── client.py          # Telethon client factory (dual account)
│   ├── config.py          # Environment variable loader + validation
│   ├── database.py        # SQLite: all tables, queries, caching layer
│   ├── filters.py         # Multi-language keyword detection engine
│   ├── groq_halal.py      # Groq AI halal verdict (async, with semaphore)
│   ├── halal_filter.py    # Keyword-based halal pre-filter (no API call)
│   ├── monitor.py         # Core: event handler + polling loop + pipeline
│   ├── notifier.py        # Admin notification helper
│   ├── scheduler.py       # Daily 9 PM KST summary scheduler
│   └── utils.py           # Logging (KST), HTML formatting, rate limiter
│
├── data/                  # SQLite database (auto-created)
├── logs/                  # Log files (auto-created)
├── sessions/              # Telethon session files (auto-created)
│
├── main.py                # ⭐ Unified entry point (recommended)
├── main_monitor.py        # Standalone monitor only
├── main_admin.py          # Standalone admin bot only
├── generate_session.py    # Interactive session string generator
├── fix_chat_ids.py        # One-time repair for malformed group IDs
│
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in all required values:

```bash
cp .env.example .env
```

### Required

| Variable | Description | Example |
|----------|-------------|---------|
| `API_ID` | Telegram API app ID from [my.telegram.org](https://my.telegram.org/apps) | `12345678` |
| `API_HASH` | Telegram API app hash | `abc123def456...` |
| `PHONE_NUMBER` | Phone number of the monitoring account | `+998901234567` |
| `BOT_TOKEN` | Admin bot token from @BotFather | `123456789:ABCdef...` |
| `ADMIN_USER_ID` | Your Telegram numeric user ID | `987654321` |

### Optional

| Variable | Description | Default |
|----------|-------------|---------|
| `SESSION_STRING` | Telethon string session for Account 1 (required for Railway) | — |
| `PHONE_NUMBER_2` | Phone number for Account 2 | — |
| `SESSION_STRING_2` | Telethon string session for Account 2 | — |
| `TARGET_GROUP` | Target group username or numeric ID | — |
| `GROQ_API_KEY` | Groq API key for AI halal filter | — |
| `DATABASE_PATH` | Path to SQLite database file | `data/job_bot.db` |
| `DEDUP_WINDOW_HOURS` | Hours to remember forwarded posts for dedup | `24` |
| `DEDUP_SIMILARITY_THRESHOLD` | Fuzzy match threshold (0.0–1.0) | `0.85` |
| `LOG_LEVEL` | Python logging level | `INFO` |

> **Railway:** `DATABASE_PATH` defaults to `/data/job_bot.db` automatically when `RAILWAY_ENVIRONMENT` is detected. Attach a Railway Volume at `/data` to persist the database across deployments.

---

## Setup Guide

### 1. Get Telegram API credentials

1. Go to [https://my.telegram.org/apps](https://my.telegram.org/apps)
2. Log in and click **Create application**
3. Copy `api_id` and `api_hash`

### 2. Create the admin bot

1. Message **@BotFather** on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 3. Generate a session string (for cloud/Railway deployment)

```bash
python generate_session.py
```

Choose account number, log in with your phone, and copy the printed `SESSION_STRING` into your `.env` or Railway environment variables.

> ⚠️ **Important:** Use a **dedicated Telegram account** for the monitoring account — add it to source groups only (no personal chats, channels, or bots). This prevents Telegram's update delivery queue from backing up with irrelevant updates, which is the primary cause of forwarding delays.

### 4. Install dependencies

```bash
python -m venv venv
source venv/bin/activate   # Linux/macOS
venv\Scripts\activate      # Windows

pip install -r requirements.txt
```

### 5. Configure `.env`

```env
API_ID=12345678
API_HASH=your_api_hash_here
PHONE_NUMBER=+998901234567
SESSION_STRING=1BVts...          # from generate_session.py
BOT_TOKEN=123456789:ABCdef...
ADMIN_USER_ID=987654321
GROQ_API_KEY=gsk_...             # optional
```

### 6. Run

```bash
python main.py
```

On first run without a session string, Telegram sends a verification code to your phone — enter it in the terminal.

---

## Deployment on Railway

### Steps

1. Push your code to GitHub (`.env` is in `.gitignore`)
2. Create a new Railway project → **Deploy from GitHub repo**
3. Add a **Volume** mounted at `/data` (persists the SQLite database)
4. Set all environment variables in Railway's **Variables** tab
5. Deploy

### Minimum Railway variables

```
API_ID
API_HASH
PHONE_NUMBER
SESSION_STRING       ← from generate_session.py
BOT_TOKEN
ADMIN_USER_ID
GROQ_API_KEY         ← optional, for AI halal filter
```

`DATABASE_PATH` does **not** need to be set — it automatically defaults to `/data/job_bot.db` on Railway.

---

## Admin Bot Commands

All commands are accessible via keyboard buttons. The bot only responds to `ADMIN_USER_ID`.

| Button | Description |
|--------|-------------|
| 📋 List Groups | Show all monitored source groups with ID, account assignment, and date added |
| ➕ Add Group | Add a group by numeric ID or @username |
| ➖ Remove Group | Tap-to-remove inline keyboard |
| 🎯 Set Target | Set the destination group |
| 📊 Status | Monitor status, group counts, total forwarded |
| 📈 Stats | 7-day analytics: top groups, language breakdown, detection method |
| 🧪 Test Send | Send a test post to the target group |
| 🔍 Test Keyword | Paste any text to see if the filter detects it as a job post |
| 🕌 Test Halal | Check if a message would be flagged by the keyword halal filter |
| 🚫 Blocked Users | View and manage blocked sender IDs |
| 🤖 AI Filter | Toggle Groq AI halal filter on/off (persists across restarts) |
| 🔎 Check Groups | Verify the monitor account is a member of all source groups |
| 📊 Clear Stats | Reset forwarding statistics (with confirmation) |

---

## Keyword Detection System

The filter uses a **two-tier system** supporting Uzbek, Russian, and Korean:

### Tier 1 — Strong keywords (single match = job post)

| Language | Examples |
|----------|----------|
| 🇺🇿 Uzbek | `ish bor`, `ishchi kerak`, `vakansiya`, `arbayt`, `ishga olinadi`, `oylik`, `ish haqi` |
| 🇷🇺 Russian | `вакансия`, `нужен работник`, `требуется`, `подработка`, `ищем сотрудниц`, `срочно нужны` |
| 🇰🇷 Korean | `알바`, `구인`, `채용`, `시급`, `월급`, `구합니다`, `모집` |

### Tier 2 — Word combinations (both words must appear in message)

| Combination | Example caught |
|-------------|----------------|
| `kerak` + person noun | `"1 yigit kerak bugun zavod"` |
| `ish` + location word | `"zavod uchun ish bor Kimhae"` |
| `работа` + context word | `"срочно работа есть склад Инчхон"` |

To add a keyword: open `bot/filters.py`, add to the appropriate list in `STRONG_KEYWORDS`, and restart.

---

## AI Halal Filter

The bot integrates **Groq API** (`llama-3.3-70b-versatile`) for an additional halal screening layer.

### How it works

1. Job post is forwarded to the target group **immediately** (no blocking)
2. A background `asyncio.Task` sends the post text to Groq
3. If verdict is `haram` or `unclear`: post is **deleted** from target group and sent to admin review queue
4. Admin sees the post with **Approve / Reject** inline buttons
5. If approved: post is re-sent to target group in full format

### Keyword pre-filter

Before Groq is called, a fast keyword-based halal filter screens obvious cases (alcohol, gambling, haram restaurant names, etc.) without any API call. Only messages that pass this filter proceed to Groq.

### Toggling

The AI filter can be toggled on/off from the admin bot (`🤖 AI Filter` button) without restarting. The state persists in the database.

---

## Odam Olindi Feature

When a job position is filled, the bot automatically marks the forwarded post in the target group.

**Triggers (any of these):**

1. Someone replies to the original job post with text containing a "filled" signal (e.g. *"odam olindi"*, *"band"*, *"взяли"*, *"채용완료"*)
2. The job poster edits their message to include a filled signal
3. The job poster sends a new message (without reply) with a filled signal — the bot finds their most recent forwarded post from that group

**Result:** The forwarded post in the target group is edited to append:

```
─────────────────
🔴 ODAM OLINDI
```

---

## Message Format

Every forwarded job post:

```
⚠️ Yangi ish e'loni:

Guruh:   [Group Name](https://t.me/groupusername)
Muallif: [Author Name](tg://user?id=123456789)
Vaqt:    2026-04-25 14:30:00 KST

Xabar matni:
Assalomu alaykum, zavod uchun 3 nafar ishchi kerak...
```

- Group and author names are **clickable hyperlinks**
- Time is always in **Korea Standard Time (KST, UTC+9)**
- No Telegram link preview cards
- All text is HTML-escaped to prevent formatting issues

---

## FAQ & Troubleshooting

**Q: The bot starts but no messages are forwarded.**
1. Check logs for `✅ JOB DETECTED` — if absent, messages lack job keywords
2. Use **🔍 Test Keyword** in the admin bot to verify
3. If no `SOURCE GROUP HIT` in logs — the monitor account is not in the source groups
4. Use **🔎 Check Groups** to find which groups the account hasn't joined

**Q: Messages are being forwarded late (5–10 minutes delay).**
This is caused by Telegram's MTProto push delivery delay — not the bot code. The backup polling loop (every 15s) catches these. Check logs for `delivery_lag=` values on `SOURCE GROUP HIT` lines. If `delivery_lag > 60`, the delay is in Telegram's infrastructure.

**Q: The same job post appears twice.**
Ensure both accounts use the same SQLite database file (they do by default). Check `DEDUP_WINDOW_HOURS` is not set to 0.

**Q: `UserWarning: the session already had an authorized user`.**
The `SESSION_STRING` still contains the old account's session. Run `generate_session.py` again with the new account and replace `SESSION_STRING`.

**Q: `UnboundLocalError: cannot access local variable 'groups'`.**
Upgrade to the latest `monitor.py` — this bug was fixed.

**Q: How do I add a new source group?**
Use **➕ Add Group** in the admin bot. The bot automatically checks if the monitor account is a member and warns you if not.

**Q: Can I run this without the Groq API key?**
Yes. Leave `GROQ_API_KEY` empty. Keyword-based filters still run. Only the AI halal background check is skipped.

**Q: Can I run this on a Linux VPS instead of Railway?**
Yes. Create a systemd service pointing to `python main.py`. Any cloud provider works (DigitalOcean, Hetzner, AWS, etc.).

---

## Security Notes

- **Never commit `.env`** — it contains your personal Telegram credentials
- **`sessions/` folder** contains Telethon session files — treat them like passwords (already in `.gitignore`)
- The admin bot only responds to `ADMIN_USER_ID` — all other users are silently rejected
- `data/` (database) and `logs/` are excluded from git

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `telethon` | 1.36.0 | Telegram MTProto client (userbot) |
| `aiogram` | 3.13.0 | Telegram Bot API framework (admin panel) |
| `python-dotenv` | 1.0.1 | `.env` file loader |
| `aiohttp` | 3.10.10 | Async HTTP (used by aiogram) |
| `qrcode` | 7.4.2 | QR login in `generate_session.py` |

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first.

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit: `git commit -m 'Add your feature'`
4. Push: `git push origin feature/your-feature`
5. Open a Pull Request

---

*Built for the Uzbek community in South Korea 🇺🇿🇰🇷*