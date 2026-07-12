"""
PropertyGPT — Configuration
All secrets come from environment variables (or a .env file).
NEVER hardcode tokens in source files again — the old ones must be rotated.

Setup:
    pip install python-dotenv
    Create a .env file (and add .env to .gitignore):

        GEMINI_API_KEY=your_new_gemini_key
        WHATSAPP_TOKEN=your_new_whatsapp_token
        WHATSAPP_PHONE_NUMBER_ID=1267138306473242
        VERIFY_TOKEN=propertygpt_verify_123
        ZOHO_CLIENT_ID=             # from api-console.zoho.in (Self Client)
        ZOHO_CLIENT_SECRET=
        ZOHO_REFRESH_TOKEN=         # one-time: python zoho_auth.py <grant_code>
        AGENT_PHONE=91XXXXXXXXXX    # agent notified of bookings
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; plain env vars still work

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]   # required

# Vapi (telephony + STT + TTS)
VAPI_API_KEY          = os.environ.get("VAPI_API_KEY", "")
VAPI_ASSISTANT_ID     = os.environ.get("VAPI_ASSISTANT_ID", "")
VAPI_PHONE_NUMBER_ID  = os.environ.get("VAPI_PHONE_NUMBER_ID", "")

# --- CRM (pick one or both; leave blank to only store locally) ---
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL", "")  # optional Zapier/Pabbly bridge
# Zoho OAuth credentials (client id/secret/refresh token) live in zoho_auth.py
ZOHO_API_BASE   = os.environ.get("ZOHO_API_BASE", "https://www.zohoapis.in/crm/v2")

# --- Site visits ---
AGENT_PHONE   = os.environ.get("AGENT_PHONE", "")  # WhatsApp number of your sales agent
SITE_LAT      = 12.9698
SITE_LNG      = 77.7499
SITE_NAME     = "Sunrise Heights"
SITE_ADDRESS  = "Plot No. 47, EPIP Zone, Whitefield Main Road, Bangalore 560066"

# Reminders outside the 24h window need a pre-approved template.
# Create a Utility template in Meta Business Manager, then set its name here.
REMINDER_TEMPLATE_NAME = os.environ.get("REMINDER_TEMPLATE_NAME", "")  # e.g. "visit_reminder"
TEMPLATE_LANG          = os.environ.get("TEMPLATE_LANG", "en")

# Portal lead first-touch (outbound). Requires an approved template.
PORTAL_TEMPLATE_NAME   = os.environ.get("PORTAL_TEMPLATE_NAME", "")   # e.g. "portal_first_touch"
PORTAL_WEBHOOK_SECRET  = os.environ.get("PORTAL_WEBHOOK_SECRET", "")  # shared secret for Zoho -> us

SEND_URL  = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
MEDIA_URL = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/media"

# --- Dashboard ---
OFFICE_START  = int(os.environ.get("OFFICE_START", "9"))   # 9 AM
OFFICE_END    = int(os.environ.get("OFFICE_END", "19"))    # 7 PM
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "change_this_key")

# DATA_DIR: where mutable data lives. Locally: current folder.
# On Railway/Render: set DATA_DIR=/data and attach a persistent volume there,
# so the database and leads survive restarts and redeploys.
DATA_DIR      = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE       = os.path.join(DATA_DIR, "propertygpt.db")
CACHE_FILE    = os.path.join(DATA_DIR, "embeddings_cache.json")
LEADS_TXT     = os.path.join(DATA_DIR, "leads.txt")
DOC_FILE      = "property_doc.txt"      # ships with the code
MEDIA_CATALOG = "media_catalog.json"    # ships with the code
