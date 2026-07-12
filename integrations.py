"""
PropertyGPT — Lead storage + CRM push
-------------------------------------
Three layers, all fire on every captured lead:

1. SQLite (propertygpt.db)  — source of truth, deduped by phone.
2. leads.txt                — kept for backward compatibility with your current flow.
3. CRM push                 — whichever of these is configured:
     a) CRM_WEBHOOK_URL : a Zapier / Pabbly / Make "catch hook" URL.
        Zero-code path: hook -> your CRM (works with Zoho, HubSpot,
        LeadSquared, Salesforce, Google Sheets, anything).
     b) ZOHO_ACCESS_TOKEN : direct push to Zoho CRM Leads module.

Leads carry conversation context (last property questions asked), which is
far more valuable to your sales team than a bare name + phone.
"""
import sqlite3, json, re, requests
from datetime import datetime
from tzutil import now_ist
from config import CRM_WEBHOOK_URL, ZOHO_API_BASE, DB_FILE, LEADS_TXT
import zoho_auth

# ─────────────────────────────────────────────
# DB SETUP
# ─────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_FILE)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS leads (
        phone       TEXT PRIMARY KEY,          -- dedupe key
        name        TEXT,
        wa_number   TEXT,                      -- WhatsApp sender id (may differ from shared phone)
        notes       TEXT,                      -- conversation context summary
        crm_synced  INTEGER DEFAULT 0,
        created_at  TEXT,
        updated_at  TEXT
    );
    CREATE TABLE IF NOT EXISTS conversations (
        wa_number     TEXT PRIMARY KEY,
        first_seen    TEXT,
        last_seen     TEXT,
        message_count INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS bookings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        wa_number   TEXT,
        name        TEXT,
        phone       TEXT,
        slot_id     TEXT,
        slot_label  TEXT,
        slot_dt        TEXT,                   -- ISO datetime of the visit
        status         TEXT DEFAULT 'confirmed',-- confirmed | done | cancelled
        reminders_sent TEXT DEFAULT '',         -- offsets already sent, e.g. "24,2"
        created_at     TEXT
    );
    """)
    con.commit()
    con.close()

init_db()

def log_conversation(wa_number):
    """Call once per incoming message: tracks unique enquiries + volume."""
    now = now_ist().isoformat(timespec="seconds")
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        INSERT INTO conversations (wa_number, first_seen, last_seen, message_count)
        VALUES (?,?,?,1)
        ON CONFLICT(wa_number) DO UPDATE SET
            last_seen=excluded.last_seen,
            message_count=message_count+1
    """, (wa_number, now, now))
    con.commit(); con.close()

# ─────────────────────────────────────────────
# PHONE VALIDATION (fixes: old code saved ANY text as phone)
# ─────────────────────────────────────────────

def normalize_phone(raw):
    """Return a clean 10-15 digit phone string, or None if invalid."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if 10 <= len(digits) <= 15:
        return digits
    return None

# ─────────────────────────────────────────────
# LEAD SAVE (SQLite + leads.txt + CRM)
# ─────────────────────────────────────────────

def save_lead(name, phone, wa_number="", notes=""):
    """Upsert lead locally, append to leads.txt, then push to CRM.
    Returns 'created', 'updated', or 'invalid_phone'."""
    phone = normalize_phone(phone)
    if not phone:
        return "invalid_phone"

    now = now_ist().isoformat(timespec="seconds")
    con = sqlite3.connect(DB_FILE)
    cur = con.execute("SELECT phone FROM leads WHERE phone=?", (phone,))
    exists = cur.fetchone() is not None

    if exists:
        con.execute(
            "UPDATE leads SET name=?, notes=notes || ' | ' || ?, updated_at=? WHERE phone=?",
            (name, notes, now, phone))
        result = "updated"
    else:
        con.execute(
            "INSERT INTO leads (phone, name, wa_number, notes, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (phone, name, wa_number, notes, now, now))
        result = "created"
        # keep your existing leads.txt in sync (new leads only, no dupes)
        with open(LEADS_TXT, "a") as f:
            f.write(f"Name: {name}, Phone: {phone}\n")

    con.commit()
    con.close()

    synced = push_lead_to_crm(name, phone, notes)
    if synced:
        con = sqlite3.connect(DB_FILE)
        con.execute("UPDATE leads SET crm_synced=1 WHERE phone=?", (phone,))
        con.commit(); con.close()

    print(f"[LEAD:{result}] {name} / {phone} / crm_synced={synced}")
    return result

# ─────────────────────────────────────────────
# CRM PUSH
# ─────────────────────────────────────────────

def push_lead_to_crm(name, phone, notes=""):
    """Push to whichever CRM path is configured. Returns True if any succeeded."""
    ok = False
    if CRM_WEBHOOK_URL:
        ok = _push_via_webhook(name, phone, notes) or ok
    if zoho_auth.zoho_configured():
        ok = _push_to_zoho(name, phone, notes) or ok
    return ok

def _push_via_webhook(name, phone, notes):
    """Zapier / Pabbly / Make catch-hook: they receive this JSON and map it
    to any CRM's 'create lead' action in their UI. Easiest path to go live."""
    try:
        r = requests.post(CRM_WEBHOOK_URL, json={
            "name": name,
            "phone": phone,
            "source": "whatsapp_bot",
            "property": "Sunrise Heights",
            "notes": notes,
            "captured_at": now_ist().isoformat(timespec="seconds"),
        }, timeout=10)
        return r.status_code in (200, 201, 202)
    except requests.RequestException as e:
        print(f"[CRM webhook ERROR] {e}")
        return False

def _push_to_zoho(name, phone, notes):
    """Direct Zoho CRM push with dedupe: search by phone first, update if found.
    Uses zoho_auth for automatic access-token refresh (tokens live 1 hour)."""
    try:
        headers = {"Authorization": f"Zoho-oauthtoken {zoho_auth.get_access_token()}"}
        # 1. dedupe check
        s = requests.get(f"{ZOHO_API_BASE}/Leads/search",
                         params={"phone": phone}, headers=headers, timeout=10)
        existing = None
        if s.status_code == 200 and s.text.strip():
            data = s.json().get("data") or []
            if data:
                existing = data[0]["id"]

        record = {
            "Last_Name": name or "WhatsApp Lead",
            "Phone": phone,
            "Lead_Source": "WhatsApp Bot",
            "Description": notes,
            "Company": "Sunrise Heights Enquiry",  # Zoho requires Company for Leads
        }
        if existing:
            r = requests.put(f"{ZOHO_API_BASE}/Leads",
                             json={"data": [{**record, "id": existing}]},
                             headers=headers, timeout=10)
        else:
            r = requests.post(f"{ZOHO_API_BASE}/Leads",
                              json={"data": [record]}, headers=headers, timeout=10)
        return r.status_code in (200, 201, 202)
    except (requests.RequestException, RuntimeError) as e:
        print(f"[Zoho ERROR] {e}")
        return False


# ─────────────────────────────────────────────
# FULL RESET (for demos / re-testing a number)
# ─────────────────────────────────────────────

def delete_lead_from_zoho(phone):
    """Find and delete a lead in Zoho by phone. Returns count deleted."""
    import zoho_auth
    if not zoho_auth.zoho_configured():
        return 0
    phone = normalize_phone(phone)
    if not phone:
        return 0
    deleted = 0
    try:
        headers = {"Authorization": f"Zoho-oauthtoken {zoho_auth.get_access_token()}"}
        # search by the last 10 digits (how numbers are usually stored)
        for term in {phone, phone[-10:]}:
            r = requests.get(f"{ZOHO_API_BASE}/Leads/search",
                             params={"phone": term}, headers=headers, timeout=10)
            if r.status_code == 200 and r.text.strip():
                for rec in (r.json().get("data") or []):
                    d = requests.delete(f"{ZOHO_API_BASE}/Leads/{rec['id']}",
                                        headers=headers, timeout=10)
                    if d.status_code in (200, 202):
                        deleted += 1
                        print(f"[RESET] Deleted Zoho lead {rec['id']}")
    except Exception as e:
        print(f"[RESET Zoho ERROR] {e}")
    return deleted


def reset_contact(phone_or_wa, include_zoho=True):
    """Erase a person from everywhere so they're treated as a brand-new lead:
    leads, bookings, conversations, portal_leads (SQLite), leads.txt, and Zoho."""
    digits = normalize_phone(phone_or_wa) or ""
    if not digits:
        return {"error": "invalid phone"}
    last10 = digits[-10:]
    result = {"phone": digits}

    con = sqlite3.connect(DB_FILE)
    # match on any stored variant (with/without country code)
    like = f"%{last10}"
    result["leads"] = con.execute(
        "DELETE FROM leads WHERE phone LIKE ? OR wa_number LIKE ?", (like, like)).rowcount
    result["bookings"] = con.execute(
        "DELETE FROM bookings WHERE phone LIKE ? OR wa_number LIKE ?", (like, like)).rowcount
    result["conversations"] = con.execute(
        "DELETE FROM conversations WHERE wa_number LIKE ?", (like,)).rowcount
    try:
        result["portal_leads"] = con.execute(
            "DELETE FROM portal_leads WHERE phone LIKE ?", (like,)).rowcount
    except Exception:
        result["portal_leads"] = 0
    con.commit(); con.close()

    # scrub leads.txt
    try:
        import os
        if os.path.exists(LEADS_TXT):
            with open(LEADS_TXT) as f:
                lines = f.readlines()
            kept = [l for l in lines if last10 not in l]
            with open(LEADS_TXT, "w") as f:
                f.writelines(kept)
            result["leads_txt"] = len(lines) - len(kept)
    except Exception as e:
        print(f"[RESET leads.txt ERROR] {e}")

    if include_zoho:
        result["zoho"] = delete_lead_from_zoho(digits)

    print(f"[RESET] {digits} -> {result}")
    return result
