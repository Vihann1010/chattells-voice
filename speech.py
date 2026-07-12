"""
PropertyGPT Voice — speech formatter
-------------------------------------
Written text and spoken text are different languages. TTS engines mangle
abbreviations, currency, and units. This module rewrites RAG output into
something that SOUNDS right when spoken aloud.

Bad  (what the RAG gives us):
    "2 BHK Standard (1,050 sq.ft.) starts at ₹94.5 lakh, rate ₹9,000/sq.ft. SBA.
     Possession: 31/03/2027. Contact: +91-98765-43210"
Good (what TTS should receive):
    "2 BHK Standard, one thousand fifty square feet, starts at ninety four point
     five lakh rupees, at nine thousand rupees per square foot, super built up
     area. Possession is March 2027."

Done with deterministic regex, NOT an LLM call — zero added latency.
"""
import re

# ── unit / abbreviation expansions (order matters: longest first) ──
REPLACEMENTS = [
    # area units
    (r"\bsq\.?\s?ft\.?\b",        "square feet"),
    (r"\bsq\.?\s?m\.?\b",         "square meters"),
    (r"\bsqft\b",                 "square feet"),
    (r"\bsq\b",                   "square"),
    # real-estate jargon
    (r"\bSBA\b",                  "super built up area"),
    (r"\bRERA\b",                 "rera"),          # say it as a word, not R-E-R-A
    (r"\bBHK\b",                  "B H K"),         # spell out: sounds right
    (r"\bEMI\b",                  "E M I"),
    (r"\bGST\b",                  "G S T"),
    (r"\bNRI\b",                  "N R I"),
    (r"\bEOI\b",                  "expression of interest"),
    (r"\bOC\b",                   "occupancy certificate"),
    (r"\bCC\b",                   "completion certificate"),
    (r"\bPLC\b",                  "preferential location charge"),
    (r"\bapprox\.?\b",            "approximately"),
    (r"\bincl\.?\b",              "including"),
    (r"\bexcl\.?\b",              "excluding"),
    (r"\bmax\.?\b",               "maximum"),
    (r"\bft\.?\b",                "feet"),
    (r"\bmin\.?\b",               "minutes"),
    (r"\bkm\b",                   "kilometers"),
    (r"\bkms\b",                  "kilometers"),
    (r"\bhrs?\b",                 "hours"),
    (r"\bNo\.\s*",                "number "),
    (r"\bw/\b",                   "with"),
    (r"\be\.g\.\b",               "for example"),
    (r"\betc\.?\b",               "and so on"),
    (r"\bvs\.?\b",                "versus"),
    (r"&",                        " and "),
    (r"%",                        " percent"),
    (r"\+",                       " plus "),
]

MONTHS = {1:"January",2:"February",3:"March",4:"April",5:"May",6:"June",
          7:"July",8:"August",9:"September",10:"October",11:"November",12:"December"}

ONES = ["zero","one","two","three","four","five","six","seven","eight","nine","ten",
        "eleven","twelve","thirteen","fourteen","fifteen","sixteen","seventeen",
        "eighteen","nineteen"]
TENS = ["","","twenty","thirty","forty","fifty","sixty","seventy","eighty","ninety"]


def _num_to_words(n):
    """Indian-style number to words, for values TTS reads awkwardly."""
    n = int(n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return ONES[n // 100] + " hundred" + ("" if rest == 0 else " " + _num_to_words(rest))
    if n < 100000:
        rest = n % 1000
        return _num_to_words(n // 1000) + " thousand" + ("" if rest == 0 else " " + _num_to_words(rest))
    if n < 10000000:
        rest = n % 100000
        return _num_to_words(n // 100000) + " lakh" + ("" if rest == 0 else " " + _num_to_words(rest))
    rest = n % 10000000
    return _num_to_words(n // 10000000) + " crore" + ("" if rest == 0 else " " + _num_to_words(rest))


def _speak_money(m):
    """₹94.5 lakh -> 'ninety four point five lakh rupees'
       ₹9,000    -> 'nine thousand rupees'
       ₹1.18 Cr  -> 'one point one eight crore rupees'"""
    raw = m.group(0)
    num = re.sub(r"[₹,\s]", "", raw)
    unit = ""
    for suffix, word in (("crore","crore"), ("cr","crore"), ("lakh","lakh"),
                         ("lac","lakh"), ("l","lakh")):
        if num.lower().endswith(suffix):
            num = num[: -len(suffix)]
            unit = word
            break
    try:
        val = float(num)
    except ValueError:
        return raw

    if val == int(val):
        words = _num_to_words(int(val))
    else:                                   # 94.5 -> "ninety four point five"
        whole, frac = str(val).split(".")
        words = _num_to_words(int(whole)) + " point " + " ".join(
            ONES[int(d)] for d in frac)
    return f"{words} {unit} rupees".replace("  ", " ").strip()


def _speak_date(m):
    """31/03/2027 or 2027-03-31 -> 'March 2027' (day rarely matters when spoken)"""
    raw = m.group(0)
    parts = re.split(r"[/-]", raw)
    try:
        if len(parts[0]) == 4:               # yyyy-mm-dd
            y, mo = int(parts[0]), int(parts[1])
        else:                                # dd/mm/yyyy
            mo, y = int(parts[1]), int(parts[2])
        return f"{MONTHS[mo]} {y}"
    except Exception:
        return raw


def _speak_bare_number(m):
    """Large bare numbers like 1,050 -> 'one thousand fifty'.
    Leaves small numbers (2, 3 BHK) alone — TTS handles those fine."""
    raw = m.group(0)
    n = int(re.sub(r"[,\s]", "", raw))
    if n < 100:
        return raw
    return _num_to_words(n)


def to_speech(text):
    """Convert written RAG output into speech-safe text for TTS."""
    if not text:
        return text
    t = text

    # strip anything that has no spoken form
    t = re.sub(r"[*_#`|]", "", t)                 # markdown
    t = re.sub(r"https?://\S+", "", t)            # URLs
    t = re.sub(r"\S+@\S+\.\S+", "", t)            # emails
    t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", t)  # emoji

    # money BEFORE generic numbers
    t = re.sub(r"₹\s?[\d,]+(?:\.\d+)?\s?(?:crore|cr|lakh|lac|L)?\b",
               _speak_money, t, flags=re.IGNORECASE)
    t = re.sub(r"\bRs\.?\s?[\d,]+(?:\.\d+)?\s?(?:crore|cr|lakh|lac)?\b",
               _speak_money, t, flags=re.IGNORECASE)

    # dates
    t = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b", _speak_date, t)
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", _speak_date, t)

    # abbreviations / units
    for pattern, repl in REPLACEMENTS:
        t = re.sub(pattern, repl, t, flags=re.IGNORECASE)

    # "9,000/square feet" -> "9000 per square foot"
    t = re.sub(r"/\s?square feet", " per square foot", t)
    t = re.sub(r"\bper square feet\b", "per square foot", t)
    # generic "/month", "/year", "/unit", "/sq"
    t = re.sub(r"/\s?(month|year|annum|unit|day|week)\b", r" per \1", t)

    # remaining large bare numbers
    t = re.sub(r"\b\d{1,3}(?:,\d{3})+\b", _speak_bare_number, t)

    # phone numbers -> digit by digit (rare, but TTS butchers them)
    t = re.sub(r"\+?\d{2}[-\s]?\d{5}[-\s]?\d{5}",
               lambda m: " ".join(d for d in re.sub(r"\D", "", m.group(0))), t)

    # tidy punctuation for natural prosody
    t = t.replace("(", ", ").replace(")", ", ")
    t = re.sub(r"\s*[:;]\s*", ", ", t)
    t = re.sub(r"\s*-\s*", " ", t)
    t = re.sub(r",\s*,+", ", ", t)
    # an expanded abbreviation can leave "square feet., starts" -> "square feet, starts"
    t = re.sub(r"\.\s*,", ",", t)
    # "...approximately. 5 minutes" -> "...approximately 5 minutes"
    # (a period followed by a lowercase word or digit was an abbreviation dot)
    t = re.sub(r"\.\s+(?=[a-z0-9])", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+([.,!?])", r"\1", t)
    t = re.sub(r",\s*$", ".", t)          # trailing comma -> full stop
    return t.strip()
