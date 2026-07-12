"""
PropertyGPT — timezone helper
All timestamps across the app use India Standard Time (Asia/Kolkata, UTC+5:30),
regardless of what timezone the cloud server runs in (Render/Railway default
to UTC). Import now_ist() instead of datetime.now() everywhere.
"""
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    """Current time in IST as a naive datetime (tzinfo stripped), so it stays
    compatible with existing code that does naive datetime arithmetic and
    fromisoformat() round-trips."""
    return datetime.now(IST).replace(tzinfo=None)
