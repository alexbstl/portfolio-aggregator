"""
Pulls all linked accounts and their positions from SnapTrade.
Dumps everything as JSON to stdout so we can see the actual shape.
"""
import os
import json
from dotenv import load_dotenv
from snaptrade_client import SnapTrade

load_dotenv()

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)

user_id = os.environ["SNAPTRADE_USER_ID"]
user_secret = os.environ["SNAPTRADE_USER_SECRET"]


def dump(label, data):
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    print(json.dumps(data, indent=2, default=str))


# 1. List all connected brokerage authorizations (one per broker linked)
print("Fetching connections...")
connections = client.connections.list_brokerage_authorizations(
    user_id=user_id,
    user_secret=user_secret,
).body
dump("CONNECTIONS", connections)

# 2. List all accounts across all connections
print("\nFetching accounts...")
accounts = client.account_information.list_user_accounts(
    user_id=user_id,
    user_secret=user_secret,
).body
dump("ACCOUNTS", accounts)

# 3. For each account, pull positions, option positions, and balances
for acct in accounts:
    acct_id = acct["id"]
    acct_name = acct.get("name") or acct.get("institution_name") or acct_id
    print(f"\n\n>>> Account: {acct_name} ({acct_id})")

    try:
        positions = client.account_information.get_user_account_positions(
            user_id=user_id,
            user_secret=user_secret,
            account_id=acct_id,
        ).body
        dump(f"POSITIONS — {acct_name}", positions)
    except Exception as e:
        print(f"  positions error: {e}")

    try:
        options = client.options.list_option_holdings(
            user_id=user_id,
            user_secret=user_secret,
            account_id=acct_id,
        ).body
        dump(f"OPTION POSITIONS — {acct_name}", options)
    except Exception as e:
        print(f"  options error: {e}")

    try:
        balances = client.account_information.get_user_account_balance(
            user_id=user_id,
            user_secret=user_secret,
            account_id=acct_id,
        ).body
        dump(f"BALANCES — {acct_name}", balances)
    except Exception as e:
        print(f"  balances error: {e}")
