# Portfolio Aggregator

Aggregates brokerage accounts and positions via [SnapTrade](https://snaptrade.com/) into a local SQLite database, served through a FastAPI dashboard.

## Features

- **Multi-brokerage support** — any broker SnapTrade connects to (Alpaca, Robinhood, Fidelity, etc.)
- **Auto-sync** every 15 minutes via APScheduler, plus manual force-refresh
- **External price feed** — [yfinance](https://github.com/ranaroussi/yfinance) is the primary source for current price and previous close; broker prices are the fallback when Yahoo can't resolve a symbol. Fixes stale broker quotes (e.g. Schwab) and gives a true previous-close basis for day change.
- **Daily change tracking** — day change $ and % computed at read time against the previous close (with a 9:29 ET pre-market snapshot as fallback). Direction-adjusted, so a profitable short reads positive.
- **Performance chart** — equity curve with %-return / TWR / $-value modes, range buttons + a custom start date, a per-account view, and configurable benchmark overlays (add any ticker, e.g. SPY/QQQ). TWR (time-weighted return) strips deposits/withdrawals out of the return so it's comparable to a benchmark.
- **Risk & performance analytics** — Sharpe / Sortino / Calmar, annualized vol, max drawdown, VaR & Expected Shortfall (Gaussian, Cornish-Fisher heavy-tailed, historical), skew / kurtosis, and benchmark-relative beta / alpha / correlation / capture — computed for the whole portfolio, **each account, and each benchmark**, over the chart's window, on deposit-adjusted (TWR) returns. A live-only toggle excludes reconstructed history so lower-fidelity tails don't skew the stats. Every formula lives in one file, [`analytics.py`](analytics.py).
- **Broker-sync health** — per-account last-sync staleness (yellow > 4h, red > 24h), plus a **"Reconnect"** flag when SnapTrade reports a connection inactive (`disabled`).
- **Historical backfill** — reconstructs the equity curve for the period *before* the app existed from SnapTrade transaction history (see [Historical backfill & maintenance](#historical-backfill--maintenance)).
- **Historical snapshots** — position and account-level snapshots stored for the equity curve, tagged `live` vs `reconstructed`.
- **Paper/real separation** — separate dashboard views at `/` and `/paper`
- **Locally-computed account totals** — `total_value` is recomputed each sync as `cash + Σ position market_value` rather than trusting the broker-reported total (works around a Robinhood undercounting bug). Equities + cash only; options aren't synced yet, so options-holding accounts would underreport.
- **Authentication** — a shared-secret gate on every route (browser logs in once at `/login`; API/device clients send an `X-App-Token` header). The app **fails closed** (HTTP 503) if `APP_TOKEN` is unset, so it can't silently run unauthenticated. See [Security](#security).

## Quickstart

```bash
# 1. Set up env
cp .env.example .env   # fill in SNAPTRADE_CLIENT_ID, SNAPTRADE_CONSUMER_KEY, SNAPTRADE_USER_ID, SNAPTRADE_USER_SECRET

# also set APP_TOKEN (required — the app refuses to serve without it). Generate one:
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Install deps
pip install -r requirements.txt

# 3. Run
uvicorn app.main:app --reload
```

Dashboard at `http://localhost:8000` — you'll be sent to `/login`; enter the `APP_TOKEN` once (it's stored in an HttpOnly cookie). API/device clients send it as the `X-App-Token` header instead.

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

`.env` must include `APP_TOKEN` (compose loads it via `env_file`) — without it the app returns 503 on every route. The container runs as a **non-root user (uid 10001)**; if your host `./data` dir was created by an earlier root container, chown it once so the new user can write:

```bash
sudo chown -R 10001:10001 ./data    # or /opt/portfolio/data
```

The container includes a health check at `/health` (30s interval, stays unauthenticated).

### Portainer

The container will appear in Portainer automatically if Portainer is monitoring the Docker socket. For full lifecycle management through Portainer, add it as a Stack (Stacks → Add Stack) and point to the `docker-compose.yml`.

To deploy a code change: pull on the server, build the image **on the same host Portainer manages**, then redeploy the stack with re-pull image + force-recreate (the `:latest` tag stays the same, so Portainer won't auto-detect the change otherwise):

```bash
cd /opt/portfolio/source && git pull
docker build -t portfolio-aggregator:latest .
# Portainer → Stacks → portfolio → Update/Redeploy (re-pull + force-recreate)
```

The SQLite DB lives on the mounted volume and survives rebuilds — see the [runbook](#full-cleanup--backfill-runbook-server--docker) for backfill/cleanup after deploying.

## Security

Designed for a single-user homelab (container bound to `127.0.0.1`, fronted by Caddy/Tailscale). Hardening in place:

- **App-layer auth** — every route requires `APP_TOKEN` (header `X-App-Token`, or the cookie set by `/login`). Constant-time comparison; **fails closed** if the token is unset. `/health` and `/login` are the only open paths. This is a second gate behind the network layer — a reverse-proxy/ACL slip no longer means open access, and it closes the browser-to-localhost (DNS-rebinding) vector. If you terminate auth at Caddy instead, have it inject `X-App-Token` and you can treat `APP_TOKEN` as an internal shared secret.
  - **Disabling auth:** set `AUTH_DISABLED=true` to bypass the gate entirely (runs wide open). Only do this when another layer already gates access (Caddy basic-auth, Tailscale-only, trusted LAN). It's a deliberate opt-out — an unset `APP_TOKEN` alone still fails closed rather than silently disabling — and a warning is logged at startup.
- **Pinned dependencies** — `requirements.txt` pins exact versions for reproducible builds. Bump deliberately; regenerate with hashes via `pip-compile --generate-hashes` if you want supply-chain enforcement.
- **Non-root container** — runs as uid 10001 with `no-new-privileges`. (Optional `read_only` rootfs is stubbed in `docker-compose.yml`; enable after verifying the yfinance cache has a writable home.)
- **No secrets in the repo** — `.env` and `data/` are gitignored; all SnapTrade calls are server-side, so broker credentials never reach the browser.

Accepted risks (single-user homelab): the SQLite DB is unencrypted at rest (rely on host disk encryption); `docker logs` contains account names/totals; `register.py`/`connect.py` print secrets to the terminal (run-once, locally); the app has no TLS of its own (Caddy/Tailscale provides transport security — don't expose uvicorn directly).

## API

All routes except `/health` and `/login` require the `APP_TOKEN` (header `X-App-Token` or the `/login` cookie).

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check (open) |
| `/login` | GET / POST | Login form / submit token, sets auth cookie (open) |
| `/api/accounts` | GET | All accounts with brokerage info |
| `/api/positions?paper=false` | GET | Positions with day change + P&L (incl. % and effective prices) |
| `/api/history?days=` | GET | Account value snapshots (optionally bounded to the last N days) |
| `/api/performance?paper=&days=&start=&account=` | GET | Daily equity series + aligned benchmark series for the chart |
| `/api/analytics?paper=&days=&start=&risk_free=&live_only=` | GET | Risk & performance metrics per subject (portfolio, each account, each benchmark) |
| `/api/benchmarks` | GET / POST / DELETE | List / add / remove benchmark tickers (`POST {"symbol": "QQQ"}`, `DELETE /api/benchmarks/{symbol}`) |
| `/api/sync` | POST | Trigger manual sync |
| `/api/sync/status` | GET | Current sync state (for the dashboard's polling) |

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

## Historical backfill & maintenance

Two operational scripts reconstruct pre-app history and clean up bad data points. Both are included in the Docker image so they can be run via `docker exec`, and both are re-runnable.

- **`backfill_history.py`** — ingests SnapTrade transaction history into the `activities` table, then reconstructs a daily equity curve for the period before the app started taking snapshots. It replays activities forward in today's split-adjusted share terms and values holdings with historical closes, writing `source='reconstructed'` rows. Shares transferred in / held before the transaction window are seeded as opening holdings so transfer-funded accounts aren't understated; money-market funds (FDRXX, SPAXX, …) are valued at $1. Prints a per-account validation report (seeded opening lots, anything still unpriceable, cash residual). Anchors to current positions/cash, so **sync first**.
- **`clean_snapshots.py`** — removes "cash-only" artifact snapshots left by a failed positions fetch before the preserve-on-error fix (a wiped-positions sync recorded `total_value ≈ cash`, plunging the curve). Scans all accounts, flags `live` snapshots below 10% of the account's normal max, and skips genuinely cash-only/tiny accounts. **Dry-run by default; pass `--delete` to remove.** Where a reconstructed row exists for the same day, the curve backfills automatically.

### Full cleanup + backfill runbook (server / Docker)

Run after deploying the latest image. Order matters: deploy → let it sync → back up → reconstruct → clean.

```bash
# 1. Latest code, build on the host
cd /opt/portfolio/source && git pull
docker build -t portfolio-aggregator:latest .

# 2. Redeploy the stack in Portainer (re-pull image + force-recreate).
#    On start it runs the schema migration and a startup sync.

# 3. Wait for the startup sync to finish (positions + cash must be current)
docker logs -f portfolio-aggregator        # Ctrl-C after the sync prints "Done."

# 4. Back up the DB (irreplaceable equity history)
docker cp portfolio-aggregator:/data/portfolio.db ./portfolio-backup-$(date +%F).db

# 5. Reconstruct history from transactions
docker exec -it portfolio-aggregator python backfill_history.py

# 6. Clean cash-only artifacts — preview, then delete
docker exec -it portfolio-aggregator python clean_snapshots.py
docker exec -it portfolio-aggregator python clean_snapshots.py --delete
```

For local (non-Docker) runs, the same scripts work from the project root with `.env` configured: `python backfill_history.py`, `python clean_snapshots.py [--delete]`.

## Project Structure

```
app/main.py          # FastAPI app, scheduler, routes
app/templates/       # Jinja2 dashboard template
db.py                # SQLite schema, helpers, queries, price feed, reconstruction
analytics.py         # All risk/performance computations (returns, TWR, VaR/ES, ratios)
sync_once.py         # SnapTrade sync logic + activities ingest
backfill_history.py  # Reconstruct pre-app equity history from transactions
clean_snapshots.py   # Remove cash-only artifact snapshots (dry-run by default)
deploy.sh            # Build and run via docker compose
register.py          # One-time: register SnapTrade user
connect.py           # One-time: generate broker connection link
disconnect.py        # List/remove broker connections
delete_user.py       # Delete SnapTrade user entirely
fetch.py             # Debug: dump raw SnapTrade API responses
force_refresh.py     # Debug: force broker re-fetch
check_sync.py        # Debug: check sync timestamps
```
