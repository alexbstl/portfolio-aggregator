# Portfolio Aggregator

Aggregates brokerage accounts and positions via [SnapTrade](https://snaptrade.com/) into a local SQLite database, served through a FastAPI dashboard.

## Features

- **Multi-brokerage support** — any broker SnapTrade connects to (Alpaca, Robinhood, Fidelity, etc.)
- **Auto-sync** every 15 minutes via APScheduler, plus manual force-refresh
- **Daily change tracking** — a 9:29 ET pre-market snapshot captures reference prices; day change $ and % are computed at read time
- **Historical snapshots** — position and account-level snapshots stored for equity curve analysis
- **Paper/real separation** — separate dashboard views at `/` and `/paper`

## Quickstart

```bash
# 1. Set up env
cp .env.example .env   # fill in SNAPTRADE_CLIENT_ID, SNAPTRADE_CONSUMER_KEY, SNAPTRADE_USER_ID, SNAPTRADE_USER_SECRET

# 2. Install deps
pip install -r requirements.txt

# 3. Run
uvicorn app.main:app --reload
```

Dashboard at `http://localhost:8000`.

## Docker

```bash
# Build the image
docker build -t portfolio-aggregator .

# Or use docker compose (builds + runs)
docker compose up -d --build

# View logs
docker compose logs -f

# Restart after code changes
docker compose up -d --build --force-recreate
```

Data persists in `./data/portfolio.db` (mounted as a volume). Binds to `127.0.0.1:8000` only — put behind Caddy/Tailscale/nginx for remote access.

The container includes a health check at `/health` (30s interval).

## API

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/api/accounts` | GET | All accounts with brokerage info |
| `/api/positions?paper=false` | GET | Positions with day change data |
| `/api/history` | GET | Account value snapshots |
| `/api/sync` | POST | Trigger manual sync |

## Project Structure

```
app/main.py          # FastAPI app, scheduler, routes
app/templates/       # Jinja2 dashboard template
db.py                # SQLite schema, helpers, queries
sync_once.py         # SnapTrade sync logic
register.py          # One-time: register SnapTrade user
connect.py           # One-time: generate connection link
```
