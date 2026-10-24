
import json
import getpass
import urllib.request
import urllib.error
from pathlib import Path
from dotenv import dotenv_values

# Load local Sandbox credentials
config = dotenv_values(Path(__file__).parent / ".env")

client_id = config.get("PLAID_CLIENT_ID")
secret = config.get("PLAID_SECRET")

if not client_id or not secret:
    raise SystemExit("Missing Plaid Sandbox credentials.")

# Never hardcode or print access tokens
access_token = getpass.getpass(
    "Enter the existing Sandbox access token: "
).strip()

if not access_token.startswith("access-sandbox-"):
    raise SystemExit("Only Sandbox tokens are allowed.")

payload = {
    "client_id": client_id,
    "secret": secret,
    "access_token": access_token
}

request = urllib.request.Request(
    "https://sandbox.plaid.com/sandbox/item/reset_login",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST"
)

try:
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)

        print("HTTP status:", response.status)
        print("Login reset:", result.get("reset_login"))
        print("Request ID:", result.get("request_id"))

except urllib.error.HTTPError as error:
    details = json.loads(error.read().decode())

    print("HTTP status:", error.code)
    print("Error code:", details.get("error_code"))
    print("Request ID:", details.get("request_id"))

except urllib.error.URLError:
    print("Network or connection error.")
