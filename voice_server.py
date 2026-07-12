"""
PropertyGPT Voice — Vapi webhook server
----------------------------------------
Vapi handles telephony + STT + TTS + barge-in. We supply the brain.

SETUP:
1. pip install -r requirements.txt
2. Deploy (Render/Railway). Note your URL, e.g. https://voice.onrender.com
3. In Vapi (dashboard.vapi.ai):
     - Create an Assistant
     - Model:  Custom LLM  ->  URL: https://YOUR-URL/vapi/chat
     - Transcriber: Deepgram nova-2, language "multi" (handles Hindi/English mix)
     - Voice: ElevenLabs (Indian-accented) or Sarvam for best Hindi
     - Server URL (for events): https://YOUR-URL/vapi/events
4. Buy/attach an Indian number (bring your own Exotel/Plivo trunk for India).

OUTBOUND: POST /call {"phone":"91XXXXXXXXXX","name":"Rahul"} triggers a call.

IMPORTANT (India): outbound promotional calling is regulated (TRAI/DLT).
Portal leads have consented to developer contact, but you still need the
compliance wrapper. See README before going live.
"""
import os, json, requests
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from config import DASHBOARD_KEY, AGENT_PHONE, SITE_NAME
import voice_brain, integrations, calls_db

VAPI_API_KEY     = os.environ.get("VAPI_API_KEY", "")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID", "")
VAPI_PHONE_ID     = os.environ.get("VAPI_PHONE_NUMBER_ID", "")

app = FastAPI()
calls_db.init()

# in-memory per-call state
call_state = {}


# ─────────────────────────────────────────────
# THE BRAIN ENDPOINT (Vapi Custom LLM)
# Vapi POSTs an OpenAI-style request; we stream back OpenAI-style chunks.
# ─────────────────────────────────────────────

@app.post("/chat/completions")
@app.post("/vapi/chat")
async def vapi_chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    call_id  = body.get("call", {}).get("id", "unknown")

    # rebuild history from what Vapi sends
    history, user_text = [], ""
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        if role == "user":
            history.append(("caller", content))
            user_text = content
        elif role == "assistant":
            history.append(("assistant", content))

    st = call_state.setdefault(call_id, {"phone": "", "name": "", "sentences": 0})

    def stream():
        for ev in voice_brain.respond_stream(st.get("phone", call_id), user_text, history):
            if "say" in ev:
                st["sentences"] += 1
                yield _sse_delta(ev["say"] + " ")
            elif "action" in ev:
                act = ev["action"]
                args = ev.get("args", {})
                if act == "book_site_visit":
                    msg = voice_brain.do_book(st.get("phone") or call_id, args, st)
                    calls_db.set_outcome(call_id, "booked")
                    yield _sse_delta(msg)
                elif act == "transfer_to_agent":
                    calls_db.set_outcome(call_id, "transferred")
                    yield _sse_delta("Main aapko abhi hamare agent se connect karti hoon, "
                                     "ek minute ji.")
                    yield _sse_tool("transferCall", {"destination": AGENT_PHONE})
                elif act == "end_call":
                    calls_db.set_outcome(call_id, args.get("outcome", "ended"))
                    yield _sse_tool("endCall", {})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


def _sse_delta(text):
    return "data: " + json.dumps({
        "choices": [{"delta": {"content": text}, "index": 0}]
    }) + "\n\n"


def _sse_tool(name, args):
    return "data: " + json.dumps({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}
        }]}, "index": 0}]
    }) + "\n\n"


# ─────────────────────────────────────────────
# CALL EVENTS (start / end / transcript)
# ─────────────────────────────────────────────

@app.post("/vapi/events")
async def vapi_events(request: Request):
    body = await request.json()
    msg  = body.get("message", {})
    typ  = msg.get("type")
    call = msg.get("call", {}) or {}
    cid  = call.get("id", "")

    if typ == "status-update" and msg.get("status") == "in-progress":
        phone = (call.get("customer", {}) or {}).get("number", "")
        call_state.setdefault(cid, {})["phone"] = phone
        calls_db.start(cid, phone)
        print(f"[CALL] started {cid} -> {phone}")

    elif typ == "end-of-call-report":
        transcript = msg.get("transcript", "")
        duration   = msg.get("durationSeconds", 0)
        ended      = msg.get("endedReason", "")
        calls_db.finish(cid, duration, ended, transcript)
        st = call_state.pop(cid, {})
        # push the call summary to the CRM as lead context
        phone = st.get("phone") or (call.get("customer", {}) or {}).get("number", "")
        if phone:
            integrations.save_lead(st.get("name") or "Voice Lead", phone, phone,
                                   f"Voice call ({int(duration)}s): {transcript[:300]}")
        print(f"[CALL] ended {cid} after {duration}s ({ended})")

    return {"ok": True}


# ─────────────────────────────────────────────
# OUTBOUND TRIGGER
# ─────────────────────────────────────────────

@app.post("/call")
async def make_call(request: Request):
    """POST {"phone":"91XXXXXXXXXX","name":"Rahul","key":"<DASHBOARD_KEY>"}"""
    body = await request.json()
    if body.get("key") != DASHBOARD_KEY:
        return {"error": "unauthorized"}
    phone = integrations.normalize_phone(body.get("phone", ""))
    if not phone:
        return {"error": "invalid phone"}
    if not (VAPI_API_KEY and VAPI_ASSISTANT_ID and VAPI_PHONE_ID):
        return {"error": "Vapi not configured — set VAPI_API_KEY, "
                         "VAPI_ASSISTANT_ID, VAPI_PHONE_NUMBER_ID"}

    r = requests.post("https://api.vapi.ai/call",
        headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
        json={
            "assistantId": VAPI_ASSISTANT_ID,
            "phoneNumberId": VAPI_PHONE_ID,
            "customer": {"number": "+" + phone,
                         "name": body.get("name", "")},
        }, timeout=15)
    ok = r.status_code in (200, 201)
    print(f"[CALL] outbound to {phone} -> {r.status_code}")
    return {"status": "calling" if ok else "failed", "detail": r.json()}


@app.get("/calls")
def calls_view(key: str = ""):
    if key != DASHBOARD_KEY:
        return {"error": "unauthorized"}
    return calls_db.stats()


@app.get("/")
def health():
    return {"status": f"{SITE_NAME} voice caller running",
            "chunks": len(voice_brain.CHUNKS)}
