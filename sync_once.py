"""
Pulls all accounts + positions from SnapTrade and writes them to SQLite.
Run this manually for now: `python sync_once.py`
This is the function that will eventually be called on a schedule by the webapp.
"""
import os
import time
from dotenv import load_dotenv
from snaptrade_client import SnapTrade

from db import (
    init_db,
    db,
    now_iso,
    upsert_connection,
    upsert_account,
    replace_positions,
    refresh_prices,
    refresh_benchmarks,
    upsert_activity,
    recompute_account_total,
    insert_account_value_snapshot,
)

load_dotenv()

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)
USER_ID = os.environ["SNAPTRADE_USER_ID"]
USER_SECRET = os.environ["SNAPTRADE_USER_SECRET"]


def _install_snaptrade_timeout(snaptrade, connect_s: float = 10, read_s: float = 60):
    """
    The SDK's typed wrappers don't expose per-request timeouts and its REST
    layer defaults to no timeout at all, so one stalled connection blocks the
    sync thread forever. Wrap the shared REST client (all tag APIs use the
    same one) to inject a default (connect, read) timeout; calls that already
    pass their own keep it.
    """
    rest = snaptrade.account_information.api_client.rest_client
    orig_request = rest.request

    def request_with_timeout(*args, **kwargs):
        if not kwargs.get("timeout"):
            kwargs["timeout"] = (connect_s, read_s)
        return orig_request(*args, **kwargs)

    rest.request = request_with_timeout


_install_snaptrade_timeout(client)


def fetch_connections():
    return client.connections.list_brokerage_authorizations(
        user_id=USER_ID, user_secret=USER_SECRET
    ).body


def fetch_accounts():
    return client.account_information.list_user_accounts(
        user_id=USER_ID, user_secret=USER_SECRET
    ).body


def fetch_positions(account_id: str):
    return client.account_information.get_user_account_positions(
        user_id=USER_ID, user_secret=USER_SECRET, account_id=account_id
    ).body


def fetch_balances(account_id: str):
    return client.account_information.get_user_account_balance(
        user_id=USER_ID, user_secret=USER_SECRET, account_id=account_id
    ).body


def fetch_account_activities(account_id: str, page_size: int = 1000) -> list[dict]:
    """
    Page through the account-level activities endpoint (the non-deprecated
    replacement for transactions_and_reporting.get_activities) and return all
    activity dicts for the account.
    """
    out: list[dict] = []
    offset = 0
    while True:
        resp = client.account_information.get_account_activities(
            user_id=USER_ID,
            user_secret=USER_SECRET,
            account_id=account_id,
            offset=offset,
            limit=page_size,
        ).body
        # Response may be a bare list or a paginated object {data: [...]}.
        if isinstance(resp, dict):
            items = resp.get("data") or resp.get("activities") or []
        else:
            items = resp or []
        out.extend(items)
        if len(items) < page_size:
            break
        offset += page_size
    return out


def sync_activities(conn, account_ids: list[str]) -> int:
    """Ingest transaction history for each account into the activities table."""
    total = 0
    for acct_id in account_ids:
        try:
            acts = fetch_account_activities(acct_id)
        except Exception as e:
            print(f"  activities error for {acct_id}: {e}")
            continue
        for a in acts:
            try:
                upsert_activity(conn, a, acct_id)
            except Exception:
                continue
        total += len(acts)
        print(f"  activities: {acct_id} -> {len(acts)}")
    return total


def prune_orphaned(conn, live_connection_ids: set[str], live_account_ids: set[str]) -> None:
    """
    Remove DB rows for connections/accounts that no longer exist on SnapTrade.
    Snapshots are preserved (they live in separate tables and aren't FK-bound).
    """
    db_conn_ids = {
        row["id"] for row in conn.execute("SELECT id FROM connections").fetchall()
    }
    orphan_connections = db_conn_ids - live_connection_ids

    db_acct_ids = {
        row["id"] for row in conn.execute("SELECT id FROM accounts").fetchall()
    }
    orphan_accounts = db_acct_ids - live_account_ids

    if not orphan_connections and not orphan_accounts:
        return

    if orphan_accounts:
        placeholders = ",".join("?" * len(orphan_accounts))
        ids = list(orphan_accounts)
        n_pos = conn.execute(
            f"DELETE FROM positions WHERE account_id IN ({placeholders})", ids
        ).rowcount
        n_acct = conn.execute(
            f"DELETE FROM accounts WHERE id IN ({placeholders})", ids
        ).rowcount
        print(f"  pruned {n_acct} orphaned accounts ({n_pos} positions)")

    if orphan_connections:
        placeholders = ",".join("?" * len(orphan_connections))
        n_conn = conn.execute(
            f"DELETE FROM connections WHERE id IN ({placeholders})",
            list(orphan_connections),
        ).rowcount
        print(f"  pruned {n_conn} orphaned connections")


def run_sync(force: bool = False, snapshot_kind: str | None = None):
    snapshot_at = now_iso()
    
    print(f"[{snapshot_at}] Starting sync...")
    mode = "FORCED" if force else "cached"
    print(f"[{snapshot_at}] Starting sync ({mode})...")
    
    init_db()

    print("  fetching connections...")
    connections = fetch_connections()

    if force and connections:
        print(f"  forcing broker refresh on {len(connections)} connection(s)...")
        any_refreshed = False
        for c in connections:
            cid = c["id"]
            name = c["brokerage"]["display_name"]
            try:
                client.connections.refresh_brokerage_authorization(
                    authorization_id=cid,
                    user_id=USER_ID,
                    user_secret=USER_SECRET,
                )
                any_refreshed = True
                print(f"    {name}: refresh requested")
            except Exception as e:
                print(f"    {name}: refresh error {e}")
        # Only wait if a refresh was actually accepted. On a real-time SnapTrade
        # plan, manual refresh is forbidden (403/1141) and pointless — the data
        # endpoints already return live data — so skip the wait entirely.
        if any_refreshed:
            print("  waiting 15s for brokers to respond...")
            time.sleep(15)
        else:
            print("  manual refresh unavailable (real-time plan) — skipping wait")


    print("  fetching accounts...")
    accounts = fetch_accounts()

    # Build sets of IDs that SnapTrade currently considers live, so we can
    # detect and prune anything in the DB that's no longer present.
    live_connection_ids = {c["id"] for c in connections}
    live_account_ids = {a["id"] for a in accounts}

    # Safety guard: if BOTH lists came back empty, that's almost certainly a
    # transient API failure rather than "the user has deleted everything." Skip
    # pruning rather than wiping the DB clean. The next successful sync will
    # prune correctly.
    should_prune = bool(connections) or bool(accounts)

    with db() as conn:
        for c in connections:
            upsert_connection(conn, c)

        # Pass 1: sync account metadata + positions from broker
        acct_ids = []
        for acct in accounts:
            acct_id = acct["id"]
            acct_ids.append(acct_id)
            acct_label = acct.get("name") or acct.get("institution_name") or acct_id
            print(f"  account: {acct_label}")

            try:
                balances = fetch_balances(acct_id)
            except Exception as e:
                print(f"    balances error: {e}")
                balances = []

            # Use first USD balance row (we're USD-only for now)
            balance_row = next(
                (b for b in balances if (b.get("currency") or {}).get("code") == "USD"),
                balances[0] if balances else None,
            )

            upsert_account(conn, acct, balance_row)

            try:
                positions = fetch_positions(acct_id)
            except Exception as e:
                # A transient fetch failure must NOT wipe the account's positions.
                # Doing so would recompute total_value as cash-only and write a
                # false dip into the equity curve. Keep the prior positions and
                # skip this account's snapshot for this cycle instead.
                print(f"    positions error: {e} — keeping previous positions, skipping snapshot")
                positions = None

            if positions is not None:
                print(f"    {len(positions)} positions")
                replace_positions(conn, acct_id, positions, snapshot_at,
                                  snapshot_kind=snapshot_kind)

        # Refresh external prices for all live symbols before recomputing totals
        symbols = [
            row[0] for row in
            conn.execute("SELECT DISTINCT symbol FROM positions").fetchall()
        ]
        print(f"  refreshing prices for {len(symbols)} symbol(s) via yfinance...")
        refresh_prices(conn, symbols)

        # Pass 2: recompute account totals (uses external prices) + snapshot
        for acct_id in acct_ids:
            recompute_account_total(conn, acct_id)
            insert_account_value_snapshot(conn, acct_id, snapshot_at)

        # Benchmark history and transaction activities are daily data — refresh
        # them once a day on the pre_open job rather than every 15-minute sync.
        if snapshot_kind == "pre_open":
            print("  refreshing benchmark history...")
            refresh_benchmarks(conn)
            print("  ingesting transaction activities...")
            sync_activities(conn, acct_ids)

        if should_prune:
            prune_orphaned(conn, live_connection_ids, live_account_ids)

    # Print a summary so we can see what landed
    print("\n--- Summary ---")
    with db() as conn:
        for row in conn.execute(
            """
            SELECT
                a.name,
                a.total_value,
                a.cash,
                COUNT(p.symbol) AS n_positions,
                SUM(CASE WHEN p.units > 0 THEN 1 ELSE 0 END) AS n_long,
                SUM(CASE WHEN p.units < 0 THEN 1 ELSE 0 END) AS n_short,
                ROUND(COALESCE(SUM(CASE WHEN p.units > 0
                    THEN COALESCE(pr.price, p.market_price) * p.units END), 0), 2) AS long_mv,
                ROUND(COALESCE(SUM(CASE WHEN p.units < 0
                    THEN COALESCE(pr.price, p.market_price) * p.units END), 0), 2) AS short_mv
            FROM accounts a
            LEFT JOIN positions p ON p.account_id = a.id
            LEFT JOIN prices pr ON pr.symbol = p.symbol
            GROUP BY a.id
            """
        ):
            print(
                f"  {row['name']}: total={row['total_value'] or 0:.2f} cash={row['cash'] or 0:.2f} "
                f"positions={row['n_positions']} ({row['n_long'] or 0} long / {row['n_short'] or 0} short) "
                f"long_mv={row['long_mv'] or 0:.2f} short_mv={row['short_mv'] or 0:.2f}"
            )
    print("Done.")


if __name__ == "__main__":
    run_sync()
