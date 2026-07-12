"""
Zoho CRM OAuth — automatic token refresh
-----------------------------------------
Zoho access tokens die every hour. This module holds a long-lived REFRESH
token and mints fresh access tokens automatically, caching each one until
~5 minutes before expiry. The rest of the code just calls get_access_token().

ONE-TIME SETUP (10 minutes):
1. Go to https://api-console.zoho.in  →  Add Client  →  "Self Client"
   (use .in since your Zoho account is on the India DC; if your CRM URL is
   zoho.com use api-console.zoho.com and change ZOHO_ACCOUNTS_URL below)
2. Copy the Client ID and Client Secret into .env
3. In the Self Client's "Generate Code" tab:
      Scope:    ZohoCRM.modules.leads.ALL
      Duration: 10 minutes
   Click Create → copy the grant code shown.
4. Exchange it for a refresh token (do this within 10 min):
      python zoho_auth.py <paste_grant_code_here>
   This prints your refresh token → put it in .env as ZOHO_REFRESH_TOKEN.
   Refresh tokens don't expire; you do this once.

.env additions:
    ZOHO_CLIENT_ID=1000.XXXXXXXX
    ZOHO_CLIENT_SECRET=xxxxxxxxxxxx
    ZOHO_REFRESH_TOKEN=1000.xxxxxxxx.xxxxxxxx
    ZOHO_ACCOUNTS_URL=https://accounts.zoho.in     # or .com / .eu
"""
import os, sys, time, requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ZOHO_CLIENT_ID     = os.environ.get("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN", "")
ZOHO_ACCOUNTS_URL  = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")

_cache = {"token": None, "expires_at": 0}

def zoho_configured():
    return bool(ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN)

def get_access_token():
    """Return a valid access token, refreshing if expired (thread-safe enough
    for this workload: worst case two refreshes race, both succeed)."""
    if _cache["token"] and time.time() < _cache["expires_at"]:
        return _cache["token"]
    r = requests.post(f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token", data={
        "grant_type":    "refresh_token",
        "client_id":     ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "refresh_token": ZOHO_REFRESH_TOKEN,
    }, timeout=10)
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"Zoho refresh failed: {data}")
    _cache["token"] = data["access_token"]
    # expires_in is seconds (usually 3600); refresh 5 min early
    _cache["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 300
    print("[ZOHO] Access token refreshed.")
    return _cache["token"]

def exchange_grant_code(grant_code):
    """One-time: grant code -> refresh token. Run: python zoho_auth.py <code>"""
    r = requests.post(f"{ZOHO_ACCOUNTS_URL}/oauth/v2/token", data={
        "grant_type":    "authorization_code",
        "client_id":     ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "code":          grant_code,
    }, timeout=10)
    data = r.json()
    if "refresh_token" not in data:
        print(f"FAILED: {data}\n(Grant codes expire in 10 min — regenerate and retry. "
              f"Also check ZOHO_ACCOUNTS_URL matches your DC: .in vs .com)")
        sys.exit(1)
    print("\nSUCCESS! Add this line to your .env:\n")
    print(f"ZOHO_REFRESH_TOKEN={data['refresh_token']}\n")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    exchange_grant_code(sys.argv[1])
