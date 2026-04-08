"""
Forces SnapTrade to re-fetch data from all linked brokers, then waits
briefly and shows the updated last_holdings_sync timestamps.
"""
import os
import time
from dotenv import load_dotenv
from snaptrade_client import SnapTrade

load_dotenv()

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)
user_id = os.environ["SNAPTRADE_USER_ID"]
user_secret = os.environ["SNAPTRADE_USER_SECRET"]

print("Fetching connections...")
connections = client.connections.list_brokerage_authorizations(
    user_id=user_id, user_secret=user_secret,
).body

print(f"Found {len(connections)} connections. Forcing refresh on each...\n")

for c in connections:
    cid = c["id"]
    name = c["brokerage"]["display_name"]
    try:
        result = client.connections.refresh_brokerage_authorization(
            authorization_id=cid,
            user_id=user_id,
            user_secret=user_secret,
        )
        print(f"  {name}: refresh requested")
    except Exception as e:
        print(f"  {name}: ERROR {e}")

print("\nWaiting 15 seconds for brokers to respond...")
time.sleep(15)

print("\nFresh account timestamps:")
accounts = client.account_information.list_user_accounts(
    user_id=user_id, user_secret=user_secret,
).body

for a in accounts:
    name = (a.get("name") or a.get("institution_name") or "")[:40]
    last = (a.get("sync_status") or {}).get("holdings", {}).get("last_successful_sync")
    print(f"  {name:40s}  {last}")
