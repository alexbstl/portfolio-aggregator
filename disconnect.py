"""
Lists all linked brokerage authorizations for the current user, and
optionally removes one. When removing, also cleans up the local SQLite
DB so the disconnected account stops appearing on the dashboard.

Snapshots (position_snapshots, account_value_snapshots) are preserved
so historical data for the account is retained.

Run with no args to list, or with a connection ID to delete that connection.
"""
import os
import sys
from dotenv import load_dotenv
from snaptrade_client import SnapTrade

from db import db

load_dotenv()

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)
user_id = os.environ["SNAPTRADE_USER_ID"]
user_secret = os.environ["SNAPTRADE_USER_SECRET"]

connections = client.connections.list_brokerage_authorizations(
    user_id=user_id, user_secret=user_secret,
).body

if len(sys.argv) < 2:
    print("Linked connections:\n")
    for c in connections:
        broker = c["brokerage"]["display_name"]
        cid = c["id"]
        created = c.get("created_date", "?")
        print(f"  {cid}  {broker:25s}  linked {created}")
    print("\nTo disconnect, run: python disconnect.py <connection_id>")
    sys.exit(0)

target_id = sys.argv[1]
match = next((c for c in connections if c["id"] == target_id), None)
if not match:
    print(f"No connection found with id {target_id}")
    sys.exit(1)

broker = match["brokerage"]["display_name"]
confirm = input(f"Disconnect {broker} ({target_id}) and clean up local DB? [y/N] ").strip().lower()
if confirm != "y":
    print("Aborted.")
    sys.exit(0)

# 1. Revoke the broker authorization on SnapTrade's side
print(f"Revoking SnapTrade authorization for {broker}...")
client.connections.remove_brokerage_authorization(
    authorization_id=target_id,
    user_id=user_id,
    user_secret=user_secret,
)

# 2. Clean up the local DB
#    - Delete positions for accounts under this connection
#    - Delete the accounts themselves
#    - Delete the connection row
#    - LEAVE position_snapshots and account_value_snapshots intact (history)
print("Cleaning up local database...")
with db() as conn:
    account_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM accounts WHERE connection_id = ?",
            (target_id,),
        ).fetchall()
    ]

    if account_ids:
        placeholders = ",".join("?" * len(account_ids))
        n_positions = conn.execute(
            f"DELETE FROM positions WHERE account_id IN ({placeholders})",
            account_ids,
        ).rowcount
        print(f"  removed {n_positions} positions")

        n_accounts = conn.execute(
            f"DELETE FROM accounts WHERE id IN ({placeholders})",
            account_ids,
        ).rowcount
        print(f"  removed {n_accounts} accounts")
    else:
        print("  no local accounts found for this connection")

    n_conn = conn.execute(
        "DELETE FROM connections WHERE id = ?",
        (target_id,),
    ).rowcount
    print(f"  removed {n_conn} connection")

print(f"\nDisconnected {broker}.")
print("Historical snapshots preserved in position_snapshots and account_value_snapshots.")
