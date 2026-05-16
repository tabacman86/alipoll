# AliExpress Order Tracker — Telegram Bot

A self-hosted Telegram bot that monitors your AliExpress orders and pushes notifications when statuses change (e.g. Shipped → In Transit → Package delivered).

AliExpress has no public order API, so the bot uses Playwright to scrape the orders page with a saved browser session.

## Features

- Background polling on a configurable interval (default: every 4 hours)
- Push notifications on status changes — only changed orders trigger a message
- Granular sub-status from the order detail page (e.g. "Package delivered.", "Customs clearance in progress.")
- Item name translation via Google Translate (configurable target language)
- Multi-recipient support — filter or group orders by recipient name
- Multiple Telegram chat IDs — notify several users simultaneously
- SQLite-backed change detection
- Docker-first deployment with noVNC for headless server login

## Telegram Commands

| Command | Description |
|---|---|
| `/update` | Fetch latest orders from AliExpress, save to DB, report changes |
| `/active` | Active orders — choose grouping: by shipping stage or by recipient |
| `/active <name>` | Active orders filtered to a specific recipient |
| `/cached` | All orders from local DB — choose sort: by status or by recipient |
| `/cached <name>` | All orders filtered to a specific recipient |
| `/status` | Bot status: last poll, next poll, order count |
| `/login` | Re-authentication instructions |

Commands are auto-registered in the Telegram menu on every startup.

## Quick Start (Docker — recommended)

### 1. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```ini
TELEGRAM_BOT_TOKEN=7123456789:AAF_your_token_here
TELEGRAM_CHAT_ID=123456789,987654321   # comma-separated for multiple users
POLL_INTERVAL_HOURS=4
LOG_LEVEL=INFO
TRANSLATE_LANG=iw                      # target language for item name translation
```

### 2. First-time login

The bot needs a one-time browser login to save your AliExpress session cookies. Use the built-in noVNC mode — no X server or VNC client needed, just a browser.

```bash
# Start the noVNC login session
docker compose run --rm -p 6080:6080 aliexpress-bot python main.py --novnc
```

Then in a second terminal on your local machine:

```bash
ssh -L 6080:localhost:6080 user@your-server
```

Open `http://localhost:6080/vnc.html?autoconnect=1&resize=scale` in your browser. Complete the AliExpress login. Cookies are saved automatically when you reach the orders page.

### 3. Run

```bash
docker compose up -d
```

Send `/update` in Telegram to do the first full sync.

---

## Running Without Docker

### Requirements

- Python 3.12+
- Chromium system libraries (see `playwright install-deps chromium`)
- Xvfb + x11vnc + novnc (for `--novnc` login on headless servers)

### Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

### First-time login

**Option A — noVNC (headless server)**

```bash
python main.py --novnc
# SSH tunnel: ssh -L 6080:localhost:6080 user@server
# Open: http://localhost:6080/vnc.html?autoconnect=1&resize=scale
```

**Option B — X11 forwarding**

```bash
ssh -X user@server
python main.py --login
```

**Option C — login locally, copy cookies**

```bash
# On your local machine
python main.py --login
scp data/cookies.json user@server:/path/to/aliexpress/data/
```

### Run

```bash
python main.py
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | BotFather token |
| `TELEGRAM_CHAT_ID` | — | Chat ID(s), comma-separated |
| `POLL_INTERVAL_HOURS` | `4` | Background poll frequency |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `TRANSLATE_LANG` | `iw` | Google Translate target language code |

## Project Structure

```
├── main.py                  # Entry point (--login / --novnc / bot mode)
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── bot/
│   ├── handlers.py          # Telegram command handlers + FSM
│   ├── notifications.py     # Message formatting
│   └── translator.py        # Item name translation + status labels
├── scraper/
│   ├── browser.py           # Playwright session management + noVNC login
│   ├── aliexpress.py        # Order scraping (DOM) + detail page enrichment
│   └── models.py            # Order dataclass, status groups, priority
├── state/
│   └── store.py             # SQLite store, upsert, change detection
└── scheduler/
    └── polling.py           # Background polling with APScheduler
```

## Session Expiry

When the session expires, the bot sends a Telegram notification to all configured chat IDs. Re-run the login flow:

```bash
# Docker
docker compose stop aliexpress-bot
docker compose run --rm -p 6080:6080 aliexpress-bot python main.py --novnc
docker compose up -d aliexpress-bot

# Without Docker
python main.py --novnc
```

## Notes

- Keep `POLL_INTERVAL_HOURS` at 2 or higher to avoid Cloudflare blocks.
- AliExpress renders 10 orders initially; the scraper clicks "View orders" to load all pages.
- `data/cookies.json` is chmod 600 and gitignored — never commit it.
- Item names are translated and cached in SQLite to avoid repeated API calls.

## License

MIT
