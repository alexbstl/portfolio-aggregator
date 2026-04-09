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
# Quick deploy (build + run + health check)
./deploy.sh

# Or manually:
docker compose up -d --build

# View logs
docker compose logs -f

# Restart after code changes
docker compose up -d --build --force-recreate
```

Data persists in `./data/portfolio.db` (mounted as a volume). Binds to `127.0.0.1:8000` only — put behind Caddy/Tailscale/nginx for remote access.

The container includes a health check at `/health` (30s interval).

### Portainer

The container will appear in Portainer automatically if Portainer is monitoring the Docker socket. For full lifecycle management through Portainer, add it as a Stack (Stacks → Add Stack) and point to the `docker-compose.yml`.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/api/accounts` | GET | All accounts with brokerage info |
| `/api/positions?paper=false` | GET | Positions with day change data |
| `/api/history` | GET | Account value snapshots |
| `/api/sync` | POST | Trigger manual sync |

## SnapTrade Setup & Management

These scripts manage the SnapTrade user and broker connections. Run them from the project root with your `.env` configured.

```bash
# 1. Register a SnapTrade user (once). Prints a userSecret — save it to .env.
python register.py

# 2. Generate a connection URL. Open in browser to link a brokerage.
python connect.py

# 3. List all linked connections
python disconnect.py

# 4. Disconnect a specific brokerage (removes from SnapTrade + cleans local DB, preserves snapshots)
python disconnect.py <connection_id>

# 5. Delete the SnapTrade user entirely (invalidates userSecret, removes all connections)
python delete_user.py
```

### Debugging

```bash
# Dump raw JSON from SnapTrade (accounts, positions, balances, options)
python fetch.py

# Force all brokers to re-fetch and show updated sync timestamps
python force_refresh.py

# Check last sync timestamps for all accounts
python check_sync.py
```

## Project Structure

```
app/main.py          # FastAPI app, scheduler, routes
app/templates/       # Jinja2 dashboard template
db.py                # SQLite schema, helpers, queries
sync_once.py         # SnapTrade sync logic
deploy.sh            # Build and run via docker compose
register.py          # One-time: register SnapTrade user
connect.py           # One-time: generate broker connection link
disconnect.py        # List/remove broker connections
delete_user.py       # Delete SnapTrade user entirely
fetch.py             # Debug: dump raw SnapTrade API responses
force_refresh.py     # Debug: force broker re-fetch
check_sync.py        # Debug: check sync timestamps
```
