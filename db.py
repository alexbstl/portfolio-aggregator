"""
SQLite schema and helpers for the portfolio aggregator.
USD-only for now. Single file, no migrations framework — if the schema
changes early on, just delete portfolio.db and re-sync.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import os

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
        SELECT p.*, a.name AS account_name, r.ref_price,
          CASE WHEN r.ref_price IS NOT NULL AND p.market_price IS NOT NULL
               THEN (p.market_price - r.ref_price) * p.units END AS day_change,
          CASE WHEN r.ref_price IS NOT NULL AND r.ref_price != 0 AND p.market_price IS NOT NULL
               THEN (p.market_price - r.ref_price) / r.ref_price END AS day_change_pct
        FROM positions p
        JOIN accounts a ON a.id = p.account_id
        LEFT JOIN ref_prices r ON r.account_id = p.account_id AND r.symbol = p.symbol
        WHERE a.is_paper = ?
        ORDER BY ABS(p.market_value) DESC
        """,
        (is_paper,),
    ).fetchall()


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
            (SELECT SUM(market_value) FROM positions WHERE account_id = ?), 0
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
