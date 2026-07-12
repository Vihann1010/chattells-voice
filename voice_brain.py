"""
PropertyGPT Voice — the brain
------------------------------
LATENCY IS THE PRODUCT. On WhatsApp a 3s reply is fine; on a call it is dead air.

The WhatsApp bot chains 3 LLM calls per turn (intent -> embed -> answer) ≈ 2-3s.
Here we make ONE call:

  * Retrieval is done with a LOCAL embedding lookup against a pre-computed cache
    (no API round-trip for the query -> we use cheap keyword+vector hybrid).
  * A single streaming Gemini call does BOTH the answering and the action
    (booking, transfer, contact capture) via function calling.
  * The reply is streamed sentence-by-sentence so TTS can start speaking
    before the model has finished thinking.
  * Speech formatting (speech.py) is pure regex — zero added latency.

Target: first audio out in < 900ms.
"""
import json, re, time
from google import genai
from google.genai import types

from config import GEMINI_API_KEY, DOC_FILE, SITE_NAME, SITE_ADDRESS
from speech import to_speech
import visits, integrations

client = genai.Client(api_key=GEMINI_API_KEY)

VOICE_MODEL = "gemini-2.5-flash"   # flash-lite is faster but weaker at tool calls

# ─────────────────────────────────────────────
# KNOWLEDGE: load once, keep in memory
# ─────────────────────────────────────────────

def _load_chunks():
    with open(DOC_FILE, encoding="utf-8") as f:
        text = f.read()
    blocks = re.split(r"\nCHUNK_ID:\s*", text)
    chunks = []
    for b in blocks[1:]:
        topic = re.search(r"TOPIC:\s*(.*)", b)
        tags  = re.search(r"TAGS:\s*(.*)", b)
        body  = re.split(r"\n-{10,}", b)[0]
        chunks.append({
            "topic": topic.group(1).strip() if topic else "",
            "tags":  tags.group(1).strip() if tags else "",
            "body":  body.strip(),
        })
    return chunks

CHUNKS = _load_chunks()
print(f"[VOICE] Loaded {len(CHUNKS)} knowledge chunks.")


def retrieve(query, k=3):
    """Keyword-overlap retrieval — no API call, ~0ms.
    For a single-property bot this is accurate enough and saves ~300-500ms
    versus embedding the query on every turn."""
    q = set(re.findall(r"\w+", query.lower()))
    scored = []
    for c in CHUNKS:
        hay = f"{c['topic']} {c['tags']} {c['body']}".lower()
        words = set(re.findall(r"\w+", hay))
        overlap = len(q & words)
        # weight topic/tag matches higher than body matches
        head = set(re.findall(r"\w+", f"{c['topic']} {c['tags']}".lower()))
        score = overlap + 3 * len(q & head)
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return [c for s, c in scored[:k] if s > 0] or CHUNKS[:k]

# ─────────────────────────────────────────────
# TOOLS the model can call mid-conversation
# ─────────────────────────────────────────────

TOOLS = [{
    "function_declarations": [
        {
            "name": "book_site_visit",
            "description": ("Book a site visit once the caller has agreed to a "
                            "specific day/time. Only call after they state a day."),
            "parameters": {"type": "object", "properties": {
                "when": {"type": "string",
                         "description": "The day/time the caller said, verbatim, "
                                        "e.g. 'kal shaam 4 baje', 'next monday 11am'"},
                "name": {"type": "string", "description": "Caller's name if known"},
            }, "required": ["when"]},
        },
        {
            "name": "transfer_to_agent",
            "description": ("Transfer the call to a human agent. Use when the caller "
                            "asks for a human, is upset, wants to negotiate price, or "
                            "asks something you cannot answer."),
            "parameters": {"type": "object", "properties": {
                "reason": {"type": "string"},
            }, "required": ["reason"]},
        },
        {
            "name": "end_call",
            "description": "End the call politely when the caller says goodbye or is not interested.",
            "parameters": {"type": "object", "properties": {
                "outcome": {"type": "string",
                            "description": "interested | not_interested | callback_later | booked"},
            }, "required": ["outcome"]},
        },
    ]
}]

# ─────────────────────────────────────────────
# SYSTEM PROMPT — tuned for SPOKEN conversation
# ─────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are Priya, a warm and polite sales assistant for {SITE_NAME},
a residential project. You are speaking to a caller on the PHONE.

THIS IS A VOICE CALL. Follow these rules absolutely:
- Keep every reply to ONE or TWO short sentences. Never more. Long answers get
  interrupted and waste the caller's time.
- Speak the way people speak, not the way people write. No lists, no bullet
  points, no markdown, no symbols, no emojis.
- Say numbers naturally: "ninety four point five lakh", not "Rs 94.5L".
- Never say a URL or an email address out loud.
- If the caller interrupts, drop what you were saying and answer their new question.
- If you did not understand, say so briefly and ask them to repeat.

LANGUAGE: Match the caller. If they speak Hindi or Hinglish, reply in natural
Hinglish (Hindi with common English words like price, site visit, possession).
Always use "aap", never "tum". Be respectful and warm — use "ji" naturally.

YOUR GOAL: Answer their questions accurately, and gently guide them towards
booking a site visit. Do not be pushy — suggest the visit once, naturally, when
they seem interested. If they say no, respect it and stay helpful.

ACCURACY: Only state facts from the property information given to you. If you do
not know something, say you will have the agent confirm it — never guess a price,
date, or legal detail.

If they want to book a visit, ask which day suits them, then call book_site_visit.
If they want a human or want to negotiate, call transfer_to_agent.
When the conversation is clearly over, call end_call.

The site address is {SITE_ADDRESS}."""


# ─────────────────────────────────────────────
# THE TURN: one streaming call, sentence-chunked
# ─────────────────────────────────────────────

# Split on sentence ends, but NOT after abbreviations (sq.ft. / Approx. / No.)
# — a period followed by a lowercase letter or a unit is not a sentence break.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u0900-\u097F])")

def respond_stream(caller_id, user_text, history):
    """Yield speech-ready sentences as soon as they're formed, plus any action.

    Yields dicts:
      {"say": "<speech-safe sentence>"}          -> send to TTS immediately
      {"action": "book"|"transfer"|"hangup", ...}
    """
    context = "\n\n".join(f"[{c['topic']}]\n{c['body']}" for c in retrieve(user_text))

    contents = []
    for role, text in history[-8:]:
        contents.append(types.Content(
            role="user" if role == "caller" else "model",
            parts=[types.Part(text=text)]))
    contents.append(types.Content(
        role="user",
        parts=[types.Part(text=f"Property information:\n{context}\n\nCaller said: {user_text}")]))

    # Build config defensively: thinking_config isn't in every google-genai version,
    # and an unsupported kwarg would crash every single turn.
    cfg_kwargs = dict(
        system_instruction=SYSTEM_PROMPT,
        tools=TOOLS,
        temperature=0.6,
        max_output_tokens=150,      # short replies = better on a call AND faster
    )
    try:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except Exception:
        pass                        # older SDK: just run without it
    try:
        cfg = types.GenerateContentConfig(**cfg_kwargs)
    except TypeError:
        cfg_kwargs.pop("thinking_config", None)
        cfg = types.GenerateContentConfig(**cfg_kwargs)

    buffer = ""
    t0 = time.time()
    first_chunk_logged = False
    said_anything = False
    did_action = False

    for chunk in client.models.generate_content_stream(
            model=VOICE_MODEL, contents=contents, config=cfg):

        # tool call?
        if chunk.candidates and chunk.candidates[0].content.parts:
            for part in chunk.candidates[0].content.parts:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    try:
                        args = dict(fc.args) if fc.args else {}
                    except Exception:
                        args = {}
                    did_action = True
                    yield {"action": fc.name, "args": args}
                    continue

        text = getattr(chunk, "text", None)
        if not text:
            continue
        if not first_chunk_logged:
            print(f"[VOICE] first token in {(time.time()-t0)*1000:.0f}ms")
            first_chunk_logged = True

        buffer += text
        # emit complete sentences immediately so TTS can start speaking
        while True:
            m = _SENTENCE_END.search(buffer)
            if not m:
                break
            sentence, buffer = buffer[:m.end()].strip(), buffer[m.end():]
            if sentence:
                spoken = to_speech(sentence)
                if spoken:
                    said_anything = True
                    yield {"say": spoken}

    if buffer.strip():
        spoken = to_speech(buffer.strip())
        if spoken:
            said_anything = True
            yield {"say": spoken}

    # Only speak a filler if we produced NEITHER speech NOR an action —
    # otherwise we'd tack "Ji, boliye" onto every booking/transfer.
    if not said_anything and not did_action:
        yield {"say": "Ji, boliye."}


# ─────────────────────────────────────────────
# ACTIONS
# ─────────────────────────────────────────────

def do_book(caller_phone, args, state):
    """Returns (success: bool, speech: str)."""
    when = args.get("when", "")
    slot = visits.parse_freeform_slot(when)

    if not slot or "error" in slot:
        msg = (slot.get("error") if isinstance(slot, dict) and "error" in slot
               else "Sorry ji, main woh time samajh nahi payi.")
        return False, to_speech(f"{msg} Aap din aur time dobara bata sakte hain?")

    if slot.get("month_only"):
        return False, to_speech(
            f"{slot['month_name']} mein kaunsi date aapko theek rahegi?")

    name = args.get("name") or state.get("name") or ""
    if name:
        state["name"] = name
    try:
        visits.book_visit(caller_phone, name, caller_phone, slot)
        integrations.save_lead(name or "Voice Lead", caller_phone, caller_phone,
                               f"Voice call — booked site visit: {slot['label']}")
    except Exception as e:
        print(f"[BOOK ERROR] {e}")
        return False, to_speech("Booking mein thodi dikkat aa rahi hai, "
                                "main agent se confirm karwa deti hoon.")

    return True, to_speech(
        f"Perfect! Aapki site visit {slot['label']} ke liye confirm ho gayi hai. "
        f"Hamari team aapko details bhej degi.")
