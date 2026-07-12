# PropertyGPT Voice — AI Caller

A voice-optimised fork of the WhatsApp bot. **The WhatsApp project is untouched** —
this is a separate, lean service.

## What was kept, what was cut

**Kept (reused as-is):** `visits.py` (booking + free-text date parsing),
`integrations.py` (Zoho push, phone validation), `zoho_auth.py`, `tzutil.py`,
`config.py`, and your `property_doc.txt`.

**Cut (WhatsApp-only):** media catalog, brochure sending, WhatsApp templates,
portal first-touch, interactive slot lists, leads/dashboard web pages,
the 24-hour window logic. None of it applies to a phone call.

**New:** `speech.py`, `voice_brain.py`, `voice_server.py`, `calls_db.py`.

## The two things that make it work

### 1. Speech-safe text (`speech.py`)
TTS engines butcher written notation. Every reply is rewritten before it is spoken:

| Written | Spoken |
|---|---|
| `1,050 sq.ft.` | one thousand fifty **square feet** |
| `₹94.5 lakh` | **ninety four point five lakh rupees** |
| `₹9,000/sq.ft. SBA` | nine thousand rupees **per square foot, super built up area** |
| `31/03/2027` | **March 2027** |
| `EMI`, `BHK`, `GST` | E M I, B H K, G S T (spelled out) |
| `5%`, `&`, `etc.` | 5 percent, and, and so on |
| URLs, emails, emoji | *(removed — unspeakable)* |

Pure regex → **~3ms, zero API cost**.

### 2. One LLM call per turn, streamed
The WhatsApp bot chains 3 calls (intent → embed → answer) ≈ 2–3s. Dead air on a call.

Voice version:
- **Retrieval is local** — keyword+topic scoring over the 45 chunks. Benchmarked at
  **~3ms** and hits the right chunk on every test query (English *and* Hinglish),
  versus ~300–500ms for an embedding API call.
- **One streaming Gemini call** does the answering *and* the actions
  (book / transfer / hang up) via function calling.
- **Thinking disabled** (`thinking_budget=0`) and `max_output_tokens=120` —
  short replies are both better on a call and faster.
- Replies are **emitted sentence-by-sentence**, so TTS starts speaking before the
  model has finished generating.

**Latency budget (target < 900ms to first audio):**
| Stage | Cost |
|---|---|
| Deepgram STT (streaming, endpointed) | ~300ms |
| Local retrieval | ~3ms |
| Gemini first token (no thinking) | ~350ms |
| Speech format | ~3ms |
| TTS first audio | ~200ms |
| **Total** | **~850ms** |

## Setup

```bash
pip install -r requirements.txt
uvicorn voice_server:app --port 8000
```

`.env`:
```
GEMINI_API_KEY=...
VAPI_API_KEY=...
VAPI_ASSISTANT_ID=...
VAPI_PHONE_NUMBER_ID=...
AGENT_PHONE=91XXXXXXXXXX       # for call transfers
DASHBOARD_KEY=...
ZOHO_CLIENT_ID=...             # same Zoho app as WhatsApp bot
ZOHO_CLIENT_SECRET=...
ZOHO_REFRESH_TOKEN=...
ZOHO_ACCOUNTS_URL=https://accounts.zoho.in
ZOHO_API_BASE=https://www.zohoapis.in/crm/v2
```

### Vapi config (dashboard.vapi.ai)
- **Model:** Custom LLM → `https://YOUR-URL/vapi/chat`
- **Transcriber:** Deepgram `nova-2`, language `multi` (handles Hindi/English mixing)
- **Voice:** ElevenLabs (Indian accent) or **Sarvam** (best for Hindi)
- **Server URL:** `https://YOUR-URL/vapi/events`
- Enable **barge-in** and **voicemail detection**

### Trigger an outbound call
```bash
curl -X POST https://YOUR-URL/call \
  -H "Content-Type: application/json" \
  -d '{"key":"YOUR_DASHBOARD_KEY","phone":"919555202648","name":"Rahul"}'
```

Stats: `GET /calls?key=YOUR_DASHBOARD_KEY` → calls made, pickup rate,
visits booked, booking rate, avg duration, recent calls.

## Cost per call (~4 min, India)

| Item | Cost |
|---|---|
| Telephony (Exotel/Plivo trunk) | ₹2–3 |
| Deepgram STT | ₹1–2 |
| TTS (ElevenLabs ≫ Sarvam) | ₹2–5 |
| Gemini (one short call per turn) | ~₹1 |
| **Total** | **≈ ₹6–11 / call** |

⚠️ **Judge cost per OUTCOME, not per call.** At a 20% pickup rate, a *conversation*
costs ₹30–55 — versus ~₹1.50 on WhatsApp. Voice must convert far better to justify
that. Measure it before scaling.

## ⚠️ Before going live (India)

**Outbound promotional calling is regulated (TRAI / TCCCPA).** You need DLT
registration, consent, DND scrubbing, and calls must originate from the correct
number series. A portal lead has typically consented to developer contact — that's
your legal basis — but the compliance wrapper is still required. Inbound calls do
not have this problem.

**Recommended rollout:**
1. WhatsApp first-touch (₹0.88, instant, non-intrusive)
2. Voice call **only if no reply in 24h** — voice as the escalation, not the opener
3. Track pickup and booking rates in `/calls` and compare against WhatsApp
