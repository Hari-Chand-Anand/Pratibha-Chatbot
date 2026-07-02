"""Turn-level tracing + auto-flag logic (Migration #004).

Every LLM turn writes one row to pratibha_agent_traces. Deterministic checks
run at write-time and populate auto_flags — these are the same checks the
offline eval harness runs, so production failures show up in the daily monitor
AND automatically become candidates for the regressions dataset.

DESIGN NOTES:
  - Writing is best-effort. If the trace insert fails, we log and continue —
    a broken trace must never break the user's session.
  - Auto-flags are deterministic (regex / rule-based). Any LLM-judge scoring
    happens later in the eval harness, not here.
  - input_state is JSON-serialised with a size cap. LangChain message objects
    are stripped down to {role, content} before serialisation.
"""
import json
import logging
import os
import re
from datetime import date as date_cls
from typing import Any

from csv_parser import get_db_conn
from hard_junk import extract_pov_inr, extract_quantity, POV_FORCE_RESURFACE

logger = logging.getLogger(__name__)

TRACES_ENABLED = os.environ.get("TRACES_ENABLED", "true").lower() == "true"
INPUT_STATE_CAP_BYTES = 32_000    # per-row JSON size limit

# ─────────────────────────────────────────────────────────────────────────────
# Auto-flag detectors — one per Blocker metric that can be checked from a
# single turn. Aggregated metrics (session completion, count accuracy) run
# in monitor_writer.py, not here.
# ─────────────────────────────────────────────────────────────────────────────

def _flag_repeat_question(input_state: dict, llm_output: str) -> bool:
    """A1 — repeat-question rate.
    True if the current question is textually identical (case-insensitive,
    whitespace-normalised) to any prior agent turn on the SAME lead in this
    session's message history."""
    lead_id = (input_state.get("current_question") or {}).get("lead_id")
    if not lead_id:
        return False
    normed = re.sub(r"\s+", " ", (llm_output or "").strip().lower())
    if not normed:
        return False
    for m in input_state.get("prior_agent_messages_on_lead") or []:
        if re.sub(r"\s+", " ", (m or "").strip().lower()) == normed:
            return True
    return False


def _flag_extraction_missed(input_state: dict, user_reply: str) -> bool:
    """A3 — field extraction accuracy.
    True if Pratibha's reply CONTAINS a model/price/attempt number but the
    per-lead extraction accumulator still shows it as missing."""
    reply_l = (user_reply or "").lower()
    extracted = input_state.get("extracted_so_far") or {}

    # Model code present in reply but missing from extraction
    has_model_in_reply = bool(re.search(
        r"\b(?:dy|dlr|dfb|ls|lu|es|zoje|kansai|b\d+|f5|f7|cm|lew|hw)"
        r"[-\s]?\d{2,5}[a-z]{0,3}\b",
        reply_l,
    ))
    if has_model_in_reply and not extracted.get("machine_sent"):
        return True

    # Numeric price mentioned in reply but missing
    has_price = bool(re.search(r"\b\d{4,7}\b", reply_l)) and any(
        w in reply_l for w in ("price", "rs", "rupee", "₹", "gst", "quote")
    )
    if has_price and not extracted.get("price_quoted_inr"):
        return True

    # Attempts number stated but missing
    has_attempts = bool(re.search(
        r"\b(?:1|2|3|4|5|once|twice|thrice|multiple)\b",
        reply_l,
    )) and any(w in reply_l for w in ("call", "attempt", "tried", "try", "rung"))
    if has_attempts and extracted.get("call_attempts") is None:
        return True

    return False


def _flag_resurface_missing_date(input_state: dict, llm_output: str) -> bool:
    """A5 — resurface opener must contain Cratio date/time.
    Only applies when trigger is a resurface trigger (followup_touch,
    returning_customer, multi_inquiry) AND touch_count >= 1."""
    current = input_state.get("current_question") or {}
    trigger = (current.get("trigger") or "").lower()
    touch = input_state.get("touch_count") or 0
    if touch < 1 or trigger not in ("followup_touch", "returning_customer", "multi_inquiry"):
        return False
    text = (llm_output or "").lower()
    # look for a date pattern like "24 jun" or "24 june" — narrow but robust
    has_date = bool(re.search(
        r"\b\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
        text,
    ))
    has_touch_marker = "touch" in text and "/4" in text
    return not (has_date and has_touch_marker)


def _flag_high_pov_flag_missed(input_state: dict, llm_output: str) -> bool:
    """A7 — high-POV lead must be flagged in the opener.
    True if the current lead has POV ≥ ₹1L or bulk quantity but the agent's
    opener doesn't mention the value (₹, lakh, crore, quantity)."""
    current = input_state.get("current_question") or {}
    req = current.get("original_requirement") or ""
    pov = extract_pov_inr(req)
    qty = extract_quantity(req)
    high_pov = (pov is not None and pov >= POV_FORCE_RESURFACE) \
             or (qty is not None and qty > 5)
    if not high_pov:
        return False
    text = (llm_output or "").lower()
    mentions_value = any(w in text for w in ("₹", "rs", "lakh", "crore", "pieces", "piece", "pcs", "quantity"))
    return not mentions_value


def _flag_terminal_ignored(input_state: dict, user_reply: str, llm_output: str) -> bool:
    """A2 — first-turn acceptance.
    True if Pratibha's reply is a terminal answer (junk, language, no need)
    but the agent asked ANOTHER question about the same lead instead of
    moving on."""
    reply_l = (user_reply or "").lower()
    terminal_hits = ("junk", "language issue", "language barrier", "not a buyer",
                     "no requirement", "we don't sell", "we do not sell",
                     "not garment industry", "wrong industry", "just checking")
    if not any(t in reply_l for t in terminal_hits):
        return False
    # If the agent's next output ends with a question mark AND references the
    # same lead, that's a violation.
    return "?" in (llm_output or "")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — called from agent.answer_received_node
# ─────────────────────────────────────────────────────────────────────────────

def _detect_all_flags(input_state: dict, llm_output: str, user_reply: str) -> list[str]:
    flags = []
    try:
        if _flag_repeat_question(input_state, llm_output):
            flags.append("repeat_question")
        if _flag_extraction_missed(input_state, user_reply):
            flags.append("extraction_missed")
        if _flag_resurface_missing_date(input_state, llm_output):
            flags.append("resurface_missing_date")
        if _flag_high_pov_flag_missed(input_state, llm_output):
            flags.append("high_pov_flag_missed")
        if _flag_terminal_ignored(input_state, user_reply, llm_output):
            flags.append("terminal_ignored")
    except Exception as e:  # never let flag detection break tracing
        logger.warning("Flag detection error: %s", e)
    return flags


def _safe_serialise(obj: Any) -> str:
    """Convert LangChain messages + arbitrary state to a bounded JSON string."""
    def _default(o):
        if hasattr(o, "content"):
            return {"role": getattr(o, "type", "message"), "content": o.content}
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)
    try:
        s = json.dumps(obj, default=_default, ensure_ascii=False)
    except Exception:
        s = json.dumps({"_serialisation_error": True})
    if len(s) > INPUT_STATE_CAP_BYTES:
        s = s[:INPUT_STATE_CAP_BYTES - 20] + '..."_TRUNCATED_"}'
    return s


def write_trace(
    *,
    session_date: str,
    thread_id: str | None,
    lead_id: int | None,
    mobile_number: str | None,
    turn_index: int,
    trigger_type: str | None,
    touch_count: int | None,
    input_state: dict,
    llm_output: str,
    user_reply: str,
    latency_ms: int | None = None,
) -> list[str]:
    """Write one trace row. Returns the auto_flags list (empty if all checks passed).
    Best-effort — never raises."""
    if not TRACES_ENABLED:
        return []
    flags = _detect_all_flags(input_state, llm_output, user_reply)
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pratibha_agent_traces
              (session_date, thread_id, lead_id, mobile_number, turn_index,
               trigger_type, touch_count, input_state, llm_output, user_reply,
               auto_flags, latency_ms)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
        """, (session_date, thread_id, lead_id, mobile_number, turn_index,
              trigger_type, touch_count, _safe_serialise(input_state),
              llm_output, user_reply, flags, latency_ms))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("write_trace failed: %s", e)
    return flags
