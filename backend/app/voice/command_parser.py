"""
BolKhata – Voice Command Parser
=================================
Deterministic, rule-based parser for Hindi / Hinglish / English ledger commands.

Responsibilities:
  - Detect intent  : CREDIT, PAYMENT, BALANCE_QUERY, HISTORY_QUERY,
                     NEW_CUSTOMER, CORRECTION, CLARIFICATION_NEEDED, UNKNOWN
  - Extract        : customer name, amount (INR), transaction description,
                     relative date offset, spoken correction
  - Return         : ParsedCommand dataclass – pure data, no side effects

Does NOT:
  - Execute any transactions
  - Touch the database
  - Make any network calls
  - Use any AI / LLM – fully deterministic

Language support:
  - Hindi   (romanised Devanagari)
  - Hinglish (Hindi + English mix)
  - English  (Indian English)

Disambiguation rules (documented so they are testable):
  "X ne  <verb>"       → X is the subject → X performed the action
  "X ko  <verb>"       → X is the object  → action was done TO X
  "diya/diye" + "ne"   → X paid           → PAYMENT
  "diya/diye" + "ko"   → gave credit to X → CREDIT
  "udhaar / credit"    → always CREDIT    (overrides diya/diye)
  "jama / paid / mile" → always PAYMENT   (overrides diya/diye)
  "liya / liye"        → always CREDIT    (took on credit)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Public enums and dataclasses
# ─────────────────────────────────────────────────────────────────────────────

class Intent(str, Enum):
    CREDIT               = "CREDIT"
    PAYMENT              = "PAYMENT"
    BALANCE_QUERY        = "BALANCE_QUERY"
    HISTORY_QUERY        = "HISTORY_QUERY"
    DAILY_CREDIT_TOTAL   = "DAILY_CREDIT_TOTAL"
    DAILY_PAYMENT_TOTAL  = "DAILY_PAYMENT_TOTAL"
    NEW_CUSTOMER         = "NEW_CUSTOMER"
    CORRECTION           = "CORRECTION"
    CLARIFICATION_NEEDED = "CLARIFICATION_NEEDED"
    UNKNOWN              = "UNKNOWN"


@dataclass
class ParsedCommand:
    """
    Structured result of parsing a voice command string.

    Attributes:
        intent               : Detected intent (see Intent enum).
        customer_name        : Title-cased customer name, or None if not found.
        amount               : Transaction amount in INR (float), or None.
        description          : Optional free-text note (future use; currently None).
        date_offset          : 0 = today (aaj), -1 = yesterday (kal),
                               -2 = day before yesterday (parso).
        is_correction        : True when this corrects a prior pending command.
        clarification_prompt : Human-readable question to ask the user when
                               intent is CLARIFICATION_NEEDED.
        raw_text             : Original unmodified input string.
        confidence           : "HIGH" | "MEDIUM" | "LOW"
    """
    intent: Intent
    customer_name: Optional[str] = None
    amount: Optional[float] = None
    description: Optional[str] = None
    date_offset: int = 0
    is_correction: bool = False
    clarification_prompt: Optional[str] = None
    raw_text: str = ""
    confidence: str = "HIGH"


# ─────────────────────────────────────────────────────────────────────────────
# Hindi number vocabulary
# ─────────────────────────────────────────────────────────────────────────────

_UNITS: dict[str, float] = {
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4,
    "paanch": 5, "chhe": 6, "chh": 6, "saat": 7, "aath": 8, "nau": 9,
}
_TEENS: dict[str, float] = {
    "das": 10, "gyarah": 11, "barah": 12, "terah": 13,
    "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17,
    "atharah": 18, "unees": 19, "unaas": 19,
}
_TENS: dict[str, float] = {
    "bees": 20, "tees": 30, "chaalees": 40, "chalees": 40,
    "pachaas": 50, "saath": 60, "sattar": 70, "assi": 80,
    "nabbe": 90, "nabbeh": 90,
}
# All recognised Hindi number tokens (for span removal in name extraction)
_NUMBER_WORDS: frozenset[str] = frozenset(
    list(_UNITS) + list(_TEENS) + list(_TENS) +
    ["sau", "hazaar", "hajar", "dedh", "dhai", "aadha", "sawa"]
)
# Compound specials: match BEFORE the general algorithm
_SPECIAL_COMPOUNDS: list[tuple[str, float]] = [
    (r"\bdedh\s+hazaar\b",          1500.0),
    (r"\bdhai\s+hazaar\b",          2500.0),
    (r"\bsaadhe\s+teen\s+hazaar\b", 3500.0),
    (r"\bdedh\s+sau\b",              150.0),
    (r"\bdhai\s+sau\b",              250.0),
]

# Placeholder token used to mark where an amount was found during name extraction
_AMOUNT_TOKEN = "__AMOUNT__"


# ─────────────────────────────────────────────────────────────────────────────
# Intent keyword sets
# ─────────────────────────────────────────────────────────────────────────────

_CREDIT_STRONG: tuple[str, ...] = (
    "udhaar", "udhar", "credit",
)
_PAYMENT_STRONG: tuple[str, ...] = (
    "jama", "jamaa", "paid", "payment", "mile", "mila", "mili",
    "aaye", "aaya", "aai", "wapas", "bhugtaan",
)
_BALANCE_WORDS: tuple[str, ...] = (
    "kitna", "kitne", "baaki", "balance", "hisab", "bata", "batao",
    "how much", "owes", "outstanding",
)
_HISTORY_WORDS: tuple[str, ...] = (
    "history", "transaction", "transactions", "pichhla", "pichle",
    "len-den", "detail", "details", "show",
)
_NEW_CUSTOMER_PHRASES: tuple[str, ...] = (
    "naya customer", "add customer", "new customer",
    "khata kholo", "nayi entry",
)
_CORRECTION_MARKERS: tuple[str, ...] = (
    r"\bnahin[,\s]+",
    r"\bnahi[,\s]+",
    r"\bnhi[,\s]+",
    r"\bna\s+nahi[,\s]+",
    r"\bmatlab[,\s]+",
    r"\bactually[,\s]+",
    r"\bsorry[,\s]+",
    r"\bcorrection[,:\s]+",
    r"\bi\s+mean[,\s]+",
)
_DATE_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\bparso\b",       -2),
    (r"\bparason\b",     -2),
    (r"\bparson\b",      -2),
    (r"\bday\s+before\b",-2),
    (r"\byesterday\b",   -1),
    (r"\bkal\b",         -1),
    (r"\baaj\b",          0),
    (r"\btoday\b",        0),
)
# Words stripped from name candidates (particles, verbs, currency, stop-words)
_NAME_NOISE: frozenset[str] = frozenset([
    # Particles
    "ne", "ko", "ka", "ki", "ke", "se", "mein", "aur", "nam", "naam", "vyakti",
    # Date words (already removed but kept as safety)
    "aaj", "kal", "parso",
    # Currency
    "rupaye", "rupees", "rupee", "rupe", "rs", "inr",
    # Transaction keywords
    "credit", "udhaar", "udhar", "jama", "payment", "paid",
    "diya", "diye", "liya", "liye", "mila", "mile", "aaya", "aaye",
    "le", "de", "wapas", "return",
    # Auxiliaries
    "karo", "kiya", "kiye", "tha", "hai", "hain", "ho", "hua", "hui",
    "gaya", "gayi", "gaye", "raha", "rahi",
    # English stop-words
    "add", "for", "from", "of", "and", "the", "a", "an",
    # New customer words
    "naya", "customer", "new", "khata", "kholo", "nayi", "entry",
    # Amount placeholder
    "__amount__",
])

_CREDIT_STRONG_DEV: tuple[str, ...] = (
    "उधार", "उधारी", "कर्ज",
)
_PAYMENT_STRONG_DEV: tuple[str, ...] = (
    "जमा", "भुगतान", "वापस", "मिला", "मिले", "आया", "आए", "जमा",
)
_BALANCE_WORDS_DEV: tuple[str, ...] = (
    "बाकी", "बैलेंस", "हिसाब", "कितना", "कितने", "बताओ", "बता", "बताइए",
)
_HISTORY_WORDS_DEV: tuple[str, ...] = (
    "हिस्ट्री", "ट्रांजेक्शन", "लेन-देन", "विवरण", "दिखाओ", "इतिहास", "history",
)
_CURRENCY_DIGIT_RE = re.compile(
    r"(?:₹\s*|rs\.?\s+)?(\d{1,7}(?:[,\.]\d{1,3})?)(?:\s*(?:rupaye|rupees|rupee|rupe|rs\.?|inr))?",
    re.IGNORECASE,
)
_DIGIT_RE = re.compile(r"\b(\d{1,7}(?:[,\.]\d{1,3})?)\b")

# Name extraction: ordered list of (pattern, name_group_index)
# Applied to text with amount already replaced by _AMOUNT_TOKEN
_NAME_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"^(.+?),",               1),   # "Ramesh, write 340 rupees as credit"
    (r"^(.+?)\s+write\b",      1),   # "Ramesh write 340 rupees as credit"
    (r"^(.+?)\s+ne\b",             1),   # "Rahul ne ..."
    (r"^(.+?)\s+ko\b",             1),   # "Rahul ko ..."
    (r"^(.+?)\s+ka\b",             1),   # "Rahul ka ..."
    (r"^(.+?)\s+ki\b",             1),   # "Ramesh ki ..."
    (r"^(.+?)\s+paid\b",           1),   # "Amit paid ..."
    (r"^(.+?)\s+se\b",             1),   # "Ramesh se ..."
    (r"\bfor\s+(.+?)(?:\s*$)",     1),   # "... for Amit"
    (r"\bfrom\s+(.+?)(?:\s*$)",    1),   # "... from Suresh"
    (r"\bof\s+(.+?)(?:\s*$)",      1),   # "... of Ramesh"
)


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

# ---------------------------------------------------------------------------
# Devanagari -> Latin transliteration (basic, for cross-script support)
# ---------------------------------------------------------------------------

_DEV_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h",
}
_DEV_VOWEL_SIGNS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ृ": "ri",
}
_DEV_INDEPENDENT_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u",
    "ऊ": "u", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
    "ऋ": "ri",
}
_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _transliterate_devanagari(text: str) -> str:
    chars = list(text)
    result = []
    i = 0
    n = len(chars)
    while i < n:
        ch = chars[i]
        if ch in _DEV_INDEPENDENT_VOWELS:
            result.append(_DEV_INDEPENDENT_VOWELS[ch])
            i += 1
        elif ch in "०१२३४५६७८९":
            result.append(ch.translate(_DEV_DIGITS))
            i += 1
        elif ch == "ं" or ch == "ँ":
            result.append("n")
            i += 1
        elif ch in _DEV_CONSONANTS:
            cons = _DEV_CONSONANTS[ch]
            if i + 1 < n and chars[i + 1] in _DEV_VOWEL_SIGNS:
                result.append(cons + _DEV_VOWEL_SIGNS[chars[i + 1]])
                i += 2
            elif i + 1 < n and chars[i + 1] == "्":
                result.append(cons)
                i += 2
            else:
                if i + 1 == n or chars[i + 1].isspace() or chars[i + 1] in "।,.!?":
                    result.append(cons)
                else:
                    result.append(cons + "a")
                i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, remove sentence-ending punctuation."""
    text = text.lower().strip()
    text = re.sub(r"[,।\.!?]+", " ", text)   # Remove punctuation (Hindi daṇḍa too)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _detect_correction(text: str) -> Optional[float]:
    """
    Detect spoken correction patterns such as:
      "nahi, 430"  |  "matlab 300 rupaye"  |  "actually 250"
    Returns the corrected amount if detected, else None.
    """
    for marker in _CORRECTION_MARKERS:
        m = re.search(marker + r"(.+)", text)
        if m:
            remainder = m.group(1).strip()
            amount = _extract_amount_value(remainder)
            if amount is not None:
                return amount
    return None


def _extract_date(text: str) -> tuple[int, str]:
    """
    Find a date reference word and return (offset, text_without_date_word).
    offset: 0 = today, -1 = yesterday, -2 = day before yesterday.
    "kal" is treated as yesterday (past tense context of recording transactions).
    """
    for pattern, offset in _DATE_PATTERNS:
        if re.search(pattern, text):
            cleaned = re.sub(pattern, " ", text)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            return offset, cleaned
    return 0, text


def _extract_amount_value(text: str) -> Optional[float]:
    """
    Extract the first valid positive amount from text.
    Does NOT modify the text; use _extract_amount() for that.
    """
    # 1. Special Hindi compound patterns (dedh hazaar, dhai sau, etc.)
    for pattern, value in _SPECIAL_COMPOUNDS:
        if re.search(pattern, text):
            return value

    # 2. Digit with optional currency prefix / suffix
    m = _CURRENCY_DIGIT_RE.search(text)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass

    # 3. Bare digit
    m = _DIGIT_RE.search(text)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass

    # 4. Hindi word numbers
    return _parse_hindi_number(text)


def _extract_amount(text: str) -> tuple[Optional[float], str]:
    """
    Extract the first valid amount and return (amount, text_with_amount_replaced).
    The amount span is replaced by _AMOUNT_TOKEN to aid subsequent name extraction.
    """
    # 1. Special Hindi compound patterns
    for pattern, value in _SPECIAL_COMPOUNDS:
        m = re.search(pattern, text)
        if m:
            cleaned = text[:m.start()] + f" {_AMOUNT_TOKEN} " + text[m.end():]
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            return value, cleaned

    # 2. Digit with optional currency prefix / suffix
    m = _CURRENCY_DIGIT_RE.search(text)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            v = float(raw)
            if v > 0:
                cleaned = text[:m.start()] + f" {_AMOUNT_TOKEN} " + text[m.end():]
                cleaned = re.sub(r"\s+", " ", cleaned).strip()
                return v, cleaned
        except ValueError:
            pass

    # 3. Hindi word numbers: remove all matched number words, insert token
    value = _parse_hindi_number(text)
    if value is not None:
        cleaned = text
        for word in sorted(_NUMBER_WORDS, key=len, reverse=True):  # longest first
            cleaned = re.sub(r"\b" + re.escape(word) + r"\b", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip() + f" {_AMOUNT_TOKEN}"
        return value, cleaned

    return None, text


def _parse_hindi_number(text: str) -> Optional[float]:
    """
    Parse a Hindi word-based number from text.
    Processes tokens left-to-right, accumulating thousands, hundreds, and units.

    Supported patterns (examples):
      sau         → 100
      do sau      → 200
      paanch sau pachaas → 550
      ek hazaar   → 1000
      do hazaar paanch sau → 2500
    """
    tokens = [t for t in text.lower().split() if t in _NUMBER_WORDS]
    if not tokens:
        return None

    total = 0.0
    thousands = 0.0
    hundreds = 0.0
    units = 0.0

    for tok in tokens:
        if tok in ("hazaar", "hajar"):
            if units > 0:
                thousands += units * 1000.0
            elif hundreds > 0:
                thousands += hundreds * 1000.0
            else:
                thousands += 1000.0
            units = 0.0
            hundreds = 0.0

        elif tok == "sau":
            if units > 0:
                hundreds += units * 100.0
            else:
                hundreds += 100.0
            units = 0.0

        elif tok in _UNITS:
            units += _UNITS[tok]

        elif tok in _TEENS:
            units += _TEENS[tok]

        elif tok in _TENS:
            units += _TENS[tok]

        # "dedh" / "dhai" / "aadha" / "sawa" standalone are handled via
        # _SPECIAL_COMPOUNDS before this function is called; skip here.

    total = thousands + hundreds + units
    return total if total > 0 else None


def _detect_aggregate_intent(text: str) -> Optional[Intent]:
    """Detect read-only daily total queries."""
    lowered = text.lower()
    has_date = any(word in lowered for word in ("aaj", "today", "kal", "yesterday"))
    has_quantity = any(
        word in lowered
        for word in ("kitna", "kitana", "kitne", "kitane", "kitni", "kitani", "total")
    )
    if not has_date or not has_quantity:
        return None
    if any(word in lowered for word in ("udhaar", "udhar", "credit")):
        return Intent.DAILY_CREDIT_TOTAL
    if any(word in lowered for word in ("jama", "payment", "mila", "mili", "aaya", "aaye", "paid")):
        return Intent.DAILY_PAYMENT_TOTAL
    return None


def _detect_intent(text: str) -> Intent:
    """
    Detect intent using priority-ordered keyword and pattern matching.
    Priority (highest first):
      1. New customer phrases
      2. Unambiguous CREDIT keywords  (udhaar, credit)
      3. Unambiguous PAYMENT keywords (jama, paid, mile, aaya…)
      4. Weak CREDIT: liya/liye (took on credit)
      5. Ambiguous diya/diye: resolve via ne/ko particle
      6. English CREDIT verbs: gave, owes, took
      7. Balance query keywords
      8. History query keywords
      9. UNKNOWN fallback
    """
    # 1. New customer
    for phrase in _NEW_CUSTOMER_PHRASES:
        if phrase in text:
            return Intent.NEW_CUSTOMER

    # 2. Unambiguous CREDIT
    for kw in _CREDIT_STRONG:
        if re.search(r"\b" + re.escape(kw) + r"\b", text):
            return Intent.CREDIT

    # 3. Unambiguous PAYMENT
    for kw in _PAYMENT_STRONG:
        if re.search(r"\b" + re.escape(kw) + r"\b", text):
            return Intent.PAYMENT

    # 4. Weak CREDIT – "liya/liye/lia" = took (credit)
    if re.search(r"\b(?:liya|liye|lia)\b", text):
        return Intent.CREDIT

    # 5. Ambiguous "diya/diye/dia"
    if re.search(r"\b(?:diya|diye|dia)\b", text):
        if re.search(r"\bne\b", text):
            return Intent.PAYMENT   # "X ne diya" = X paid
        if re.search(r"\bko\b", text):
            return Intent.CREDIT    # "X ko diya" = gave credit to X
        return Intent.PAYMENT       # standalone diya → default to payment

    # 6. English credit verbs
    if re.search(r"\b(?:gave|given|owes|took|taken)\b", text):
        return Intent.CREDIT

    # 7. History query
    for kw in _HISTORY_WORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", text):
            return Intent.HISTORY_QUERY

    # 8. Balance query
    for kw in _BALANCE_WORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", text):
            return Intent.BALANCE_QUERY

    return Intent.UNKNOWN


def _detect_intent_devanagari(text: str) -> Intent:
    aggregate_intent = _detect_aggregate_intent(_transliterate_devanagari(text))
    if aggregate_intent is not None:
        return aggregate_intent
    for phrase in ("नया कस्टमर", "नया customer", "add customer", "new customer"):
        if phrase in text:
            return Intent.NEW_CUSTOMER
    for kw in _CREDIT_STRONG_DEV:
        if kw in text:
            return Intent.CREDIT
    for kw in _PAYMENT_STRONG_DEV:
        if kw in text:
            return Intent.PAYMENT
    if "लिया" in text or "लिए" in text:
        return Intent.CREDIT
    if "दिया" in text or "दिए" in text:
        if "ने" in text:
            return Intent.PAYMENT
        if "को" in text:
            return Intent.CREDIT
        return Intent.PAYMENT
    for kw in _HISTORY_WORDS_DEV:
        if kw in text:
            return Intent.HISTORY_QUERY
    for kw in _BALANCE_WORDS_DEV:
        if kw in text:
            return Intent.BALANCE_QUERY
    return Intent.UNKNOWN


def _extract_name(text_with_token: str) -> Optional[str]:
    """
    Extract a customer name from text in which the amount has already been
    replaced by _AMOUNT_TOKEN. Uses ordered positional regex patterns.
    """
    # Remove the amount placeholder before pattern matching
    working = text_with_token.replace(_AMOUNT_TOKEN, "").strip()
    working = re.sub(r"\s+", " ", working)

    for pattern, group in _NAME_PATTERNS:
        m = re.search(pattern, working)
        if m:
            candidate = m.group(group).strip()
            cleaned = _clean_name(candidate)
            if cleaned:
                return cleaned

    return None


def _clean_name(raw: str) -> Optional[str]:
    """
    Strip noise words from a raw name candidate and title-case the result.
    Returns None if nothing valid remains after filtering.
    """
    if not raw:
        return None
    words = raw.strip().split()
    filtered = [
        w for w in words
        if w.lower() not in _NAME_NOISE
        and w.lower() not in _NUMBER_WORDS
        and not w.isdigit()
        and len(w) > 1          # ignore single-letter fragments
    ]
    if not filtered:
        return None
    return " ".join(w.capitalize() for w in filtered)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_command(text: str) -> ParsedCommand:
    """
    Parse a voice command string into a structured ParsedCommand.

    Guarantees:
      - Deterministic: identical input → identical output.
      - Side-effect-free: no DB, no network, no file I/O.
      - Safe: never raises; returns UNKNOWN intent on any unexpected input.

    Args:
        text: Raw transcript string from STT or typed input.

    Returns:
        ParsedCommand with intent, customer_name, amount, date_offset, etc.
        Returns CLARIFICATION_NEEDED when a CREDIT/PAYMENT/BALANCE/HISTORY
        command is detected but required fields (name, amount) are absent.
    """
    raw_text = text if text else ""

    if any("\u0900" <= c <= "\u097F" for c in raw_text):
        processing_text = _transliterate_devanagari(raw_text)
    else:
        processing_text = raw_text

    # ── Guard: empty / blank ─────────────────────────────────────────────
    if not processing_text.strip():
        return ParsedCommand(
            intent=Intent.UNKNOWN,
            raw_text=raw_text,
            confidence="LOW",
            clarification_prompt="Kuch sunai nahi diya. Dobara boliye please.",
        )

    normalized = _normalize(processing_text)

    # ── Step 1: Correction – early return ────────────────────────────────
    corrected_amount = _detect_correction(normalized)
    if corrected_amount is not None:
        return ParsedCommand(
            intent=Intent.CORRECTION,
            amount=corrected_amount,
            is_correction=True,
            raw_text=raw_text,
            confidence="HIGH",
        )

    # ── Step 2: Date offset ──────────────────────────────────────────────
    aggregate_intent = _detect_aggregate_intent(normalized)

    date_offset, normalized = _extract_date(normalized)

    # ── Step 3: Amount extraction ────────────────────────────────────────
    amount, text_with_token = _extract_amount(normalized)

    # ── Step 4: Intent detection ─────────────────────────────────────────
    if aggregate_intent is not None:
        intent = aggregate_intent
    elif any("\u0900" <= c <= "\u097F" for c in raw_text):
        intent = _detect_intent_devanagari(_normalize(raw_text))
        if intent == Intent.UNKNOWN:
            intent = _detect_intent(normalized)
    else:
        intent = _detect_intent(normalized)      # use full text (amount still present)

    # ── Step 5: Name extraction ──────────────────────────────────────────
    customer_name = _extract_name(text_with_token)

    # ── Step 6: Completeness check ───────────────────────────────────────
    if intent in (Intent.CREDIT, Intent.PAYMENT):
        if customer_name is None and amount is None:
            return ParsedCommand(
                intent=Intent.CLARIFICATION_NEEDED,
                raw_text=raw_text,
                confidence="LOW",
                clarification_prompt=(
                    "Kiska naam aur kitna paisa? Poora bataye, jaise: "
                    "'Ramesh ko 500 rupaye udhaar'."
                ),
            )
        if customer_name is None:
            return ParsedCommand(
                intent=Intent.CLARIFICATION_NEEDED,
                amount=amount,
                raw_text=raw_text,
                confidence="LOW",
                clarification_prompt="Kiske liye? Customer ka naam bataye.",
            )
        if amount is None:
            return ParsedCommand(
                intent=Intent.CLARIFICATION_NEEDED,
                customer_name=customer_name,
                raw_text=raw_text,
                confidence="LOW",
                clarification_prompt=(
                    f"{customer_name} ke liye kitna? Amount bataye."
                ),
            )

    if intent == Intent.BALANCE_QUERY and customer_name is None:
        return ParsedCommand(
            intent=Intent.CLARIFICATION_NEEDED,
            raw_text=raw_text,
            confidence="LOW",
            clarification_prompt="Kis customer ka balance dekhna hai? Naam bataye.",
        )

    if intent == Intent.HISTORY_QUERY and customer_name is None:
        return ParsedCommand(
            intent=Intent.CLARIFICATION_NEEDED,
            raw_text=raw_text,
            confidence="LOW",
            clarification_prompt="Kis customer ki history dekhni hai? Naam bataye.",
        )

    # ── Step 7: Confidence scoring ───────────────────────────────────────
    if intent == Intent.UNKNOWN:
        confidence = "LOW"
    elif intent in (Intent.CREDIT, Intent.PAYMENT) and customer_name and amount:
        confidence = "HIGH"
    elif intent in (Intent.BALANCE_QUERY, Intent.HISTORY_QUERY) and customer_name:
        confidence = "HIGH"
    elif intent in (Intent.NEW_CUSTOMER, Intent.CORRECTION):
        confidence = "HIGH"
    else:
        confidence = "MEDIUM"

    return ParsedCommand(
        intent=intent,
        customer_name=customer_name,
        amount=amount,
        date_offset=date_offset,
        raw_text=raw_text,
        confidence=confidence,
    )


def _clause_has_amount(clause: str) -> bool:
    return bool(re.search(r"\d", clause)) or any(
        word in clause.lower()
        for word in ("ek", "do", "teen", "char", "paanch", "chhe", " saat", "aath", "nau", "das", "sau", "hazaar")
    )


def parse_commands(text: str) -> list[ParsedCommand]:
    """Parse one or more ledger commands from a single transcript."""
    raw_text = text if text else ""

    if any("\u0900" <= c <= "\u097F" for c in raw_text):
        processing_text = _transliterate_devanagari(raw_text)
    else:
        processing_text = raw_text

    if not processing_text.strip():
        return [parse_command(raw_text)]

    # Keep Devanagari/Roman/English correction markers and their amounts together.
    lower_text = processing_text.lower()
    marker_pos = -1
    for marker in ("nahin", "nahi", "nhi", "na nahi", "matlab", "actually", "sorry", "correction", "i mean"):
        pos = lower_text.find(marker)
        if pos != -1 and (marker_pos == -1 or pos < marker_pos):
            marker_pos = pos

    if marker_pos != -1:
        head = processing_text[:marker_pos]
        head = re.sub(r"\s*(?:,|;|\band\b|\baur\b|are|arre)\s*$", "", head, flags=re.IGNORECASE)
        tail = processing_text[marker_pos:]
        head_clauses = [
            clause.strip()
            for clause in re.split(r"\s*(?:,|;|\band\b|\baur\b)\s*", head, flags=re.IGNORECASE)
            if clause.strip()
        ]
        clauses = head_clauses + [tail.strip()] if head_clauses else [tail.strip()]
    else:
        clauses = [
            clause.strip()
            for clause in re.split(r"\s*(?:,|;|\band\b|\baur\b)\s*", processing_text, flags=re.IGNORECASE)
            if clause.strip()
        ]
    if not clauses:
        clauses = [processing_text.strip()]

    # A comma may separate a customer name from the rest of one command,
    # e.g. "Ramesh, write 340 rupees as credit."  Merge such fragments so
    # they are not treated as separate transactions.
    merged_clauses = []
    for clause in clauses:
        if merged_clauses and not _clause_has_amount(merged_clauses[-1]) and _clause_has_amount(clause):
            merged_clauses[-1] = merged_clauses[-1] + " " + clause
        else:
            merged_clauses.append(clause)
    clauses = merged_clauses

    return [parse_command(clause) for clause in clauses]
