"""
One-shot script: registers you as a SnapTrade user and prints your userSecret.
Run this exactly once. Save the printed userSecret into .env as SNAPTRADE_USER_SECRET.
"""
import os
from dotenv import load_dotenv
from snaptrade_client import SnapTrade

load_dotenv()

client_id = os.environ["SNAPTRADE_CLIENT_ID"]
consumer_key = os.environ["SNAPTRADE_CONSUMER_KEY"]
user_id = os.environ["SNAPTRADE_USER_ID"]

client = SnapTrade(client_id=client_id, consumer_key=consumer_key)

print(f"Registering SnapTrade user: {user_id!r}")
response = client.authentication.register_snap_trade_user(user_id=user_id)

print("\n--- Response ---")
print(response.body)
print("----------------\n")

user_secret = response.body.get("userSecret")
if user_secret:
    print(f"SUCCESS. Add this to your .env file:\n")
    print(f"SNAPTRADE_USER_SECRET={user_secret}\n")
else:
    print("No userSecret in response — check the output above.")
