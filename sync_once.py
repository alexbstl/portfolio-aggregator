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
    insert_account_value_snapshot,
)

load_dotenv()

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)
USER_ID = os.environ["SNAPTRADE_USER_ID"]
USER_SECRET = os.environ["SNAPTRADE_USER_SECRET"]


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
        for c in connections:
            cid = c["id"]
            name = c["brokerage"]["display_name"]
            try:
                client.connections.refresh_brokerage_authorization(
                    authorization_id=cid,
                    user_id=USER_ID,
                    user_secret=USER_SECRET,
                )
                print(f"    {name}: refresh requested")
            except Exception as e:
                print(f"    {name}: refresh error {e}")
        # Give brokers a moment to respond before we re-fetch
        print("  waiting 15s for brokers to respond...")
        time.sleep(15)


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

        for acct in accounts:
            acct_id = acct["id"]
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
                print(f"    positions error: {e}")
                positions = []

            print(f"    {len(positions)} positions")
            replace_positions(conn, acct_id, positions, snapshot_at,
                              snapshot_kind=snapshot_kind)
            insert_account_value_snapshot(conn, acct_id, snapshot_at)

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
                ROUND(COALESCE(SUM(CASE WHEN p.units > 0 THEN p.market_value END), 0), 2) AS long_mv,
                ROUND(COALESCE(SUM(CASE WHEN p.units < 0 THEN p.market_value END), 0), 2) AS short_mv
            FROM accounts a
            LEFT JOIN positions p ON p.account_id = a.id
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
