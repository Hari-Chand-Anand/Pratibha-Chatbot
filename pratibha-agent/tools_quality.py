"""Data-quality layer for save_response (Migration #003).
Expanded LLM extraction + completeness scoring + persistence of new fields.
Split out to keep tools.py under the per-file size limit."""

import os
import re
import logging
from required_fields import compute_completeness_score, missing_fields, next_followup_question

logger = logging.getLogger(__name__)


EXTRACTION_PROMPT = """You are a data extractor. Output ONLY a raw JSON object. No prose. No markdown.

Sales rep answered about a sales lead:
Question: "{question}"
Answer:   "{answer}"

Output this exact JSON structure (use null for missing fields):
{{
  "machine_sent": null,
  "price_quoted_inr": null,
  "customer_response_status": null,
  "visit_date": null,
  "call_attempts": null,
  "next_action": null,
  "next_action_date": null,
  "follow_up_plan": null,
  "dropout_status": null,
  "why_not_required": null,
  "future_potential": null,
  "actual_customer_response": null,
  "junk_reason": null,
  "forwarded_to_name": null,
  "handoff_status": null,
  "callback_outcome": null,
  "summary_line": "one short phrase, max 10 words"
}}

Rules:
- machine_sent: extract ONLY the literal model name/number the rep mentioned
  (e.g. "DY-1201", "9503-d1", "DLR 1508P"). NEVER write "told above" /
  "as mentioned" / "previously" / "see above" — if the rep didn't name a model
  in THIS answer, return null. Don't invent or paraphrase.
- price_quoted_inr: numeric rupees (e.g. 95000 not "95k"), 0 if rep explicitly
  said no price was quoted (e.g. "sent catalog only"), or null if unknown.
- customer_response_status: one of "awaiting" | "positive" | "revision_requested"
  | "visit_planned" | "no_answer" | "declined" | null.
- visit_date: ISO YYYY-MM-DD or null.
- call_attempts: integer. Pratibha saying "yet to talk" or "called but didnt
  pick" means at least 1 attempt — not 0. Use 0 only when she explicitly said
  she hasn't tried.
- next_action: one of "call" | "visit" | "quote_revision" | "whatsapp" | "junk"
  | null.
- next_action_date: ISO YYYY-MM-DD or null.
- dropout_status: "ordered" | "declined" | null (only if customer definitively
  bought or refused).
- All other fields: short string or null.

Few-shot examples (study these carefully before extracting):

Q: "Which exact model did you send — DY-1201, ZOJE HS, something else? I need the model number."
A: "dy 6800-ds overlock"
→ {{"machine_sent": "DY 6800-DS", "price_quoted_inr": null, "call_attempts": null, "customer_response_status": null, "next_action": null, "summary_line": "sent DY 6800-DS overlock"}}

Q: "What price did you quote — exact figure in rupees?"
A: "36000 + gst"
→ {{"machine_sent": null, "price_quoted_inr": 36000, "call_attempts": null, "customer_response_status": null, "summary_line": "quoted Rs 36000 + GST"}}

Q: "What price did you quote — exact figure in rupees?"
A: "0"
→ {{"machine_sent": null, "price_quoted_inr": 0, "call_attempts": null, "customer_response_status": null, "summary_line": "no price quoted, catalog only"}}

Q: "Which exact model did you send?"
A: "just told you above"
→ {{"machine_sent": null, "price_quoted_inr": null, "call_attempts": null, "summary_line": "already stated in prior answer"}}
(IMPORTANT: "told above", "as mentioned", "same as before" are parroting — machine_sent must be null)

Q: "How many times did you try calling — exact number?"
A: "2"
→ {{"machine_sent": null, "price_quoted_inr": null, "call_attempts": 2, "next_action": null, "summary_line": "called 2 times"}}

Q: "Has the customer replied — yes positively, no response, revision requested, or declined?"
A: "did not responded"
→ {{"machine_sent": null, "price_quoted_inr": null, "call_attempts": null, "customer_response_status": "no_answer", "summary_line": "customer did not respond"}}

Q: "For Ramesh — you sent details. Which machine/model did you send? What was the price quoted? Have they responded?"
A: "sent 9503-d1 overlock, quoted 42000, no response from customer"
→ {{"machine_sent": "9503-D1", "price_quoted_inr": 42000, "call_attempts": null, "customer_response_status": "no_answer", "summary_line": "sent 9503-D1, Rs 42000, no response"}}

Output the JSON now:"""


def parse_price(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower().replace(",", "").replace("rs", "").replace("₹", "").strip()
    mult = 1
    if "lakh" in s or "l " in s or s.endswith("l"):
        mult = 100000
        s = s.replace("lakh", "").replace("l", "").strip()
    elif s.endswith("k") or "thousand" in s:
        mult = 1000
        s = s.replace("k", "").replace("thousand", "").strip()
    m = re.search(r"\d+\.?\d*", s)
    if not m:
        return None
    try:
        return float(m.group(0)) * mult
    except ValueError:
        return None


def parse_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        m = re.search(r"\d+", str(v))
        return int(m.group(0)) if m else None


def parse_iso_date(v):
    """Returns ISO date string or None. Accepts YYYY-MM-DD and a few common variants."""
    if v is None or v == "":
        return None
    from datetime import datetime
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# Phrases that mean "I already told you" rather than an actual model name.
# When the LLM extracts these, the user is referring to a prior answer — so
# we reject the value (return null) instead of treating it as a model.
_MODEL_PARROT_TOKENS = (
    "told",            # "told you above"
    "above",           # "see above", "as above"
    "previously",
    "as mentioned",
    "mentioned",
    "as said",
    "as told",
    "earlier said",
    "earlier",
    "same as",
)


def clean_model(value):
    """Reject 'told above'-style parroting. Otherwise normalise whitespace and
    keep the value as-is so downstream digest aggregation can dedup."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    low = s.lower()
    # If the LLM regurgitated a "I already told you" phrase, drop it.
    if any(p in low for p in _MODEL_PARROT_TOKENS):
        return None
    # Reject when it literally matches the prompt's example placeholders.
    if low in {"dy-1201, zoje hs", "dy-1201,zoje hs", "<model_number>", "model_number"}:
        return None
    return s


def build_response_row(extracted: dict) -> dict:
    """Normalise LLM-extracted dict into the column-typed dict we'll write."""
    if not isinstance(extracted, dict):
        extracted = {}
    return {
        "machine_sent":              clean_model(extracted.get("machine_sent")),
        "price_quoted_inr":          parse_price(extracted.get("price_quoted_inr")),
        "customer_response_status":  (extracted.get("customer_response_status") or None),
        "visit_date":                parse_iso_date(extracted.get("visit_date")),
        "call_attempts":             parse_int(extracted.get("call_attempts")),
        "next_action":               (extracted.get("next_action") or None),
        "next_action_date":          parse_iso_date(extracted.get("next_action_date")),
        "follow_up_plan":            (extracted.get("follow_up_plan") or None),
        "why_not_required":          (extracted.get("why_not_required") or None),
        "future_potential":          (extracted.get("future_potential") or None),
        "actual_customer_response":  (extracted.get("actual_customer_response") or None),
        "junk_reason":               (extracted.get("junk_reason") or None),
        "forwarded_to_name":         (extracted.get("forwarded_to_name") or None),
        "handoff_status":            (extracted.get("handoff_status") or None),
        "callback_outcome":          (extracted.get("callback_outcome") or None),
        "summary_line":              (extracted.get("summary_line") or None),
        "dropout_status":            (extracted.get("dropout_status") or None),
    }


def persist_quality_fields(cur, response_id: int, row: dict, trigger: str, score: int):
    """UPDATE pratibha_responses with all the Migration #003 fields."""
    cur.execute("""
        UPDATE pratibha_responses
        SET price_quoted_inr=%s, customer_response_status=%s, visit_date=%s,
            next_action=%s, next_action_date=%s,
            why_not_required=%s, future_potential=%s, actual_customer_response=%s,
            junk_reason=%s, forwarded_to_name=%s, handoff_status=%s,
            callback_outcome=%s, trigger_type=%s, completeness_score=%s
        WHERE id = %s
    """, (
        row.get("price_quoted_inr"),
        row.get("customer_response_status"),
        row.get("visit_date"),
        row.get("next_action"),
        row.get("next_action_date"),
        row.get("why_not_required"),
        row.get("future_potential"),
        row.get("actual_customer_response"),
        row.get("junk_reason"),
        row.get("forwarded_to_name"),
        row.get("handoff_status"),
        row.get("callback_outcome"),
        trigger,
        score,
        response_id,
    ))


def is_enabled() -> bool:
    return os.environ.get("DATA_QUALITY_ENABLED", "true").lower() != "false"


def evaluate_quality(trigger: str, row: dict, already_asked: list = None):
    """Returns (completeness_score, missing_list, followup_question_or_empty, asked_field).
    Migration #004 phase 2: `already_asked` lets us skip fields we already prompted
    on so the same missing field isn't asked twice — that's the loop guard."""
    score = compute_completeness_score(trigger, row)
    miss = missing_fields(trigger, row)
    if miss:
        asked_field, followup = next_followup_question(trigger, row, already_asked)
    else:
        asked_field, followup = "", ""
    return score, miss, followup, asked_field


# ─────────────────────────────────────────────────────────────────────────────
# Migration #004 phase 2 — context-aware extraction.
#
# The LLM was failing on short replies like "2", "0", "36000 + gst", "dy 6800-ds"
# because bare answers lack context. This deterministic pre-parser looks at the
# QUESTION that was asked and maps a matching answer directly to the field it
# was about. Runs BEFORE the LLM extraction so it always wins for these clean
# cases. Groq fills in gaps for everything else.
# ─────────────────────────────────────────────────────────────────────────────

_MODEL_CODE = re.compile(
    r"\b(?:dy|dlr|dfb|ls|lu|es|zoje|kansai|b\d+|f5|f7|cm|lew|hw|hikari|juki|jack)"
    r"[-\s]?\d{2,5}[a-z]{0,3}(?:-[a-z0-9]+)?\b",
    re.IGNORECASE,
)


def _looks_like_call_attempts_q(q: str) -> bool:
    q = q.lower()
    return ("how many" in q and ("call" in q or "tri" in q or "attempt" in q)) \
        or "attempts" in q


def _looks_like_price_q(q: str) -> bool:
    q = q.lower()
    return "price" in q or "figure in rupee" in q or "quote" in q


def _looks_like_model_q(q: str) -> bool:
    q = q.lower()
    return ("exact model" in q or "which model" in q or "model number" in q
            or "model did you send" in q)


def _looks_like_next_action_q(q: str) -> bool:
    q = q.lower()
    return "next action" in q or "call back, send revision" in q


def _looks_like_response_status_q(q: str) -> bool:
    q = q.lower()
    return "customer replied" in q or "no response, revision" in q


_WORD_NUMBERS = {"once": 1, "twice": 2, "thrice": 3,
                 "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "multiple": 3}   # "multiple" → conservative estimate

_NEXT_ACTION_MAP = {
    "call back": "call", "call": "call", "callback": "call", "kal call": "call",
    "send revision": "quote_revision", "revised quote": "quote_revision",
    "revision": "quote_revision",
    "schedule visit": "visit", "visit": "visit",
    "whatsapp": "whatsapp", "wa": "whatsapp",
    "junk": "junk", "mark junk": "junk", "discard": "junk",
}

_STATUS_MAP = {
    "yes": "positive", "positive": "positive",
    "no response": "no_response", "no reply": "no_response",
    "no answer": "no_response", "silent": "no_response",
    "not responded": "no_response", "did not respond": "no_response",
    "did not responded": "no_response",   # observed Pratibha typo (26-Jun chat)
    "didnt respond": "no_response", "didn't respond": "no_response",
    "revision requested": "revision_requested", "revision": "revision_requested",
    "asked for revision": "revision_requested", "price high": "revision_requested",
    "prices were high": "revision_requested",
    "declined": "declined", "refused": "declined", "not interested": "declined",
}


def extract_from_context(question: str, answer: str) -> dict:
    """Deterministically extract fields from Pratibha's answer using the
    question as context. Populated fields override the LLM's guess.
    Returns dict with only the fields it could confidently extract."""
    q = (question or "").lower()
    a = (answer or "").strip()
    a_low = a.lower()
    out = {}
    if not a:
        return out

    # call_attempts — bare digit or "once/twice/multiple" when the question
    # was about call count. Also "0" and "not called" are valid answers.
    if _looks_like_call_attempts_q(q):
        m = re.search(r"\b(\d+)\b", a)
        if m:
            try:
                out["call_attempts"] = int(m.group(1))
            except ValueError:
                pass
        for w, n in _WORD_NUMBERS.items():
            if w in a_low:
                out["call_attempts"] = n; break
        if a_low in ("not called", "no", "nahi", "nahin", "zero"):
            out["call_attempts"] = 0
        elif a_low == "yet to talk" and "call_attempts" not in out:
            out["call_attempts"] = 1   # yet-to-talk = called once in Cratio convention

    # price_quoted_inr — question was about price and answer has a rupee figure.
    if _looks_like_price_q(q):
        # "36000 + gst" / "Rs 36,000" / "36k" / "1.5 lakh" / "0"
        p = parse_price(a)
        if p is not None:
            out["price_quoted_inr"] = float(p)
        elif a_low in ("0", "zero", "no price", "catalog only"):
            out["price_quoted_inr"] = 0.0

    # machine_sent — model code present in answer AND question was about model.
    if _looks_like_model_q(q) or _MODEL_CODE.search(a):
        m = _MODEL_CODE.search(a)
        if m:
            out["machine_sent"] = m.group(0).strip()

    # next_action — free-text mapped to a canonical value.
    if _looks_like_next_action_q(q):
        for phrase, canon in _NEXT_ACTION_MAP.items():
            if phrase in a_low:
                out["next_action"] = canon; break

    # customer_response_status — free-text mapped. Try even when the question
    # wasn't specifically about response, because a "sent details" reply often
    # contains BOTH model info AND status ("dy 6800-ds ... they did not responded").
    for phrase, canon in _STATUS_MAP.items():
        if phrase in a_low:
            out.setdefault("customer_response_status", canon)
            break

    return out
