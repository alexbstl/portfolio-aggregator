"""
SQLite schema and helpers for the portfolio aggregator.
USD-only for now. Single file, no migrations framework — if the schema
changes early on, just delete portfolio.db and re-sync.
"""
import sqlite3
import bisect
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import os
import yfinance as yf

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


def refresh_prices(conn, symbols: list[str]):
    """
    Fetch current price + previous close from Yahoo Finance for each symbol
    and upsert into the `prices` table. Failures per-symbol are swallowed so
    a bad ticker doesn't abort the sync; the stale row (if any) stays in place.
    """
    if not symbols:
        return
    ts = now_iso()
    for symbol in symbols:
        try:
            fi = yf.Ticker(symbol).fast_info
            price = fi.last_price
            prev_close = fi.previous_close
        except Exception:
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


def fetch_daily_equity_series(conn, is_paper: int, days: int | None = None) -> list[dict]:
    """
    One portfolio total per calendar day: for each day, take each account's last
    snapshot and sum across accounts. Accounts with no snapshot on a given day are
    simply absent from that day's sum (correct for accounts added later).
    """
    where_days = ""
    params: list = [is_paper]
    if days:
        where_days = "AND date(avs.snapshot_at) >= date('now', ?)"
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
                    ORDER BY avs.snapshot_at DESC
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
