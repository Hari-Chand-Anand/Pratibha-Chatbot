"""Claude-driven question queue construction (Migration #004).

WHEN INVOKED
    Once at session start, when CLAUDE_QUEUE_ENABLED=true and ANTHROPIC_API_KEY
    is set. Falls back to csv_parser_queue.build_question_queue on any error.

WHY CLAUDE, NOT QWEN
    - Reads activity notes + Sourcewise requirement + touch history and writes
      note-aware, non-generic questions (the missing capability that made 26 Jun
      chats fill with "How many times did you try calling — exact number?" on
      leads Pratibha had already answered).
    - Applies deterministic priority — high-POV first, then blank notes, then
      stale multi-touch, then vague, then rest — but WITHIN a priority tier
      Claude decides the phrasing.

INPUT
    Rows already assembled by build_question_queue's SQL — the same lead,
    inquiries, last_response, touch_count. This function is a REPLACEMENT
    for _build_question_for_customer's phrasing step, not a re-query.

OUTPUT
    A list of {lead_id, mobile_number, contact_name, city, question,
    trigger, original_requirement, priority_score, touch_count}.

    priority_score: lower = asked first. 0..99 tiers.
"""
import json
import logging
import os
from typing import Iterable

from hard_junk import extract_pov_inr, extract_quantity, POV_FORCE_RESURFACE

logger = logging.getLogger(__name__)

_CLAUDE_ENABLED = os.environ.get("CLAUDE_QUEUE_ENABLED", "false").lower() == "true"
_MODEL = os.environ.get("CLAUDE_QUEUE_MODEL", "claude-sonnet-5")
_MAX_TOKENS = 4000
_MAX_LEADS_PER_CALL = 30   # keep prompt bounded

try:
    from anthropic import Anthropic
    _key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    _client = Anthropic(api_key=_key) if _key else None
except Exception:
    _client = None


# ─────────────────────────────────────────────────────────────────────────────
# Priority scoring — deterministic, applied before AND after Claude phrasing
# ─────────────────────────────────────────────────────────────────────────────

def _priority_score(lead_context: dict) -> int:
    """Lower = asked first. Runs before Claude sees the leads so the order in
    the prompt already reflects urgency; Claude cannot reorder."""
    req = lead_context.get("original_requirement") or ""
    note = (lead_context.get("activity_note") or "").lower()
    stage = (lead_context.get("lead_stage") or "").lower()
    touch = lead_context.get("touch_count") or 0

    pov = extract_pov_inr(req)
    qty = extract_quantity(req)

    # Tier 0 — critical flags. High-POV junked or blank.
    if (pov and pov >= POV_FORCE_RESURFACE) or (qty and qty > 5):
        if stage == "junk" or not note.strip():
            return 0
        return 10

    # Tier 1 — blank note (no activity at all)
    if not note.strip() and stage in ("", "yet to talk", "new"):
        return 20

    # Tier 2 — stale multi-touch
    if touch >= 2:
        return 30

    # Tier 3 — "sent details" without model/price (data-quality gap)
    if "sent details" in note and not lead_context.get("last_machine_sent"):
        return 40

    # Tier 4 — vague notes
    if any(v in note for v in ("will check", "call after", "not required")):
        return 50

    # Tier 5 — everything else
    return 60


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
# ─────────────────────────────────────────────────────────────────────────────

def _lead_json_line(lead: dict) -> dict:
    """Trim a lead dict to the fields Claude actually needs — keeps prompt small."""
    return {
        "id": lead.get("lead_id"),
        "mobile": lead.get("mobile_number"),
        "name": lead.get("contact_name"),
        "city": lead.get("city"),
        "note": (lead.get("activity_note") or "")[:140],
        "requirement": (lead.get("original_requirement") or "")[:160],
        "stage": lead.get("lead_stage"),
        "touch": lead.get("touch_count") or 0,
        "cratio_ts": lead.get("cratio_ts"),
        "last_machine_sent": lead.get("last_machine_sent"),
        "last_answer": (lead.get("last_answer") or "")[:120],
    }


SYSTEM = """You are the HCA Company Brain. You build a daily question queue for
Pratibha, a sales rep. Each question must be note-aware (read the activity note
AND original requirement before writing), reference the CRM date/time when a
lead is being revisited, and avoid asking things the note already answers.

STRICT RULES:
1. Return one question per lead. Do NOT concatenate multiple questions.
2. If touch >= 1, prefix with "(Touch <N>/4 — Cratio <date>) " so Pratibha can
   identify the lead instantly.
3. If note says "language barrier" or "not garment industry", ask a CLOSING
   question ("Confirming junk — final?"), never a re-litigating one.
4. If requirement has POV ≥ ₹1L or quantity > 5, LEAD with the value:
   "This inquiry was for X pieces / ₹Y — did you follow up?"
5. If note says "sent details" without a model, ask: "Which model did you send
   to <name>? And what price did you quote?"
6. No generic templates. Every question must reference specific fields.
7. Output MUST be valid JSON: an array of {id, question}."""


def _build_prompt(leads: list[dict]) -> str:
    trimmed = [_lead_json_line(l) for l in leads[:_MAX_LEADS_PER_CALL]]
    return (
        "LEADS FOR TODAY (pre-sorted by priority — do not reorder):\n"
        + json.dumps(trimmed, ensure_ascii=False, indent=2)
        + "\n\nWrite one question per lead. Return JSON array only:\n"
        + '[{"id": <lead_id>, "question": "<the question>"}, ...]'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def build_claude_queue(leads: Iterable[dict]) -> list[dict]:
    """Given already-fetched leads (with note, requirement, touch, etc.), return
    the priority-ordered queue with Claude-written questions. Returns [] if
    Claude is disabled/unavailable — caller must fall back."""
    if not _CLAUDE_ENABLED or _client is None:
        return []

    leads_list = list(leads)
    if not leads_list:
        return []

    # Deterministic sort BEFORE the LLM call so Claude sees the correct order.
    leads_sorted = sorted(leads_list, key=_priority_score)

    try:
        resp = _client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            system=SYSTEM,
            messages=[{"role": "user", "content": _build_prompt(leads_sorted)}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
    except Exception as e:
        logger.warning("Claude queue build failed: %s", e)
        return []

    # Parse JSON — Claude sometimes wraps in ```json fences. Strip them.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        questions_by_id = {q["id"]: q["question"] for q in json.loads(text)}
    except Exception as e:
        logger.warning("Claude queue JSON parse failed: %s (text=%.200r)", e, text)
        return []

    # Stitch Claude phrasing back onto the priority-sorted lead list.
    queue = []
    for lead in leads_sorted:
        lid = lead.get("lead_id")
        q = questions_by_id.get(lid)
        if not q:
            continue
        queue.append({
            "lead_id": lid,
            "mobile_number": lead.get("mobile_number"),
            "contact_name": lead.get("contact_name"),
            "city": lead.get("city"),
            "question": q,
            "trigger": lead.get("trigger") or "claude_generated",
            "original_requirement": lead.get("original_requirement"),
            "touch_count": lead.get("touch_count"),
            "priority_score": _priority_score(lead),
        })
    logger.info("Claude queue built: %s questions", len(queue))
    return queue
