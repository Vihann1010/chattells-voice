"""
PropertyGPT Voice — Configuration
All secrets come from environment variables (or a .env file).

.env / Render environment:
    GEMINI_API_KEY=...
    VAPI_API_KEY=...              # Vapi PRIVATE key
    VAPI_ASSISTANT_ID=...
    VAPI_PHONE_NUMBER_ID=...
    AGENT_PHONE=91XXXXXXXXXX      # for call transfers
    DASHBOARD_KEY=...             # protects /call and /calls
    ZOHO_CLIENT_ID=...            # same Zoho app as the WhatsApp bot
    ZOHO_CLIENT_SECRET=...
    ZOHO_REFRESH_TOKEN=...
    ZOHO_ACCOUNTS_URL=https://accounts.zoho.in
    ZOHO_API_BASE=https://www.zohoapis.in/crm/v2
"""
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- LLM ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]      # required

# --- Vapi (telephony + STT + TTS) ---
VAPI_API_KEY         = os.environ.get("VAPI_API_KEY", "")
VAPI_ASSISTANT_ID    = os.environ.get("VAPI_ASSISTANT_ID", "")
VAPI_PHONE_NUMBER_ID = os.environ.get("VAPI_PHONE_NUMBER_ID", "")

# --- Property ---
SITE_NAME    = "Sunrise Heights"
SITE_ADDRESS = "Plot No. 47, EPIP Zone, Whitefield Main Road, Bangalore 560066"

# --- Agent (call transfers) ---
AGENT_PHONE = os.environ.get("AGENT_PHONE", "")

# --- Access ---
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "change_this_key")

# --- CRM ---
CRM_WEBHOOK_URL = os.environ.get("CRM_WEBHOOK_URL", "")
ZOHO_API_BASE   = os.environ.get("ZOHO_API_BASE", "https://www.zohoapis.in/crm/v2")

# --- Storage ---
# On Render/Railway set DATA_DIR=/data with a persistent volume attached.
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE   = os.path.join(DATA_DIR, "voicecaller.db")
LEADS_TXT = os.path.join(DATA_DIR, "leads.txt")
DOC_FILE  = "property_doc.txt"     # ships with the code
