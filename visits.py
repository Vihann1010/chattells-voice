"""
PropertyGPT — Site visit booking + reminders
--------------------------------------------
- generate_slots(): next 4 days of visit slots (11 AM & 4 PM, IST)
- book_visit(): store booking, notify agent
- reminder_loop(): background thread; sends 24h and 2h reminders

IMPORTANT — WhatsApp 24-hour rule:
Free-form messages can only be sent within 24h of the user's LAST message.
The 2h reminder usually falls outside that window, so it must be a
pre-approved Utility TEMPLATE. Create one in Meta Business Manager, e.g.:

  Name: visit_reminder   Category: Utility   Language: en
  Body: "Reminder: your site visit to {{1}} is scheduled for {{2}}.
         Location: {{3}}. Reply here if you need to reschedule."

then set REMINDER_TEMPLATE_NAME=visit_reminder in .env.
Until it's approved, reminders are attempted as plain text (works only
inside the 24h window) and failures are logged, not fatal.
"""
import sqlite3, threading, time
from datetime import datetime, timedelta
from tzutil import now_ist
from config import (DB_FILE, AGENT_PHONE, SITE_NAME, SITE_ADDRESS,
                    REMINDER_TEMPLATE_NAME, TEMPLATE_LANG)

# sending functions are injected by whatsapp_backend to avoid circular import
_send_text = None
_send_template = None
_send_location = None

def wire_senders(send_text, send_template, send_location):
    global _send_text, _send_template, _send_location
    _send_text, _send_template, _send_location = send_text, send_template, send_location

def _ensure_columns():
    """Add reminders_sent column to older bookings tables if missing."""
    try:
        con = sqlite3.connect(DB_FILE)
        cols = [r[1] for r in con.execute("PRAGMA table_info(bookings)").fetchall()]
        if "reminders_sent" not in cols:
            con.execute("ALTER TABLE bookings ADD COLUMN reminders_sent TEXT DEFAULT ''")
            con.commit()
        con.close()
    except Exception as e:
        print(f"[MIGRATION] {e}")

# ─────────────────────────────────────────────
# SLOTS
# ─────────────────────────────────────────────

def generate_slots(days=7):
    """Next `days` days, two slots per day (11:00, 16:00). WhatsApp list
    messages allow up to 10 rows; we show the first 8 and rely on free-typed
    dates (parse_freeform_slot) for anything further out."""
    slots = []
    today = now_ist()
    for d in range(1, days + 1):
        day = today + timedelta(days=d)
        for hour in (11, 16):
            dt = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            slot_id = dt.strftime("slot_%Y%m%d_%H%M")
            label   = dt.strftime("%a %d %b, %I:%M %p")   # "Thu 09 Jul, 11:00 AM"
            slots.append({"id": slot_id, "label": label, "dt": dt.isoformat()})
    return slots[:8]

def generate_slots_for_month(month, year, count=8):
    """Slots inside a specific month (used when user says 'in August').
    Starts from tomorrow if it's the current month, else the 1st."""
    now = now_ist()
    if month == now.month and year == now.year:
        start_day = now.day + 1
    else:
        start_day = 1
    slots = []
    d = start_day
    while len(slots) < count and d <= 28:      # 28 keeps it valid for all months
        try:
            base = datetime(year, month, d)
        except ValueError:
            break
        for hour in (11, 16):
            dt = base.replace(hour=hour)
            if dt > now:
                slots.append({"id": dt.strftime("slot_%Y%m%d_%H%M"),
                              "label": dt.strftime("%a %d %b, %I:%M %p"),
                              "dt": dt.isoformat()})
            if len(slots) >= count:
                break
        d += 1
    return slots

def slot_from_id(slot_id):
    """Rebuild datetime + label from a slot_id like slot_20260709_1100."""
    try:
        dt = datetime.strptime(slot_id, "slot_%Y%m%d_%H%M")
        return {"id": slot_id, "label": dt.strftime("%a %d %b, %I:%M %p"),
                "dt": dt.isoformat()}
    except ValueError:
        return None


# ─────────────────────────────────────────────
# FREE-TEXT DATE/TIME PARSING
# Handles: "next monday", "tomorrow 4pm", "15th", "after 3 days",
# "this weekend", "sat 4pm", "20 July 11am", etc.
# ─────────────────────────────────────────────

_WEEKDAYS = {"monday":0,"mon":0,"tuesday":1,"tue":1,"tues":1,"wednesday":2,"wed":2,
             "thursday":3,"thu":3,"thurs":3,"friday":4,"fri":4,"saturday":5,"sat":5,
             "sunday":6,"sun":6}
_MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,
           "sep":9,"sept":9,"oct":10,"nov":11,"dec":12}
VISIT_OPEN, VISIT_CLOSE = 10, 18   # accept visit times between 10 AM and 6 PM

def _next_weekday(base, target_wd):
    days_ahead = (target_wd - base.weekday() + 7) % 7
    days_ahead = days_ahead or 7   # "monday" when today is monday -> next monday
    return base + timedelta(days=days_ahead)

def _parse_time(text, default_hour=11):
    """Pull a clock time out of text; return hour (24h)."""
    import re
    m = re.search(r"(\d{1,2})\s*[:\.]?\s*(\d{2})?\s*(am|pm)", text)
    if m:
        h = int(m.group(1)); mn = m.group(2); ap = m.group(3)
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        return h
    m2 = re.search(r"\b(\d{1,2})\s*(am|pm)\b", text)
    if m2:
        h = int(m2.group(1))
        if m2.group(2) == "pm" and h != 12: h += 12
        return h
    if "morning" in text: return 11
    if "afternoon" in text: return 15
    if "evening" in text: return 17
    return default_hour

def parse_freeform_slot(text):
    """Return a slot dict for a free-typed date/time, or None if unparseable.
    Returns {'error': msg} for a recognised-but-invalid time (e.g. past / out of hours)."""
    import re
    if not text:
        return None
    t = text.lower().strip()
    now = now_ist()
    day = None

    if "day after tomorrow" in t:
        day = now + timedelta(days=2)
    elif "tomorrow" in t:
        day = now + timedelta(days=1)
    elif "today" in t:
        day = now
    elif "weekend" in t:                       # this weekend -> Saturday
        day = _next_weekday(now, 5)
    else:
        m = re.search(r"after\s+(\d{1,2})\s*day", t)   # "after 3 days"
        if m:
            day = now + timedelta(days=int(m.group(1)))
        if day is None:                                    # "in 5 days"
            m = re.search(r"in\s+(\d{1,2})\s*day", t)
            if m: day = now + timedelta(days=int(m.group(1)))
        if day is None:                                    # weekday name
            for wd, idx in _WEEKDAYS.items():
                if re.search(r"\b"+wd+r"\b", t):
                    day = _next_weekday(now, idx); break
        if day is None:                                    # "next month"
            if "next month" in t or "agle mahine" in t or "agle month" in t:
                y, mo = now.year, now.month + 1
                if mo > 12: mo, y = 1, y + 1
                day = datetime(y, mo, 1, 11)   # first of next month, 11 AM default

        if day is None:                                    # "15 July" / "20th"
            md = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)?", t)
            if md:
                dnum = int(md.group(1)); mon = md.group(2)
                if 1 <= dnum <= 31:
                    month = _MONTHS[mon] if mon else now.month
                    year = now.year
                    try:
                        cand = now.replace(month=month, day=dnum)
                        if cand.date() < now.date():       # date already passed -> next month/year
                            if mon:  year += 1
                            else:    month = month % 12 + 1; year += (month == 1)
                            cand = datetime(year, month, dnum)
                        day = cand
                    except ValueError:
                        return None

        if day is None:                       # bare month, no day: "in august"
            for mname, mnum in _MONTHS.items():
                if re.search(r"\b" + mname + r"\w*\b", t):
                    y = now.year
                    if mnum < now.month:      # month already passed -> next year
                        y += 1
                    # if it's the current month, start from tomorrow; else the 1st
                    if mnum == now.month and y == now.year:
                        cand = now + timedelta(days=1)
                    else:
                        cand = datetime(y, mnum, 1)
                    day = cand
                    return {"month_only": True, "month": mnum, "year": y,
                            "month_name": datetime(y, mnum, 1).strftime("%B")}
    if day is None:
        return None

    hour = _parse_time(t)
    dt = day.replace(hour=hour, minute=0, second=0, microsecond=0)

    if dt <= now:
        return {"error": "That time has already passed. Could you pick a future date?"}
    if not (VISIT_OPEN <= hour < VISIT_CLOSE):
        return {"error": f"Site visits run {VISIT_OPEN} AM–{VISIT_CLOSE-12} PM. "
                         f"What time in that window works?"}

    return {"id": dt.strftime("slot_%Y%m%d_%H%M"),
            "label": dt.strftime("%a %d %b, %I:%M %p"), "dt": dt.isoformat()}

# ─────────────────────────────────────────────
# BOOKING
# ─────────────────────────────────────────────

def book_visit(wa_number, name, phone, slot):
    con = sqlite3.connect(DB_FILE)
    # cancel any previous active booking from the same person (reschedule)
    con.execute("UPDATE bookings SET status='cancelled' "
                "WHERE wa_number=? AND status='confirmed'",
                (wa_number,))
    con.execute(
        "INSERT INTO bookings (wa_number, name, phone, slot_id, slot_label, slot_dt, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (wa_number, name or "", phone or "", slot["id"], slot["label"], slot["dt"],
         now_ist().isoformat(timespec="seconds")))
    con.commit(); con.close()
    print(f"[BOOKING] {name or wa_number} -> {slot['label']}")

    # notify the sales agent instantly
    if AGENT_PHONE and _send_text:
        _send_text(AGENT_PHONE,
                   f"📅 New site visit booked\n{SITE_NAME}\n"
                   f"Visitor: {name or 'Unknown'} ({phone or wa_number})\n"
                   f"Slot: {slot['label']}")

def cancel_visit(wa_number):
    con = sqlite3.connect(DB_FILE)
    cur = con.execute("UPDATE bookings SET status='cancelled' "
                      "WHERE wa_number=? AND status='confirmed'",
                      (wa_number,))
    con.commit(); n = cur.rowcount; con.close()
    return n > 0

def get_active_booking(wa_number):
    con = sqlite3.connect(DB_FILE)
    cur = con.execute("SELECT slot_label, slot_dt FROM bookings "
                      "WHERE wa_number=? AND status='confirmed' "
                      "ORDER BY id DESC LIMIT 1", (wa_number,))
    row = cur.fetchone(); con.close()
    return {"slot_label": row[0], "slot_dt": row[1]} if row else None

# ─────────────────────────────────────────────
# REMINDER SCHEDULER (background thread, checks every 10 min)
# ─────────────────────────────────────────────

def _send_reminder(wa_number, name, slot_label, offset_h):
    if offset_h >= 24:
        when = "tomorrow"
    elif offset_h >= 12:
        when = "later today"
    else:
        when = "in about 2 hours"
    body = (f"Hi {name or 'there'}! Reminder: your site visit to {SITE_NAME} "
            f"is {when} — {slot_label}. 📍 {SITE_ADDRESS}. "
            f"Reply RESCHEDULE if you need a different time.")
    sent_ok = False
    if REMINDER_TEMPLATE_NAME and _send_template:
        sent_ok = _send_template(wa_number, REMINDER_TEMPLATE_NAME, TEMPLATE_LANG,
                                 [SITE_NAME, slot_label, SITE_ADDRESS])
    if not sent_ok and _send_text:
        # fallback: plain text (only delivers inside the 24h session window)
        r = _send_text(wa_number, body)
        sent_ok = r is not None and r.status_code == 200
    return sent_ok

def _reminder_offset_for(lead_time_h):
    """Pick when to remind, based on how far ahead the booking was made:
       booked <= 12h before visit  -> remind 2h before
       booked <= 24h before visit  -> remind 12h before
       booked  > 24h before visit  -> remind 24h before
    """
    if lead_time_h <= 12:
        return 2
    elif lead_time_h <= 24:
        return 12
    else:
        return 24

def reminder_loop():
    _ensure_columns()
    while True:
        try:
            now = now_ist()
            con = sqlite3.connect(DB_FILE)
            rows = con.execute(
                "SELECT id, wa_number, name, slot_label, slot_dt, created_at, reminders_sent "
                "FROM bookings WHERE status='confirmed'").fetchall()
            for bid, wa, name, label, slot_dt, created_at, sent in rows:
                dt = datetime.fromisoformat(slot_dt)
                hours_left = (dt - now).total_seconds() / 3600
                if hours_left <= 0:
                    con.execute("UPDATE bookings SET status='done' WHERE id=?", (bid,))
                    continue

                # lead time = how far ahead the booking was made
                try:
                    created = datetime.fromisoformat(created_at)
                    lead_time_h = (dt - created).total_seconds() / 3600
                except Exception:
                    lead_time_h = 999   # unknown -> treat as far-ahead (24h reminder)

                offset = _reminder_offset_for(lead_time_h)
                already = set(x for x in (sent or "").split(",") if x)

                # fire the reminder once we're within the offset window and haven't sent it
                if str(offset) not in already and hours_left <= offset:
                    if _send_reminder(wa, name, label, offset):
                        already.add(str(offset))
                        con.execute("UPDATE bookings SET reminders_sent=? WHERE id=?",
                                    (",".join(sorted(already)), bid))
                        print(f"[REMINDER] {name or wa}: {offset}h reminder sent "
                              f"(booked {lead_time_h:.0f}h ahead, {hours_left:.1f}h left)")
            con.commit(); con.close()
        except Exception as e:
            print(f"[REMINDER ERROR] {e}")
        time.sleep(600)  # 10 minutes

def start_reminder_thread():
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()
    print("[REMINDERS] Scheduler running (10-min interval).")
