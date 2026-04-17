"""
FastAPI webapp + background sync scheduler.
Run with: uvicorn app.main:app --reload
"""
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from db import db, init_db, fetch_positions_with_day_change
from sync_once import run_sync

load_dotenv()

SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "15"))

scheduler = BackgroundScheduler(timezone="UTC")
templates = Jinja2Templates(directory="app/templates")

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
def api_history():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT snapshot_at, account_id, total_value, long_market_value, short_market_value
            FROM account_value_snapshots
            ORDER BY snapshot_at
            """
        ).fetchall()
    return [dict(r) for r in rows]


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
                COALESCE(SUM(CASE WHEN p.units > 0 THEN p.market_value END), 0) AS long_mv,
                COALESCE(SUM(CASE WHEN p.units < 0 THEN p.market_value END), 0) AS short_mv,
                COALESCE(SUM(p.computed_pnl), 0) AS total_pnl
            FROM positions p
            JOIN accounts a ON a.id = p.account_id
            WHERE a.is_paper = ?
            """,
            (is_paper_flag,),
        ).fetchone()

        last_sync = conn.execute(
            """
            SELECT MAX(last_holdings_sync) AS ts
            FROM accounts
            WHERE is_paper = ?
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

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "accounts": accounts_dicts,
            "positions": positions_dicts,
            "net_value": totals["net_value"],
            "total_cash": totals["total_cash"],
            "long_mv": long_short["long_mv"],
            "short_mv": long_short["short_mv"],
            "total_pnl": long_short["total_pnl"],
            "total_day_change": total_day_change,
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
