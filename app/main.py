"""
FastAPI webapp + background sync scheduler.
Run with: uvicorn app.main:app --reload
"""
import os
import hmac
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

import analytics
from db import (
    db,
    init_db,
    fetch_positions_with_day_change,
    fetch_daily_equity_series,
    fetch_external_flows,
    compute_twr_index,
    fetch_aligned_benchmark_series,
    list_benchmarks,
    add_benchmark,
    remove_benchmark,
)
from sync_once import run_sync

load_dotenv()

SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "15"))

scheduler = BackgroundScheduler(timezone="UTC")
templates = Jinja2Templates(directory="app/templates")

# ---------- auth ----------
# Single shared secret. The 127.0.0.1 bind + Caddy/Tailscale is the network
# gate; this is the application-layer second gate. Browsers authenticate once
# via /login (sets an HttpOnly cookie); API/device clients send X-App-Token.
APP_TOKEN = os.environ.get("APP_TOKEN", "")
# Set true when always fronted by HTTPS (Caddy); leave false for plain-HTTP LAN
# / Tailscale access so the login cookie is still sent.
APP_COOKIE_SECURE = os.environ.get("APP_COOKIE_SECURE", "false").lower() == "true"
# Explicit opt-out: run with NO auth (e.g. when another layer already gates
# access — Caddy basic-auth, Tailscale-only, trusted LAN). Must be set on
# purpose; an unset APP_TOKEN still fails closed rather than disabling silently.
AUTH_DISABLED = os.environ.get("AUTH_DISABLED", "false").lower() == "true"
if AUTH_DISABLED:
    print("WARNING: AUTH_DISABLED=true — the app is running with NO authentication")
# Reachable without the token. /health stays open for the Docker healthcheck;
# /login must be open so the user can authenticate.
_OPEN_PATHS = {"/health", "/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if AUTH_DISABLED or request.url.path in _OPEN_PATHS:
            return await call_next(request)
        if not APP_TOKEN:
            # Fail closed: a missing token must not silently run wide open.
            return JSONResponse(
                {"error": "server auth not configured (set APP_TOKEN)"},
                status_code=503,
            )
        presented = (
            request.headers.get("X-App-Token")
            or request.cookies.get("app_token")
            or ""
        )
        if not hmac.compare_digest(presented, APP_TOKEN):  # constant-time
            accepts_html = "text/html" in request.headers.get("accept", "")
            if accepts_html and request.method == "GET":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

# Lock ensures only one sync runs at a time, regardless of source (manual or scheduled).
_sync_lock = threading.Lock()

# Sync state for the frontend to poll. Updated under _sync_state_lock.
_sync_state_lock = threading.Lock()
_sync_state = {
    "running": False,
    "started_at": None,      # ISO timestamp when last sync began
    "finished_at": None,     # ISO timestamp when last sync finished
    "error": None,           # error message from the last sync, if any
}


def _set_sync_state(**kwargs):
    with _sync_state_lock:
        _sync_state.update(kwargs)


def _locked_sync(force: bool, snapshot_kind: str | None = None) -> bool:
    """
    Run a sync under the shared lock. Returns True if the sync ran, False if
    another sync was already in progress and we skipped.
    """
    if not _sync_lock.acquire(blocking=False):
        print("  sync skipped: another sync is already running")
        return False
    _set_sync_state(
        running=True,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        error=None,
    )
    try:
        run_sync(force=force, snapshot_kind=snapshot_kind)
        return True
    except Exception as e:
        _set_sync_state(error=str(e))
        raise
    finally:
        _sync_lock.release()
        _set_sync_state(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


def _scheduled_sync():
    """Entry point for the APScheduler interval job. Never forces."""
    _locked_sync(force=False)


def _pre_open_sync():
    """9:29 ET weekday job: snapshot with 'pre_open' tag for day-change reference."""
    _locked_sync(force=False, snapshot_kind="pre_open")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Run sync once at startup, then on a schedule
    scheduler.add_job(
        _scheduled_sync,
        "interval",
        minutes=SYNC_INTERVAL_MINUTES,
        next_run_time=datetime.now(timezone.utc),
        id="sync",
        max_instances=1,
        coalesce=True,
    )
    # 9:29 ET weekday snapshot for day-change reference prices
    scheduler.add_job(
        _pre_open_sync,
        CronTrigger(hour=9, minute=29, day_of_week="mon-fri",
                    timezone="America/New_York"),
        id="pre_open_snapshot",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(AuthMiddleware)


# ---------- auth routes ----------

_LOGIN_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Portfolio — Sign in</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:320px;margin:6rem auto;padding:0 1rem;color:#222}
input,button{font-size:1rem;padding:.55rem;width:100%;box-sizing:border-box;margin-top:.6rem}
button{background:#222;color:#fff;border:none;border-radius:4px;cursor:pointer}
.err{color:#c0392b;font-size:.9rem;min-height:1.2em}</style></head><body>
<h2>Portfolio</h2>
<form onsubmit="return doLogin(event)">
<input id=t type=password placeholder="Access token" autofocus autocomplete=current-password>
<button type=submit>Sign in</button>
</form><p id=err class=err></p>
<script>
async function doLogin(e){e.preventDefault();
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:document.getElementById('t').value})});
  if(r.ok){location.href='/';}
  else{document.getElementById('err').textContent=r.status===503?'Server auth not configured.':'Incorrect token.';}
  return false;}
</script></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_form():
    return _LOGIN_HTML


@app.post("/login")
def login_submit(payload: dict):
    if not APP_TOKEN:
        return JSONResponse({"error": "server auth not configured"}, status_code=503)
    if not hmac.compare_digest(payload.get("token") or "", APP_TOKEN):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "app_token", APP_TOKEN,
        httponly=True, samesite="strict", secure=APP_COOKIE_SECURE,
        max_age=60 * 60 * 24 * 30, path="/",
    )
    return resp


# ---------- JSON API ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/accounts")
def api_accounts():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT a.*, b.display_name AS brokerage_name, b.logo_url
            FROM accounts a
            JOIN connections c ON c.id = a.connection_id
            JOIN brokerages b ON b.id = c.brokerage_id
            ORDER BY a.name
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/positions")
def api_positions(paper: bool = False):
    with db() as conn:
        rows = fetch_positions_with_day_change(conn, 1 if paper else 0)
    return [dict(r) for r in rows]


@app.get("/api/history")
def api_history(days: int | None = None):
    where = ""
    params: list = []
    if days:
        where = "WHERE date(snapshot_at) >= date('now', ?)"
        params.append(f"-{int(days)} days")
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT snapshot_at, account_id, total_value, long_market_value, short_market_value
            FROM account_value_snapshots
            {where}
            ORDER BY snapshot_at
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/performance")
def api_performance(paper: bool = False, days: int | None = None,
                    start: str | None = None, account: str | None = None):
    """Daily portfolio equity series plus each benchmark's close aligned to the
    same dates. `start` (YYYY-MM-DD) clips the series and overrides days;
    `account` restricts to a single account. Frontend normalizes at display time."""
    is_paper_flag = 1 if paper else 0
    with db() as conn:
        series = fetch_daily_equity_series(conn, is_paper_flag, days, start, account)
        dates = [p["date"] for p in series]
        benchmarks = {
            sym: fetch_aligned_benchmark_series(conn, sym, dates)
            for sym in list_benchmarks(conn)
        }
        twr = compute_twr_index(conn, series, is_paper_flag, account)
    return {
        "dates": dates,
        "portfolio": [p["value"] for p in series],
        "benchmarks": benchmarks,
        "twr": twr,
    }


@app.get("/api/analytics")
def api_analytics(paper: bool = False, days: int | None = None,
                  start: str | None = None, risk_free: float = 0.0):
    """
    Risk & performance metrics for every subject: the aggregate portfolio, each
    account (sub-portfolio), and each benchmark standalone. Portfolio subjects
    also get benchmark-relative metrics (beta/alpha/etc.) vs each benchmark.
    All math lives in analytics.py; this only assembles the return series.
    `risk_free` is an annual rate (e.g. 0.04). `start` (YYYY-MM-DD) clips the
    window; risk stats default to live+reconstructed but respect that clip.
    """
    is_paper_flag = 1 if paper else 0
    subjects: dict = {}

    with db() as conn:
        benchmarks = list_benchmarks(conn)

        def portfolio_subject(series, account_id):
            dates = [p["date"] for p in series]
            values = [p["value"] for p in series]
            flows = fetch_external_flows(conn, dates, is_paper_flag, account_id)
            rets = analytics.daily_returns(values, flows)
            block = analytics.compute_metrics(rets, risk_free)
            block["kind"] = "portfolio"
            vs = {}
            for sym in benchmarks:
                closes = fetch_aligned_benchmark_series(conn, sym, dates)
                brets = analytics.simple_returns(closes)
                vs[sym] = analytics.compute_vs_benchmark(rets, brets, risk_free)
            block["vs_benchmark"] = vs
            return block

        # Aggregate portfolio (defines the window used for standalone benchmarks)
        agg = fetch_daily_equity_series(conn, is_paper_flag, days, start, None)
        agg_dates = [p["date"] for p in agg]
        if len(agg) >= 2:
            subjects["Portfolio"] = portfolio_subject(agg, None)

        # Each account = a sub-portfolio (skip empty / all-zero accounts)
        accts = conn.execute(
            "SELECT id, name FROM accounts WHERE is_paper = ? ORDER BY name",
            (is_paper_flag,),
        ).fetchall()
        for a in accts:
            s = fetch_daily_equity_series(conn, is_paper_flag, days, start, a["id"])
            if len(s) >= 2 and max((p["value"] or 0) for p in s) > 0:
                subjects[a["name"] or a["id"]] = portfolio_subject(s, a["id"])

        # Each benchmark standalone, over the aggregate window
        for sym in benchmarks:
            closes = fetch_aligned_benchmark_series(conn, sym, agg_dates)
            brets = analytics.simple_returns(closes)
            block = analytics.compute_metrics(brets, risk_free)
            block["kind"] = "benchmark"
            subjects[sym] = block

    return {
        "window": {
            "start": agg_dates[0] if agg_dates else None,
            "end": agg_dates[-1] if agg_dates else None,
            "n": len(agg_dates),
        },
        "risk_free": risk_free,
        "subjects": subjects,
    }


@app.get("/api/benchmarks")
def api_benchmarks_list():
    with db() as conn:
        return list_benchmarks(conn)


@app.post("/api/benchmarks")
def api_benchmarks_add(payload: dict):
    symbol = (payload.get("symbol") or "").strip().upper()
    if not symbol:
        return {"error": "symbol required"}
    try:
        with db() as conn:
            ok = add_benchmark(conn, symbol)
            benchmarks = list_benchmarks(conn) if ok else None
    except Exception as e:
        # Transient Yahoo/yfinance failure (rate limit, network). Surface a
        # clear message instead of an opaque 500 so the UI can show it.
        print(f"benchmark add error for {symbol}: {e}")
        return {"error": f"Yahoo fetch for '{symbol}' failed — try again"}
    if not ok:
        return {"error": f"could not resolve '{symbol}' on Yahoo Finance"}
    return {"benchmarks": benchmarks}


@app.delete("/api/benchmarks/{symbol}")
def api_benchmarks_remove(symbol: str):
    with db() as conn:
        remove_benchmark(conn, symbol)
        return {"benchmarks": list_benchmarks(conn)}


@app.post("/api/sync")
def api_sync():
    """Kick off a forced sync in the background and return immediately."""
    with _sync_state_lock:
        if _sync_state["running"]:
            return {"status": "already_running"}

    def _run():
        try:
            _locked_sync(force=True)
        except Exception as e:
            print(f"sync error: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.get("/api/sync/status")
def api_sync_status():
    with _sync_state_lock:
        return dict(_sync_state)


# ---------- Dashboard ----------

def _render_dashboard(request: Request, paper: bool):
    is_paper_flag = 1 if paper else 0

    with db() as conn:
        accounts = conn.execute(
            """
            SELECT
                a.*,
                b.display_name AS brokerage_name,
                COALESCE(pos_count.n, 0) AS position_count
            FROM accounts a
            JOIN connections c ON c.id = a.connection_id
            JOIN brokerages b ON b.id = c.brokerage_id
            LEFT JOIN (
                SELECT account_id, COUNT(*) AS n
                FROM positions
                GROUP BY account_id
            ) pos_count ON pos_count.account_id = a.id
            WHERE a.is_paper = ?
              AND NOT (
                  COALESCE(pos_count.n, 0) = 0
                  AND COALESCE(a.cash, 0) = 0
                  AND COALESCE(a.total_value, 0) = 0
              )
            ORDER BY a.name
            """,
            (is_paper_flag,),
        ).fetchall()

        positions = fetch_positions_with_day_change(conn, is_paper_flag)

        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(total_value), 0) AS net_value,
                COALESCE(SUM(cash), 0) AS total_cash
            FROM accounts
            WHERE is_paper = ?
            """,
            (is_paper_flag,),
        ).fetchone()

        long_short = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN p.units > 0
                    THEN COALESCE(pr.price, p.market_price) * p.units END), 0) AS long_mv,
                COALESCE(SUM(CASE WHEN p.units < 0
                    THEN COALESCE(pr.price, p.market_price) * p.units END), 0) AS short_mv,
                COALESCE(SUM(
                    COALESCE((pr.price - p.avg_purchase_price) * p.units, p.computed_pnl)
                ), 0) AS total_pnl,
                COALESCE(SUM(p.avg_purchase_price * ABS(p.units)), 0) AS cost_basis
            FROM positions p
            JOIN accounts a ON a.id = p.account_id
            LEFT JOIN prices pr ON pr.symbol = p.symbol
            WHERE a.is_paper = ?
            """,
            (is_paper_flag,),
        ).fetchone()

        last_sync = conn.execute(
            """
            SELECT MAX(avs.snapshot_at) AS ts
            FROM account_value_snapshots avs
            JOIN accounts a ON a.id = avs.account_id
            WHERE a.is_paper = ?
            """,
            (is_paper_flag,),
        ).fetchone()

    positions_dicts = [dict(p) for p in positions]
    total_day_change = sum(p["day_change"] for p in positions_dicts if p.get("day_change") is not None)

    # Per-account day change, keyed by account_id
    account_day_change: dict[str, float] = {}
    for p in positions_dicts:
        if p.get("day_change") is not None:
            account_day_change[p["account_id"]] = (
                account_day_change.get(p["account_id"], 0) + p["day_change"]
            )

    accounts_dicts = [dict(a) for a in accounts]
    for a in accounts_dicts:
        a["day_change"] = account_day_change.get(a["id"])

    net_value = totals["net_value"]
    long_mv = long_short["long_mv"]
    short_mv = long_short["short_mv"]
    total_pnl = long_short["total_pnl"]
    cost_basis = long_short["cost_basis"]
    net_exposure = long_mv + short_mv

    # Percentages. Exposure is measured against net liquidation value (NLV);
    # Open P&L against gross cost basis; day change against the prior-day
    # portfolio value (current NLV minus today's change).
    long_pct = (long_mv / net_value) if net_value else None
    short_pct = (short_mv / net_value) if net_value else None
    net_exposure_pct = (net_exposure / net_value) if net_value else None
    pnl_pct = (total_pnl / cost_basis) if cost_basis else None
    prior_value = net_value - total_day_change
    day_change_pct = (total_day_change / prior_value) if prior_value else None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "accounts": accounts_dicts,
            "positions": positions_dicts,
            "net_value": net_value,
            "total_cash": totals["total_cash"],
            "long_mv": long_mv,
            "short_mv": short_mv,
            "long_pct": long_pct,
            "short_pct": short_pct,
            "net_exposure": net_exposure,
            "net_exposure_pct": net_exposure_pct,
            "total_pnl": total_pnl,
            "pnl_pct": pnl_pct,
            "total_day_change": total_day_change,
            "day_change_pct": day_change_pct,
            "is_paper_view": paper,
            "last_sync": last_sync["ts"],
        },
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return _render_dashboard(request, paper=False)


@app.get("/paper", response_class=HTMLResponse)
def dashboard_paper(request: Request):
    return _render_dashboard(request, paper=True)
