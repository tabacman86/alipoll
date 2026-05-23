# AGENT.md — AliExpress Order Tracker Bot

Everything a new developer or AI agent needs to work on this codebase.

---

## What This Is

A self-hosted Telegram bot that monitors AliExpress orders and sends push notifications when statuses change. AliExpress has no public API, so the bot uses Playwright to scrape the orders page with a saved browser session.

Deployed as a Docker container. Re-authentication is done via `/relogin` in Telegram (starts an in-container noVNC session the user accesses over SSH tunnel).

---

## Project Structure

```
main.py                   # Entry point — asyncio.run(run_bot()) only
Dockerfile
docker-compose.yml        # Project-level compose (for standalone use)
/opt/docker_home/docker-compose.yml  # Server-wide compose (production)
requirements.txt
.env                      # Never committed — see .env.example

bot/
  handlers.py             # All Telegram command handlers + FSM + middleware
  notifications.py        # Message formatting (HTML, Hebrew)
  translator.py           # Google Translate wrapper + status label map

scraper/
  browser.py              # BrowserManager, NoVNCStack, cookie management
  aliexpress.py           # JS-injected order scraping (no BeautifulSoup)
  models.py               # Order dataclass, status groups, priority

state/
  store.py                # SQLite via aiosqlite — upsert, change detection
scheduler/
  polling.py              # APScheduler wrapper for background polling
```

---

## Architecture

```
asyncio.run(run_bot())
  ├── OrderStateStore.init_db()          SQLite at data/orders.db
  ├── BrowserManager (headless)          Playwright, loads data/cookies.json
  ├── AliExpressScraper(browser_mgr)
  ├── aiogram Bot + Dispatcher
  ├── WhitelistMiddleware                blocks unknown chat IDs
  ├── AppContextMiddleware               injects ctx into all handlers
  ├── PollingScheduler.start()           APScheduler on same event loop
  └── dp.start_polling(bot)              blocks
```

Single asyncio event loop. APScheduler `max_instances=1`. `scrape_lock: asyncio.Lock` prevents concurrent Playwright usage between scheduler and manual `/update`.

---

## Key Files in Detail

### `scraper/aliexpress.py`
**No BeautifulSoup.** Uses `page.evaluate()` with two injected JS functions:
- `_JS_EXTRACT_ORDERS` — runs on the order list page, returns all orders as a JSON array in one call
- `_JS_EXTRACT_DETAIL` — runs on the order detail page, returns `{recipient, subStatus, itemName}`

`fetch_orders(full=False)` — first 10 orders (fast delta poll)
`fetch_orders(full=True)` — clicks "View orders" until all loaded (used on `/update`)
`enrich_recipients(orders, known)` — fetches detail pages only for orders missing recipient/sub_status/item_name

### `scraper/browser.py`
`BrowserManager` — Playwright lifecycle, cookie load/save/reload, session validation
`NoVNCStack` — sync context manager: starts Xvfb + x11vnc + websockify, tears down on exit
`BrowserManager.reload_cookies()` — clears context cookies and reloads from `data/cookies.json` (used after `/relogin`)
`BrowserManager.run_login_flow()` — opens headed browser, detects successful login by URL pattern (`seen_login` flag prevents saving cookies before the login redirect)

### `scraper/models.py`
`Order` dataclass fields: `order_id`, `item_name`, `status`, `order_url`, `order_date`, `tracking_number`, `estimated_delivery`, `seller`, `recipient`, `sub_status`, `completed_at`, `thumbnail_url`, `last_seen`

`STATUS_GROUPS` — maps Hebrew section labels to lists of status strings
`EN_ROUTE_STATUSES` — statuses that trigger change notifications
`STATUS_PRIORITY` — for sorting

### `state/store.py`
`get_active()` — non-cancelled, completed orders within 14 days (for `/active`)
`get_cached()` — non-cancelled, completed/delivered within 30 days by `completed_at` (for `/cached`)
`upsert()` — ON CONFLICT update; preserves `item_name` if new value is "Unknown item"; sets `completed_at` when status first becomes "completed" or "delivered"

### `bot/handlers.py`
`WhitelistMiddleware` — runs first on `dp.update`; drops any chat_id not in `TELEGRAM_CHAT_ID`
`AppContext.relogin_in_progress` — flag to prevent concurrent `/relogin` calls
`RecipientFilter` FSM — `waiting_for_active`, `waiting_for_cached` states for name-filtered views

Commands: `/update`, `/active`, `/cached`, `/status`, `/relogin`

`/active` and `/cached` show inline keyboards (by status / by recipient). "By recipient" triggers FSM waiting for the next text message as the name filter.

### `bot/notifications.py`
`format_order()` — renders one order card (HTML, Hebrew labels). Shows `completed_at` if set.
`format_active_grouped()` — groups by STATUS_GROUPS sections or by recipient
`format_order_list()` — flat list, sortable by recipient
All UI strings are **Hebrew**. Item names are translated via Google Translate (`TRANSLATE_LANG` env var, default `iw`).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | BotFather token |
| `TELEGRAM_CHAT_ID` | — | Comma-separated chat IDs (whitelist) |
| `POLL_INTERVAL_HOURS` | `4` | Background poll frequency |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `TRANSLATE_LANG` | `iw` | Google Translate target language |

---

## Data & Cookies

- `data/cookies.json` — Playwright session cookies, chmod 600, gitignored
- `data/orders.db` — SQLite, gitignored
- Both mounted as a Docker volume from `/opt/aliexpress/data/`

---

## Authentication Flow

**First time or session expired:**
1. Send `/relogin` in Telegram
2. Bot starts Xvfb + x11vnc + websockify inside the running container
3. Bot sends SSH tunnel instructions via Telegram
4. User opens SSH tunnel: `ssh -L 6080:localhost:6080 user@server`
5. User opens `http://localhost:6080/vnc.html?autoconnect=1&resize=scale`
6. User completes AliExpress Google login in the browser
7. Bot detects success (URL change to `/p/order/`), saves cookies, reloads them into the live context, tears down VNC
8. Bot sends "✅ Login successful" message

Session expiry during polling triggers an automatic Telegram notification to all chat IDs.

---

## Docker

```bash
# Build and start
docker compose -f /opt/docker_home/docker-compose.yml up -d --build aliexpress-bot

# Logs
docker logs aliexpress-bot -f

# Restart only (no rebuild)
docker compose -f /opt/docker_home/docker-compose.yml restart aliexpress-bot
```

The project-level `docker-compose.yml` is for standalone use. Production uses `/opt/docker_home/docker-compose.yml`.

Volumes: `/opt/aliexpress/data` → `/app/data`, `/opt/aliexpress/logs` → `/app/logs`

---

## Active Branch: `js-extractor`

Current work is on branch `js-extractor`. Changes vs `main`:
- Replaced BeautifulSoup DOM parsing with `page.evaluate()` JS injection (`_JS_EXTRACT_ORDERS`, `_JS_EXTRACT_DETAIL`)
- Removed `beautifulsoup4` from `requirements.txt`
- Added `/relogin` Telegram command (Docker-native re-auth)
- Removed `--login` / `--novnc` CLI flags from `main.py`
- `NoVNCStack` moved to `scraper/browser.py`

This branch needs `/update` validation before merging to `main`.

---

## Known Patterns & Gotchas

- **AliExpress renders 10 orders initially** — `fetch_orders(full=True)` clicks `.order-more button` to load more pages
- **Some orders have no item rows** — `item_name` falls back to detail page `class="item-title"` with skip filter for non-product strings (`return`, `refund`, `policy`, etc.)
- **"Canceled" spelling** — AliExpress uses one 'l'. Filter uses both `cancelled` and `canceled`
- **`completed_at` vs `last_seen`** — `last_seen` is refreshed every poll. Use `completed_at` to determine how long ago an order was completed/delivered
- **Cookie save timing** — `seen_login` flag in `run_login_flow()` prevents saving cookies before the initial redirect to the login page
- **No DISPLAY in headless Docker** — `/relogin` sets `os.environ["DISPLAY"] = ":20"` after Xvfb starts; the headless bot browser is unaffected
- **Whitelist security** — `WhitelistMiddleware` silently drops any update from chat IDs not in `TELEGRAM_CHAT_ID`. Add new users by updating the env var and restarting
- **Translation cache** — item names translated once and stored in `translations` SQLite table; subsequent fetches use the cache
