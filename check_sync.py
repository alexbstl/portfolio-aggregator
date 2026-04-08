import os
import json
from dotenv import load_dotenv
from snaptrade_client import SnapTrade

load_dotenv()

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)

accounts = client.account_information.list_user_accounts(
    user_id=os.environ["SNAPTRADE_USER_ID"],
    user_secret=os.environ["SNAPTRADE_USER_SECRET"],
).body

for a in accounts:
    name = a.get("name") or a.get("institution_name")
    sync_status = a.get("sync_status", {})
    holdings = sync_status.get("holdings", {})
    last = holdings.get("last_successful_sync")
    print(f"{name:30s}  last_holdings_sync: {last}")
