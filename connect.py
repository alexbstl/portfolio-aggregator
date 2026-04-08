"""
Generates a SnapTrade Connection Portal URL.
Open the printed URL in a browser, log into your broker, and the connection
will be saved against your SnapTrade user. Run this once per broker.
"""
import os
from dotenv import load_dotenv
from snaptrade_client import SnapTrade

load_dotenv()

client = SnapTrade(
    client_id=os.environ["SNAPTRADE_CLIENT_ID"],
    consumer_key=os.environ["SNAPTRADE_CONSUMER_KEY"],
)

response = client.authentication.login_snap_trade_user(
    user_id=os.environ["SNAPTRADE_USER_ID"],
    user_secret=os.environ["SNAPTRADE_USER_SECRET"],
)

print("\n--- Response ---")
print(response.body)
print("----------------\n")

url = response.body.get("redirectURI")
if url:
    print(f"Open this URL in your browser:\n\n{url}\n")
else:
    print("No redirectURI in response — check the output above.")
