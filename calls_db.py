"""PropertyGPT Voice — call log + stats."""
import sqlite3
from config import DB_FILE
from tzutil import now_ist


def init():
    con = sqlite3.connect(DB_FILE)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS calls (
        call_id     TEXT PRIMARY KEY,
        phone       TEXT,
        started_at  TEXT,
        duration    REAL DEFAULT 0,
        outcome     TEXT DEFAULT '',   -- booked | transferred | not_interested | ...
        ended_reason TEXT DEFAULT '',
        transcript  TEXT DEFAULT ''
    );""")
    con.commit(); con.close()


def start(call_id, phone):
    con = sqlite3.connect(DB_FILE)
    con.execute("INSERT OR REPLACE INTO calls (call_id, phone, started_at) VALUES (?,?,?)",
                (call_id, phone, now_ist().isoformat(timespec="seconds")))
    con.commit(); con.close()


def set_outcome(call_id, outcome):
    con = sqlite3.connect(DB_FILE)
    con.execute("UPDATE calls SET outcome=? WHERE call_id=?", (outcome, call_id))
    con.commit(); con.close()


def finish(call_id, duration, ended_reason, transcript):
    con = sqlite3.connect(DB_FILE)
    con.execute("UPDATE calls SET duration=?, ended_reason=?, transcript=? WHERE call_id=?",
                (duration, ended_reason, transcript[:4000], call_id))
    con.commit(); con.close()


def stats():
    con = sqlite3.connect(DB_FILE)
    total    = con.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    answered = con.execute("SELECT COUNT(*) FROM calls WHERE duration > 10").fetchone()[0]
    booked   = con.execute("SELECT COUNT(*) FROM calls WHERE outcome='booked'").fetchone()[0]
    avg      = con.execute("SELECT AVG(duration) FROM calls WHERE duration>10").fetchone()[0] or 0
    rows = con.execute("SELECT phone, started_at, duration, outcome FROM calls "
                       "ORDER BY started_at DESC LIMIT 20").fetchall()
    con.close()
    return {
        "calls_made": total,
        "answered": answered,
        "pickup_rate": f"{round(100*answered/total)}%" if total else "–",
        "visits_booked": booked,
        "booking_rate": f"{round(100*booked/answered)}%" if answered else "–",
        "avg_duration_sec": round(avg),
        "recent": [{"phone": p, "at": s, "duration": round(d), "outcome": o}
                   for p, s, d, o in rows],
    }
