"""
One-shot: deletes the current SnapTrade user. After this runs, all linked
broker connections under this user are gone and the userSecret is invalid.
"""
import os
from dotenv import load_dotenv
from snaptrade_client import SnapTrade

load_dotenv()

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)

user_id = os.environ["SNAPTRADE_USER_ID"]
user_secret = os.environ["SNAPTRADE_USER_SECRET"]

print(f"Deleting SnapTrade user: {user_id!r}")
response = client.authentication.delete_snap_trade_user(user_id=user_id)
print(response.body)
print("Done. The userSecret in your .env is now invalid.")
