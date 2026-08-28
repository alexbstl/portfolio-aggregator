"""
SQLite schema and helpers for the portfolio aggregator.
USD-only for now. Single file, no migrations framework — if the schema
changes early on, just delete portfolio.db and re-sync.
"""
import sqlite3
import bisect
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import os
import yfinance as yf

import analytics

DB_PATH = Path(os.environ.get("DATABASE_PATH", "./data/portfolio.db"))


# ---------- connection ----------

def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- schema ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS brokerages (
    id              TEXT PRIMARY KEY,           -- SnapTrade brokerage UUID
    slug            TEXT NOT NULL,              -- e.g. "ALPACA-PAPER"
    display_name    TEXT NOT NULL,
    logo_url        TEXT
);

CREATE TABLE IF NOT EXISTS connections (
    id                  TEXT PRIMARY KEY,       -- SnapTrade authorization UUID
    brokerage_id        TEXT NOT NULL,
    created_date        TEXT,
    disabled            INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (brokerage_id) REFERENCES brokerages(id)
);

CREATE TABLE IF NOT EXISTS accounts (
    id                      TEXT PRIMARY KEY,   -- SnapTrade account UUID
    connection_id           TEXT NOT NULL,
    name                    TEXT,
    number                  TEXT,                -- masked acct number from broker
    institution_name        TEXT,
    is_paper                INTEGER NOT NULL DEFAULT 0,
    total_value             REAL,                -- broker-reported total (source of truth)
    cash                    REAL,
    buying_power            REAL,
    last_holdings_sync      TEXT,                -- ISO timestamp from broker
    FOREIGN KEY (connection_id) REFERENCES connections(id)
);

-- Current state: one row per (account, symbol). Overwritten on every sync.
CREATE TABLE IF NOT EXISTS positions (
    account_id              TEXT NOT NULL,
    symbol                  TEXT NOT NULL,       -- ticker, e.g. "AAPL"
    description             TEXT,
    figi                    TEXT,                -- nullable; cross-broker join key when present
    exchange                TEXT,                -- e.g. "NASDAQ"
    security_type           TEXT,                -- e.g. "cs" for common stock
    units                   REAL NOT NULL,       -- SIGNED: negative = short
    avg_purchase_price      REAL,
    market_price            REAL,
    market_value            REAL,                -- units * market_price (signed)
    open_pnl                REAL,                -- broker-reported, for reference
    computed_pnl            REAL,                -- units * (price - avg_price)
    as_of                   TEXT NOT NULL,       -- when we wrote this row
    PRIMARY KEY (account_id, symbol),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

-- Per-position historical snapshots. One row per (account, symbol) per sync.
-- snapshot_kind: NULL for regular 15-min snapshots, 'pre_open' for the 9:29 ET
-- reference snapshot used to compute daily change.
CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_at             TEXT NOT NULL,
    account_id              TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    units                   REAL NOT NULL,
    market_price            REAL,
    market_value            REAL,
    snapshot_kind           TEXT,                 -- NULL | 'pre_open'
    PRIMARY KEY (snapshot_at, account_id, symbol),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_position_snapshots_symbol
    ON position_snapshots(symbol, snapshot_at);

CREATE INDEX IF NOT EXISTS idx_position_snapshots_account
    ON position_snapshots(account_id, snapshot_at);

CREATE INDEX IF NOT EXISTS idx_position_snapshots_kind
    ON position_snapshots(snapshot_kind, snapshot_at);

-- Latest external market prices, refreshed each sync.
-- price = most recent trade (including after-hours); prev_close = prior regular-session close.
-- Used as the primary price source; broker prices in `positions` are the fallback.
CREATE TABLE IF NOT EXISTS prices (
    symbol      TEXT PRIMARY KEY,
    price       REAL,
    prev_close  REAL,
    as_of       TEXT
);

-- Raw transaction/activity history from SnapTrade. Immutable historical
-- records; also the foundation for realized-P&L. Used to reconstruct the
-- equity curve for the period before the app started taking snapshots.
CREATE TABLE IF NOT EXISTS activities (
    id              TEXT PRIMARY KEY,    -- SnapTrade activity UUID
    account_id      TEXT NOT NULL,
    type            TEXT NOT NULL,       -- BUY/SELL/REI/DIVIDEND/CONTRIBUTION/...
    symbol          TEXT,                -- ticker; NULL for pure-cash events
    trade_date      TEXT,
    settlement_date TEXT,
    units           REAL,                -- signed
    price           REAL,
    amount          REAL,                -- signed cash impact
    fee             REAL,
    option_ticker   TEXT,                -- option_symbol.ticker when present
    description     TEXT
);

CREATE INDEX IF NOT EXISTS idx_activities_acct_date
    ON activities(account_id, trade_date);

-- Daily close history used by reconstruction (raw closes, not dividend-adjusted).
CREATE TABLE IF NOT EXISTS price_history (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,               -- YYYY-MM-DD
    close   REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

-- Benchmark symbols the user has chosen to compare against (e.g. SPY, QQQ).
CREATE TABLE IF NOT EXISTS benchmarks (
    symbol      TEXT PRIMARY KEY,
    added_at    TEXT
);

-- Daily close history for each benchmark symbol, pulled from Yahoo Finance.
CREATE TABLE IF NOT EXISTS benchmark_prices (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,              -- YYYY-MM-DD
    close       REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

-- Account-level historical snapshots for the equity curve.
CREATE TABLE IF NOT EXISTS account_value_snapshots (
    snapshot_at             TEXT NOT NULL,
    account_id              TEXT NOT NULL,
    total_value             REAL NOT NULL,
    cash                    REAL,
    long_market_value       REAL,                -- sum of positive market_value
    short_market_value      REAL,                -- sum of negative market_value
    source                  TEXT DEFAULT 'live', -- 'live' | 'reconstructed'
    PRIMARY KEY (snapshot_at, account_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
"""


def init_db():
    with db() as conn:
        # Migration: add snapshot_kind column before running full schema
        # (CREATE TABLE IF NOT EXISTS won't add new columns to existing tables,
        # and the CREATE INDEX on snapshot_kind would fail without the column)
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='position_snapshots'"
        ).fetchone()
        if existing:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(position_snapshots)")}
            if "snapshot_kind" not in cols:
                conn.execute("ALTER TABLE position_snapshots ADD COLUMN snapshot_kind TEXT")

        # Migration: tag account_value_snapshots with their source so live and
        # reconstructed (backfilled) points can coexist and be told apart.
        avs = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='account_value_snapshots'"
        ).fetchone()
        if avs:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(account_value_snapshots)")}
            if "source" not in cols:
                conn.execute(
                    "ALTER TABLE account_value_snapshots ADD COLUMN source TEXT DEFAULT 'live'"
                )
        conn.executescript(SCHEMA)


# ---------- helpers ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(d: dict, *keys, default=None):
    """Walk a nested dict safely. _safe(pos, 'symbol', 'symbol', 'symbol') -> 'AAPL' or None."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


# ---------- writes ----------

def upsert_brokerage(conn, brokerage: dict):
    conn.execute(
        """
        INSERT INTO brokerages (id, slug, display_name, logo_url)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            slug=excluded.slug,
            display_name=excluded.display_name,
            logo_url=excluded.logo_url
        """,
        (
            brokerage["id"],
            brokerage.get("slug", ""),
            brokerage.get("display_name") or brokerage.get("name", ""),
            brokerage.get("aws_s3_square_logo_url") or brokerage.get("aws_s3_logo_url"),
        ),
    )


def upsert_connection(conn, connection: dict):
    brokerage = connection["brokerage"]
    upsert_brokerage(conn, brokerage)
    conn.execute(
        """
        INSERT INTO connections (id, brokerage_id, created_date, disabled)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            brokerage_id=excluded.brokerage_id,
            disabled=excluded.disabled
        """,
        (
            connection["id"],
            brokerage["id"],
            connection.get("created_date"),
            1 if connection.get("disabled") else 0,
        ),
    )


def upsert_account(conn, account: dict, balance_row: dict | None):
    total = _safe(account, "balance", "total", "amount")
    cash = balance_row.get("cash") if balance_row else None
    buying_power = balance_row.get("buying_power") if balance_row else None

    conn.execute(
        """
        INSERT INTO accounts (
            id, connection_id, name, number, institution_name,
            is_paper, total_value, cash, buying_power, last_holdings_sync
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            connection_id=excluded.connection_id,
            name=excluded.name,
            number=excluded.number,
            institution_name=excluded.institution_name,
            is_paper=excluded.is_paper,
            total_value=excluded.total_value,
            cash=excluded.cash,
            buying_power=excluded.buying_power,
            last_holdings_sync=excluded.last_holdings_sync
        """,
        (
            account["id"],
            account["brokerage_authorization"],
            account.get("name"),
            account.get("number"),
            account.get("institution_name"),
            1 if account.get("is_paper") else 0,
            total,
            cash,
            buying_power,
            _safe(account, "sync_status", "holdings", "last_successful_sync"),
        ),
    )


def replace_positions(conn, account_id: str, positions: list[dict], snapshot_at: str,
                      snapshot_kind: str | None = None):
    """
    Overwrite the live `positions` rows for this account, AND append a row
    per position to `position_snapshots`.

    snapshot_kind: None for regular syncs, 'pre_open' for the 9:29 ET reference snapshot.
    """
    # Wipe current state for this account so closed positions disappear
    conn.execute("DELETE FROM positions WHERE account_id = ?", (account_id,))

    for pos in positions:
        symbol = _safe(pos, "symbol", "symbol", "symbol")
        if not symbol:
            continue  # skip anything malformed

        units = pos.get("units") or 0.0
        price = pos.get("price")
        avg = pos.get("average_purchase_price")
        market_value = (units * price) if (units is not None and price is not None) else None
        computed_pnl = (
            units * (price - avg)
            if (units is not None and price is not None and avg is not None)
            else None
        )

        row = (
            account_id,
            symbol,
            _safe(pos, "symbol", "symbol", "description"),
            _safe(pos, "symbol", "symbol", "figi_code"),
            _safe(pos, "symbol", "symbol", "exchange", "code"),
            _safe(pos, "symbol", "symbol", "type", "code"),
            units,
            avg,
            price,
            market_value,
            pos.get("open_pnl"),
            computed_pnl,
            snapshot_at,
        )

        conn.execute(
            """
            INSERT INTO positions (
                account_id, symbol, description, figi, exchange, security_type,
                units, avg_purchase_price, market_price, market_value,
                open_pnl, computed_pnl, as_of
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

        conn.execute(
            """
            INSERT INTO position_snapshots (
                snapshot_at, account_id, symbol, units, market_price, market_value,
                snapshot_kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_at, account_id, symbol) DO NOTHING
            """,
            (snapshot_at, account_id, symbol, units, price, market_value,
             snapshot_kind),
        )


def fetch_positions_with_day_change(conn, is_paper: int) -> list[sqlite3.Row]:
    """
    Return all positions for real (is_paper=0) or paper (is_paper=1) accounts,
    joined with the latest pre_open snapshot to compute day change.
    """
    return conn.execute(
        """
        WITH ref_prices AS (
            SELECT account_id, symbol, market_price AS ref_price
            FROM position_snapshots ps1
            WHERE snapshot_kind = 'pre_open'
              AND snapshot_at = (
                SELECT MAX(snapshot_at) FROM position_snapshots ps2
                WHERE ps2.account_id = ps1.account_id
                  AND ps2.symbol = ps1.symbol
                  AND ps2.snapshot_kind = 'pre_open'
              )
        )
        SELECT
            p.*,
            a.name AS account_name,
            COALESCE(pr.price, p.market_price) AS effective_price,
            COALESCE(pr.price, p.market_price) * p.units AS effective_market_value,
            COALESCE(
                (pr.price - p.avg_purchase_price) * p.units,
                p.computed_pnl
            ) AS effective_pnl,
            CASE
                WHEN p.avg_purchase_price IS NOT NULL AND p.avg_purchase_price != 0
                 AND p.units != 0
                THEN COALESCE((pr.price - p.avg_purchase_price) * p.units, p.computed_pnl)
                     / (p.avg_purchase_price * ABS(p.units))
            END AS effective_pnl_pct,
            COALESCE(pr.prev_close, r.ref_price) AS day_ref_price,
            CASE
                WHEN COALESCE(pr.prev_close, r.ref_price) IS NOT NULL
                 AND COALESCE(pr.price, p.market_price) IS NOT NULL
                THEN (COALESCE(pr.price, p.market_price) - COALESCE(pr.prev_close, r.ref_price))
                     * p.units
            END AS day_change,
            CASE
                WHEN COALESCE(pr.prev_close, r.ref_price) IS NOT NULL
                 AND COALESCE(pr.prev_close, r.ref_price) != 0
                 AND COALESCE(pr.price, p.market_price) IS NOT NULL
                THEN (COALESCE(pr.price, p.market_price) - COALESCE(pr.prev_close, r.ref_price))
                     / COALESCE(pr.prev_close, r.ref_price)
                     * (CASE WHEN p.units < 0 THEN -1 ELSE 1 END)
            END AS day_change_pct
        FROM positions p
        JOIN accounts a ON a.id = p.account_id
        LEFT JOIN prices pr ON pr.symbol = p.symbol
        LEFT JOIN ref_prices r ON r.account_id = p.account_id AND r.symbol = p.symbol
        WHERE a.is_paper = ?
        ORDER BY ABS(COALESCE(pr.price, p.market_price) * p.units) DESC
        """,
        (is_paper,),
    ).fetchall()


# Bounds for refresh_prices. Yahoo throttling turns the per-symbol loop into
# an hours-long retry storm without these (one wedged sync blocked the app for
# ~23h); a bounded pass just leaves stale rows, which display already tolerates.
PRICE_FETCH_TIMEOUT_S = 15                    # hard cap per symbol
PRICE_REFRESH_BUDGET_S = 300                  # hard cap for the whole pass
PRICE_REFRESH_MAX_CONSECUTIVE_FAILURES = 8    # assume throttled and bail


def _fetch_fast_info(symbol: str):
    fi = yf.Ticker(symbol).fast_info
    return fi.last_price, fi.previous_close


def refresh_prices(conn, symbols: list[str]):
    """
    Fetch current price + previous close from Yahoo Finance for each symbol
    and upsert into the `prices` table. Failures per-symbol are swallowed so
    a bad ticker doesn't abort the sync; the stale row (if any) stays in place.

    The pass is bounded: each fetch runs under a hard timeout, the loop stops
    once its total time budget is spent, and a run of consecutive failures
    aborts early (Yahoo is throttling — the remaining symbols would fail too).
    Skipped symbols keep their stale row / broker-price fallback.
    """
    if not symbols:
        return
    ts = now_iso()
    deadline = time.monotonic() + PRICE_REFRESH_BUDGET_S
    consecutive_failures = 0
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        for i, symbol in enumerate(symbols):
            if time.monotonic() > deadline:
                print(f"  price refresh: time budget spent, "
                      f"skipping {len(symbols) - i} remaining symbol(s)")
                break
            if consecutive_failures >= PRICE_REFRESH_MAX_CONSECUTIVE_FAILURES:
                print(f"  price refresh: {consecutive_failures} consecutive "
                      f"failures (throttled?), "
                      f"skipping {len(symbols) - i} remaining symbol(s)")
                break
            try:
                future = executor.submit(_fetch_fast_info, symbol)
                price, prev_close = future.result(timeout=PRICE_FETCH_TIMEOUT_S)
                consecutive_failures = 0
            except FuturesTimeoutError:
                # The worker is wedged in a network call that ignores its own
                # timeouts. Abandon this executor (the thread leaks until the
                # call dies — bounded by the failure cap) and continue fresh.
                executor.shutdown(wait=False)
                executor = ThreadPoolExecutor(max_workers=1)
                consecutive_failures += 1
                continue
            except Exception:
                consecutive_failures += 1
                continue
            if price is None:
                continue
            conn.execute(
                """
                INSERT INTO prices (symbol, price, prev_close, as_of)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    price      = excluded.price,
                    prev_close = excluded.prev_close,
                    as_of      = excluded.as_of
                """,
                (symbol, price, prev_close, ts),
            )
    finally:
        executor.shutdown(wait=False)


def recompute_account_total(conn, account_id: str):
    """
    Overwrite accounts.total_value with cash + SUM(positions.market_value).
    SnapTrade's broker-reported total is unreliable for some brokers
    (Robinhood has returned ~$4 vs an actual ~$37k). Cash and per-position
    market values are reliable, so we recompute. market_value is signed,
    so shorts subtract correctly. Skipped when cash is NULL (balance fetch
    failed) to avoid zeroing out the cash component.

    Caveat: options aren't stored in `positions`, so accounts holding
    options will underreport here.
    """
    conn.execute(
        """
        UPDATE accounts
        SET total_value = cash + COALESCE(
            (SELECT SUM(COALESCE(pr.price, p.market_price) * p.units)
             FROM positions p
             LEFT JOIN prices pr ON pr.symbol = p.symbol
             WHERE p.account_id = ?),
            0
        )
        WHERE id = ? AND cash IS NOT NULL
        """,
        (account_id, account_id),
    )


def insert_account_value_snapshot(conn, account_id: str, snapshot_at: str):
    """Compute long/short totals from the just-written positions and snapshot the account."""
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN units > 0 THEN market_value END), 0) AS long_mv,
            COALESCE(SUM(CASE WHEN units < 0 THEN market_value END), 0) AS short_mv
        FROM positions
        WHERE account_id = ?
        """,
        (account_id,),
    ).fetchone()

    acct = conn.execute(
        "SELECT total_value, cash FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()

    if acct["total_value"] is None:
        return  # no balance data (e.g. broker returned 500), skip snapshot

    conn.execute(
        """
        INSERT INTO account_value_snapshots (
            snapshot_at, account_id, total_value, cash,
            long_market_value, short_market_value
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_at, account_id) DO NOTHING
        """,
        (
            snapshot_at,
            account_id,
            acct["total_value"],
            acct["cash"],
            row["long_mv"],
            row["short_mv"],
        ),
    )


# ---------- benchmarks & performance ----------

def list_benchmarks(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT symbol FROM benchmarks ORDER BY symbol"
    ).fetchall()]


def _earliest_snapshot_date(conn) -> str | None:
    row = conn.execute(
        "SELECT MIN(date(snapshot_at)) FROM account_value_snapshots"
    ).fetchone()
    return row[0] if row and row[0] else None


def refresh_benchmark_history(conn, symbol: str, retries: int = 2) -> bool:
    """
    Pull daily close history for `symbol` from Yahoo Finance, from the earliest
    account snapshot date (so the comparison covers the full portfolio history)
    to today, and upsert into benchmark_prices. Idempotent.

    Yahoo throttles yfinance often, so the fetch is retried with a short backoff.
    If all attempts raise, the last exception is re-raised so callers can
    distinguish a transient fetch failure from a valid-but-empty result.
    Returns True if rows were written, False if the symbol resolved no data
    (likely an invalid ticker).
    """
    start = _earliest_snapshot_date(conn)
    hist = None
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            tk = yf.Ticker(symbol)
            hist = tk.history(start=start) if start else tk.history(period="1y")
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    if hist is None or hist.empty:
        return False
    wrote = False
    for idx, row in hist.iterrows():
        try:
            d = idx.strftime("%Y-%m-%d")
            close = float(row["Close"])
        except Exception:
            continue
        if close != close:  # NaN (halts / missing bars)
            continue
        conn.execute(
            """
            INSERT INTO benchmark_prices (symbol, date, close)
            VALUES (?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET close = excluded.close
            """,
            (symbol, d, close),
        )
        wrote = True
    return wrote


def add_benchmark(conn, symbol: str) -> bool:
    """
    Register a benchmark symbol and backfill its history. Returns True if the
    symbol resolved to real price data (so the caller can reject typos).
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return False
    ok = refresh_benchmark_history(conn, symbol)
    if not ok:
        return False  # don't register a symbol Yahoo can't resolve
    conn.execute(
        "INSERT INTO benchmarks (symbol, added_at) VALUES (?, ?) "
        "ON CONFLICT(symbol) DO NOTHING",
        (symbol, now_iso()),
    )
    return True


def remove_benchmark(conn, symbol: str) -> None:
    symbol = (symbol or "").strip().upper()
    conn.execute("DELETE FROM benchmarks WHERE symbol = ?", (symbol,))
    conn.execute("DELETE FROM benchmark_prices WHERE symbol = ?", (symbol,))


def refresh_benchmarks(conn) -> None:
    """Refresh history for every registered benchmark (called on the daily job).
    Per-symbol failures are swallowed so one bad fetch can't abort the sync."""
    for symbol in list_benchmarks(conn):
        try:
            refresh_benchmark_history(conn, symbol)
        except Exception as e:
            print(f"  benchmark refresh failed for {symbol}: {e}")


def fetch_daily_equity_series(conn, is_paper: int, days: int | None = None,
                              start: str | None = None,
                              account_id: str | None = None,
                              live_only: bool = False) -> list[dict]:
    """
    One portfolio total per calendar day: for each day, take each account's last
    snapshot and sum across accounts. Accounts with no snapshot on a given day are
    simply absent from that day's sum (correct for accounts added later).

    `start` (YYYY-MM-DD) clips the series to that date onward and takes precedence
    over `days`. `account_id` restricts the series to a single account. `live_only`
    excludes reconstructed (backfilled) rows so lower-fidelity history doesn't skew
    risk stats.
    """
    where_days = ""
    params: list = [is_paper]
    if account_id:
        where_days += " AND avs.account_id = ?"
        params.append(account_id)
    if live_only:
        where_days += " AND avs.source = 'live'"
    if start:
        where_days += " AND date(avs.snapshot_at) >= ?"
        params.append(start)
    elif days:
        where_days += " AND date(avs.snapshot_at) >= date('now', ?)"
        params.append(f"-{int(days)} days")
    rows = conn.execute(
        f"""
        WITH daily AS (
            SELECT
                date(avs.snapshot_at) AS d,
                avs.account_id,
                avs.total_value,
                ROW_NUMBER() OVER (
                    PARTITION BY date(avs.snapshot_at), avs.account_id
                    -- on a day with both, prefer the live snapshot over reconstructed
                    ORDER BY (avs.source = 'live') DESC, avs.snapshot_at DESC
                ) AS rn
            FROM account_value_snapshots avs
            JOIN accounts a ON a.id = avs.account_id
            WHERE a.is_paper = ? {where_days}
        )
        SELECT d, SUM(total_value) AS total_value
        FROM daily
        WHERE rn = 1
        GROUP BY d
        ORDER BY d
        """,
        params,
    ).fetchall()
    return [{"date": r["d"], "value": r["total_value"]} for r in rows]


def fetch_external_flows(conn, dates: list[str], is_paper: int,
                         account_id: str | None = None) -> list[float]:
    """
    Net external cashflow per day (CONTRIBUTION / DEPOSIT / WITHDRAWAL / TRANSFER),
    aligned to `dates` (0.0 where none). Used to flow-strip returns so deposits /
    withdrawals don't count as performance — shared by the TWR curve and the
    risk analytics.
    """
    if not dates:
        return []
    where = "WHERE a.is_paper = ?"
    params: list = [is_paper]
    if account_id:
        where += " AND act.account_id = ?"
        params.append(account_id)
    where += " AND date(act.trade_date) >= ? AND date(act.trade_date) <= ?"
    params += [dates[0], dates[-1]]
    placeholders = ",".join("?" * len(_EXTERNAL_FLOW_TYPES))
    where += f" AND act.type IN ({placeholders})"
    params += list(_EXTERNAL_FLOW_TYPES)

    rows = conn.execute(
        f"""
        SELECT date(act.trade_date) AS d, COALESCE(SUM(act.amount), 0) AS flow
        FROM activities act
        JOIN accounts a ON a.id = act.account_id
        {where}
        GROUP BY date(act.trade_date)
        """,
        params,
    ).fetchall()
    fbd = {r["d"]: r["flow"] for r in rows}
    return [fbd.get(d, 0.0) for d in dates]


def compute_twr_index(conn, series: list[dict], is_paper: int,
                      account_id: str | None = None) -> list[float]:
    """
    Time-weighted return index parallel to `series` (a daily equity series from
    fetch_daily_equity_series): flow-stripped daily returns compounded into a
    growth index (1.0 = flat). The math lives in analytics.py; this only fetches
    the flows and delegates.
    """
    if not series:
        return []
    dates = [p["date"] for p in series]
    values = [p["value"] for p in series]
    flows = fetch_external_flows(conn, dates, is_paper, account_id)
    returns = analytics.daily_returns(values, flows)
    return [float(x) for x in analytics.twr_index(returns)]


def fetch_aligned_benchmark_series(conn, symbol: str, dates: list[str]) -> list[float | None]:
    """
    Return one close per date in `dates`, carrying the most recent prior close
    forward across non-trading days. None for dates before the benchmark's first
    available close.
    """
    pricemap = {
        r["date"]: r["close"]
        for r in conn.execute(
            "SELECT date, close FROM benchmark_prices WHERE symbol = ?", (symbol,)
        ).fetchall()
    }
    bdates = sorted(pricemap)
    out: list[float | None] = []
    for d in dates:
        i = bisect.bisect_right(bdates, d) - 1
        out.append(pricemap[bdates[i]] if i >= 0 else None)
    return out


# ---------- transaction history & reconstruction ----------

# Activity types that don't map cleanly onto equity share counts: option events
# carry contract (not share) units, and mergers are share-for-share swaps the
# generic rule can't reverse. We skip their share math and report the residual.
_OPTION_MA_TYPES = {
    "OPTIONEXPIRATION", "OPTIONASSIGNMENT", "OPTIONEXERCISE", "MA",
}

# External cashflows for time-weighted return: money entering/leaving the
# account from outside (not internal trades, dividends, or fees, which are part
# of performance). Used to strip deposit/withdrawal effects out of return.
_EXTERNAL_FLOW_TYPES = ("CONTRIBUTION", "DEPOSIT", "WITHDRAWAL", "TRANSFER")

# Money-market / sweep funds hold a constant $1.00 NAV but have no Yahoo price.
# Value them at $1 instead of treating them as unpriceable (worth $0).
_CASH_EQUIVALENT_SYMBOLS = {
    "FDRXX", "SPAXX", "FZFXX", "SWVXX", "VMFXX", "SPRXX", "SNVXX", "SNAXX",
    "SCHFDX0", "VMRXX", "FNSXX",
}


def upsert_activity(conn, a: dict, account_id: str | None = None) -> None:
    """
    Insert one SnapTrade activity. Immutable, so existing rows are left as-is.

    `account_id` should be passed by the caller: the account-level activities
    endpoint returns activities WITHOUT a nested `account` object, so we can't
    rely on a['account']['id']. Falls back to the nested field when not given.
    """
    aid = a.get("id") or a.get("external_reference_id")
    acct_id = account_id or _safe(a, "account", "id")
    if not aid or not acct_id:
        return
    conn.execute(
        """
        INSERT INTO activities (
            id, account_id, type, symbol, trade_date, settlement_date,
            units, price, amount, fee, option_ticker, description
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            aid,
            acct_id,
            a.get("type") or "?",
            _safe(a, "symbol", "symbol"),
            a.get("trade_date"),
            a.get("settlement_date"),
            a.get("units"),
            a.get("price"),
            a.get("amount"),
            a.get("fee"),
            _safe(a, "option_symbol", "ticker"),
            a.get("description"),
        ),
    )


def _load_symbol_prices(symbol: str, start: str, retries: int = 2):
    """
    Return (closes, splits) for a symbol from Yahoo Finance:
      closes: {date 'YYYY-MM-DD' -> raw close}   (NOT dividend-adjusted)
      splits: [(date, ratio), ...]
    ({}, []) on failure / no data.
    """
    hist = splits = None
    for attempt in range(retries + 1):
        try:
            tk = yf.Ticker(symbol)
            hist = tk.history(start=start, auto_adjust=False)
            splits = tk.splits
            break
        except Exception:
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
            else:
                return {}, []
    if hist is None or hist.empty:
        return {}, []
    closes: dict[str, float] = {}
    for idx, row in hist.iterrows():
        try:
            c = float(row["Close"])
        except Exception:
            continue
        if c != c:  # NaN (halts / missing bars); SQLite would store it as NULL
            continue
        closes[idx.strftime("%Y-%m-%d")] = c
    sp: list[tuple[str, float]] = []
    try:
        for idx, ratio in splits.items():
            sp.append((idx.strftime("%Y-%m-%d"), float(ratio)))
    except Exception:
        pass
    return closes, sp


def reconstruct_account_history(conn, account_id: str, end_date: str | None = None) -> dict:
    """
    Reconstruct a daily equity curve for the period before live snapshots, by
    replaying this account's activities forward in today's split-adjusted share
    terms and valuing holdings with historical closes.

    Writes one `source='reconstructed'` row per trading day into
    account_value_snapshots (existing reconstructed rows for this account are
    cleared first). Returns a validation report; check holdings_residual and
    cash_residual to judge how trustworthy the curve is.
    """
    if end_date is None:
        end_date = datetime.now(timezone.utc).date().isoformat()

    current_positions = {
        r["symbol"]: r["units"]
        for r in conn.execute(
            "SELECT symbol, units FROM positions WHERE account_id = ?", (account_id,)
        ).fetchall()
    }
    acct = conn.execute(
        "SELECT cash FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    current_cash = (acct["cash"] if acct and acct["cash"] is not None else 0.0)

    acts = [
        dict(r) for r in conn.execute(
            "SELECT * FROM activities WHERE account_id = ? ORDER BY date(trade_date) ASC",
            (account_id,),
        ).fetchall()
    ]
    if not acts:
        return {"status": "no_activities", "account_id": account_id}

    start_date = (acts[0]["trade_date"] or end_date)[:10]

    symbols = {s for s in current_positions if s} | {
        a["symbol"] for a in acts if a["symbol"]
    }

    closes: dict[str, dict] = {}
    splits: dict[str, list] = {}
    unpriceable: list[str] = []
    for sym in symbols:
        if sym in _CASH_EQUIVALENT_SYMBOLS:
            continue  # priced at $1.00 in price_on; no history fetch needed
        c, sp = _load_symbol_prices(sym, start_date)
        if not c:
            unpriceable.append(sym)
            continue
        closes[sym] = c
        splits[sym] = sp
        for d, px in c.items():
            conn.execute(
                "INSERT INTO price_history (symbol, date, close) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol, date) DO UPDATE SET close = excluded.close",
                (sym, d, px),
            )

    def split_factor(sym: str, d: str) -> float:
        f = 1.0
        for sd, ratio in splits.get(sym, []):
            if sd > d:
                f *= ratio
        return f

    sorted_dates = {sym: sorted(closes[sym]) for sym in closes}

    def price_on(sym: str, d: str):
        if sym in _CASH_EQUIVALENT_SYMBOLS:
            return 1.0
        ds = sorted_dates.get(sym)
        if not ds:
            return None
        i = bisect.bisect_right(ds, d) - 1
        if i < 0:
            return None
        ref = ds[i]
        return closes[sym][ref] / split_factor(sym, ref)

    # Express each trade's units in today's split-adjusted terms.
    for a in acts:
        sym = a["symbol"]
        td = (a["trade_date"] or "")[:10]
        a["adj_units"] = (a["units"] or 0.0) * (split_factor(sym, td) if sym else 1.0)

    # Net in-window activity per symbol (today's split-adjusted terms).
    replayed_units: dict[str, float] = {}
    for a in acts:
        if a["symbol"] and a["type"] not in _OPTION_MA_TYPES:
            replayed_units[a["symbol"]] = replayed_units.get(a["symbol"], 0.0) + a["adj_units"]

    # Opening holdings: what must have been held at the window start to reconcile
    # to today's positions. This captures shares transferred in or acquired before
    # SnapTrade's transaction window — otherwise the curve understates them. Held-
    # since-2015 accumulation accounts get an opening near zero; transfer-funded
    # accounts get their transferred lots seeded at the window start.
    opening: dict[str, float] = {}
    for sym in set(current_positions) | set(replayed_units):
        op = current_positions.get(sym, 0.0) - replayed_units.get(sym, 0.0)
        if abs(op) > 1e-6:
            opening[sym] = op

    # Clear any prior reconstruction for this account.
    conn.execute(
        "DELETE FROM account_value_snapshots WHERE account_id = ? AND source = 'reconstructed'",
        (account_id,),
    )

    # Forward replay across trading days, valuing holdings each day.
    all_dates = sorted({
        d for sym in closes for d in closes[sym] if start_date <= d <= end_date
    })
    holdings: dict[str, float] = dict(opening)  # seed with pre-window holdings
    cash = current_cash - sum((a["amount"] or 0.0) for a in acts)  # cash before first activity
    ai, n = 0, len(acts)
    for D in all_dates:
        while ai < n and (acts[ai]["trade_date"] or "")[:10] <= D:
            a = acts[ai]
            ai += 1
            cash += (a["amount"] or 0.0)
            if a["symbol"] and a["type"] not in _OPTION_MA_TYPES:
                holdings[a["symbol"]] = holdings.get(a["symbol"], 0.0) + a["adj_units"]
        mv = 0.0
        for sym, u in holdings.items():
            px = price_on(sym, D)
            if px is not None:
                mv += u * px
        conn.execute(
            """
            INSERT INTO account_value_snapshots (
                snapshot_at, account_id, total_value, cash,
                long_market_value, short_market_value, source
            ) VALUES (?, ?, ?, ?, ?, ?, 'reconstructed')
            ON CONFLICT(snapshot_at, account_id) DO NOTHING
            """,
            (f"{D}T21:00:00+00:00", account_id, mv + cash, cash, None, None),
        )

    # Holdings now reconcile to current by construction (opening + replayed).
    # The honest caveat is opening lots we couldn't price: their value is still
    # missing from the early curve.
    opening_unpriceable = sorted(s for s in opening if s in unpriceable)

    return {
        "status": "ok",
        "account_id": account_id,
        "start_date": start_date,
        "end_date": end_date,
        "days_written": len(all_dates),
        "activities": n,
        "unpriceable_symbols": unpriceable,
        "skipped_option_ma": sum(1 for a in acts if a["type"] in _OPTION_MA_TYPES),
        # Pre-window holdings assumed held from start_date (transfers / old lots):
        "seeded_opening": {s: round(v, 4) for s, v in opening.items()},
        # Of those, the ones we can't price (value still missing from the curve):
        "opening_unpriceable": opening_unpriceable,
        "cash_residual": round(current_cash - cash, 2),  # should be ~0
    }
