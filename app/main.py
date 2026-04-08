"""
FastAPI webapp + background sync scheduler.
Run with: uvicorn app.main:app --reload
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from db import db, init_db
from sync_once import run_sync

load_dotenv()

SYNC_INTERVAL_MINUTES = int(os.environ.get("SYNC_INTERVAL_MINUTES", "15"))

scheduler = BackgroundScheduler(timezone="UTC")
templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Run sync once at startup, then on a schedule
    scheduler.add_job(
        run_sync,
        "interval",
        minutes=SYNC_INTERVAL_MINUTES,
        next_run_time=datetime.now(timezone.utc),
        id="sync",
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
def api_positions():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.*, a.name AS account_name
            FROM positions p
            JOIN accounts a ON a.id = p.account_id
            ORDER BY ABS(p.market_value) DESC
            """
        ).fetchall()
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
    run_sync()
    return {"status": "ok"}


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
        
        positions = conn.execute(
            """
            SELECT p.*, a.name AS account_name
            FROM positions p
            JOIN accounts a ON a.id = p.account_id
            WHERE a.is_paper = ?
            ORDER BY ABS(p.market_value) DESC
            """,
            (is_paper_flag,),
        ).fetchall()

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

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "accounts": [dict(a) for a in accounts],
            "positions": [dict(p) for p in positions],
            "net_value": totals["net_value"],
            "total_cash": totals["total_cash"],
            "long_mv": long_short["long_mv"],
            "short_mv": long_short["short_mv"],
            "total_pnl": long_short["total_pnl"],
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
