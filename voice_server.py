"""
PropertyGPT Voice — Vapi webhook server
----------------------------------------
Vapi handles telephony + STT + TTS + barge-in. We supply the brain.

ENDPOINTS
  POST /chat/completions   <- Vapi Custom LLM hits this (it appends the path itself)
  POST /vapi/chat          <- same handler, alternate path
  POST /vapi/events        <- call lifecycle events (start / end / transcript)
  POST /call               <- trigger an outbound call
  GET  /calls?key=...      <- stats
  GET  /                   <- health

VAPI CONFIG
  Custom LLM URL : https://YOUR-APP.onrender.com        (base only!)
  Server URL     : https://YOUR-APP.onrender.com/vapi/events

DESIGN NOTE: every failure path still yields SPEECH. A voice call must never
go silent — if the model errors, we say something graceful rather than crash.
"""
import os, json, time, traceback
import requests
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from config import (DASHBOARD_KEY, AGENT_PHONE, SITE_NAME,
                    VAPI_API_KEY, VAPI_ASSISTANT_ID, VAPI_PHONE_NUMBER_ID)
import voice_brain, integrations, calls_db

app = FastAPI()
calls_db.init()

# in-memory per-call state
call_state = {}

FALLBACK_LINE = ("Sorry ji, mujhe woh theek se sunai nahi diya. "
                 "Kya aap dobara keh sakte hain?")


def _get_state(call_id):
    """Always returns a state dict with every key present.
    (Bug fixed: /vapi/events creates the state first with only 'phone',
    so setdefault(call_id, {...full dict...}) never filled the other keys.)"""
    st = call_state.setdefault(call_id, {})
    st.setdefault("phone", "")
    st.setdefault("name", "")
    st.setdefault("sentences", 0)
    st.setdefault("booked", False)
    return st


# ─────────────────────────────────────────────
# SSE HELPERS (OpenAI-compatible, which is what Vapi expects)
# ─────────────────────────────────────────────

def _sse_delta(text):
    return "data: " + json.dumps({
        "id": "chatcmpl-vapi",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }) + "\n\n"


def _sse_tool(name, args):
    return "data: " + json.dumps({
        "id": "chatcmpl-vapi",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": f"call_{int(time.time()*1000)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }]}, "finish_reason": None}],
    }) + "\n\n"


def _sse_done():
    return ("data: " + json.dumps({
        "id": "chatcmpl-vapi",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }) + "\n\ndata: [DONE]\n\n")


# ─────────────────────────────────────────────
# THE BRAIN ENDPOINT
# ─────────────────────────────────────────────

@app.post("/chat/completions")
@app.post("/vapi/chat")
@app.post("/vapi/chat/completions")
async def vapi_chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    messages = body.get("messages", []) or []
    call_obj = body.get("call") or {}
    call_id  = call_obj.get("id") or body.get("id") or "unknown"

    # rebuild conversation from what Vapi sends
    history, user_text = [], ""
    for m in messages:
        role    = m.get("role")
        content = m.get("content") or ""
        if not content:
            continue
        if role == "user":
            history.append(("caller", content))
            user_text = content
        elif role == "assistant":
            history.append(("assistant", content))

    st = _get_state(call_id)
    # phone may arrive here before /vapi/events fires
    cust = (call_obj.get("customer") or {})
    if cust.get("number") and not st["phone"]:
        st["phone"] = cust["number"]

    caller_phone = st["phone"] or call_id

    def stream():
        spoke = False
        try:
            if not user_text.strip():
                # Vapi sometimes pings with no user turn (e.g. call start)
                yield _sse_delta("Namaste! Main " + SITE_NAME +
                                 " ki assistant bol rahi hoon. Boliye, kaise madad karun?")
                yield _sse_done()
                return

            for ev in voice_brain.respond_stream(caller_phone, user_text, history):
                if "say" in ev and ev["say"].strip():
                    st["sentences"] += 1
                    spoke = True
                    yield _sse_delta(ev["say"] + " ")

                elif "action" in ev:
                    act  = ev.get("action")
                    args = ev.get("args") or {}

                    if act == "book_site_visit":
                        try:
                            ok, msg = voice_brain.do_book(caller_phone, args, st)
                            if ok:
                                st["booked"] = True
                                calls_db.set_outcome(call_id, "booked")
                            spoke = True
                            yield _sse_delta(msg + " ")
                        except Exception as e:
                            print(f"[BOOK ERROR] {e}")
                            spoke = True
                            yield _sse_delta("Booking mein thodi dikkat aa rahi hai, "
                                             "main agent se aapko connect kar deti hoon. ")

                    elif act == "transfer_to_agent":
                        calls_db.set_outcome(call_id, "transferred")
                        spoke = True
                        yield _sse_delta("Bilkul, main aapko abhi hamare agent se "
                                         "connect karti hoon. Ek minute ji. ")
                        if AGENT_PHONE:
                            yield _sse_tool("transferCall",
                                            {"destination": "+" + AGENT_PHONE.lstrip("+")})

                    elif act == "end_call":
                        outcome = args.get("outcome", "ended")
                        if not st["booked"]:
                            calls_db.set_outcome(call_id, outcome)
                        spoke = True
                        yield _sse_delta("Thank you ji, aapka din shubh ho! ")
                        yield _sse_tool("endCall", {})

            # never leave the caller in silence
            if not spoke:
                yield _sse_delta(FALLBACK_LINE)

            yield _sse_done()

        except Exception as e:
            print(f"[VOICE ERROR] {e}")
            traceback.print_exc()
            if not spoke:
                yield _sse_delta(FALLBACK_LINE)
            yield _sse_done()

    return StreamingResponse(stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────
# CALL EVENTS
# ─────────────────────────────────────────────

@app.post("/vapi/events")
async def vapi_events(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    msg  = body.get("message") or {}
    typ  = msg.get("type")
    call = msg.get("call") or {}
    cid  = call.get("id") or ""
    if not cid:
        return {"ok": True}

    try:
        if typ == "status-update" and msg.get("status") == "in-progress":
            phone = (call.get("customer") or {}).get("number", "")
            st = _get_state(cid)
            st["phone"] = phone
            calls_db.start(cid, phone)
            print(f"[CALL] started {cid} -> {phone}")

        elif typ == "end-of-call-report":
            transcript = msg.get("transcript") or ""
            duration   = float(msg.get("durationSeconds") or 0)
            ended      = msg.get("endedReason") or ""
            calls_db.finish(cid, duration, ended, transcript)

            st = call_state.pop(cid, {})
            phone = st.get("phone") or (call.get("customer") or {}).get("number", "")
            # only log a real conversation as a lead
            if phone and duration > 15:
                integrations.save_lead(
                    st.get("name") or "Voice Lead", phone, phone,
                    f"Voice call ({int(duration)}s): {transcript[:300]}")
            print(f"[CALL] ended {cid} after {duration:.0f}s ({ended})")

    except Exception as e:
        print(f"[EVENT ERROR] {e}")

    return {"ok": True}


# ─────────────────────────────────────────────
# OUTBOUND
# ─────────────────────────────────────────────

@app.post("/call")
async def make_call(request: Request):
    """POST {"key":"<DASHBOARD_KEY>","phone":"91XXXXXXXXXX","name":"Rahul"}"""
    body = await request.json()
    if body.get("key") != DASHBOARD_KEY:
        return {"error": "unauthorized"}

    phone = integrations.normalize_phone(body.get("phone", ""))
    if not phone:
        return {"error": "invalid phone"}

    missing = [k for k, v in (("VAPI_API_KEY", VAPI_API_KEY),
                              ("VAPI_ASSISTANT_ID", VAPI_ASSISTANT_ID),
                              ("VAPI_PHONE_NUMBER_ID", VAPI_PHONE_NUMBER_ID)) if not v]
    if missing:
        return {"error": f"Vapi not configured — missing: {', '.join(missing)}"}

    try:
        r = requests.post("https://api.vapi.ai/call",
            headers={"Authorization": f"Bearer {VAPI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"assistantId": VAPI_ASSISTANT_ID,
                  "phoneNumberId": VAPI_PHONE_NUMBER_ID,
                  "customer": {"number": "+" + phone,
                               "name": body.get("name", "")}},
            timeout=20)
        ok = r.status_code in (200, 201)
        print(f"[CALL] outbound -> {phone}: {r.status_code}")
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:300]
        return {"status": "calling" if ok else "failed", "detail": detail}
    except Exception as e:
        print(f"[CALL ERROR] {e}")
        return {"status": "failed", "error": str(e)}


@app.get("/calls")
def calls_view(key: str = ""):
    if key != DASHBOARD_KEY:
        return {"error": "unauthorized"}
    return calls_db.stats()


@app.get("/")
def health():
    return {"status": f"{SITE_NAME} voice caller running",
            "chunks": len(voice_brain.CHUNKS),
            "vapi_configured": bool(VAPI_API_KEY and VAPI_ASSISTANT_ID)}
