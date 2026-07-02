"""Layer 1 — deterministic evaluation checks.

Same rule engine that traces.py uses for real-time auto-flags, but here it's
invoked against labelled dataset rows so we can compute pass/fail per metric.
Zero LLM cost. Runs in seconds.
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Per-case check result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    metric: str
    passed: bool
    detail: str = ""
    severity: str = "blocker"


# ─────────────────────────────────────────────────────────────────────────────
# A1 — Repeat-question rate
# ─────────────────────────────────────────────────────────────────────────────

def check_a1_repeat(agent_output: str, prior_agent_msgs: list[str]) -> CheckResult:
    normed = re.sub(r"\s+", " ", (agent_output or "").strip().lower())
    for prior in prior_agent_msgs or []:
        if re.sub(r"\s+", " ", (prior or "").strip().lower()) == normed:
            return CheckResult("A1", False, "duplicate of prior turn", "blocker")
    return CheckResult("A1", True)


# ─────────────────────────────────────────────────────────────────────────────
# A2 — First-turn answer acceptance
#
# 02 Jul 2026 fix: this used to (a) grade a frozen expected["agent_next"]
# field that was hardcoded to "" in every dataset row — meaning the check
# always passed no matter what the code actually did — and (b) maintain its
# own separate, drifted list of "terminal" phrases instead of reusing
# agent.py's real is_terminal_answer(). Both are fixed: agent_next is now
# whatever the live/candidate output ACTUALLY was (see run_eval.py's
# simulate_agent_next), and terminality is decided by the same function
# production uses, imported directly so the two can never disagree again.
# ─────────────────────────────────────────────────────────────────────────────

try:
    from agent import is_terminal_answer
except Exception:  # pragma: no cover — keeps this module importable in
    # contexts where agent.py's heavier deps (langchain/langgraph/groq) aren't
    # installed, e.g. a lightweight lint pass. Falls back to a small local
    # list rather than crashing; production behaviour is unaffected either way.
    _FALLBACK_TERMINAL = (
        "junk", "language issue", "language barrier", "not a buyer", "no requirement",
        "we don't sell", "we do not sell", "not garment industry", "wrong industry",
        "just checking",
    )

    def is_terminal_answer(answer: str) -> bool:
        a = (answer or "").lower()
        return any(t in a for t in _FALLBACK_TERMINAL)


def check_a2_first_turn_acceptance(user_reply: str, agent_next: str) -> CheckResult:
    """If reply is terminal, agent should NOT ask another question."""
    if not is_terminal_answer(user_reply or ""):
        return CheckResult("A2", True, "not-terminal reply, N/A")
    if "?" in (agent_next or ""):
        return CheckResult(
            "A2", False,
            "terminal reply followed by another question",
            "blocker",
        )
    return CheckResult("A2", True)


# ─────────────────────────────────────────────────────────────────────────────
# A3 — Field extraction accuracy
# ─────────────────────────────────────────────────────────────────────────────

def check_a3_extraction(user_reply: str, extracted: dict, expected: dict) -> CheckResult:
    misses = []
    for field_name, expected_val in (expected or {}).items():
        actual = extracted.get(field_name)
        if expected_val is None:
            continue
        if actual is None:
            misses.append(f"{field_name} not extracted (expected {expected_val!r})")
        elif isinstance(expected_val, (int, float)) and isinstance(actual, (int, float)):
            if abs(float(actual) - float(expected_val)) > 0.01:
                misses.append(f"{field_name}={actual!r} vs expected {expected_val!r}")
        else:
            if str(actual).strip().lower() != str(expected_val).strip().lower():
                misses.append(f"{field_name}={actual!r} vs expected {expected_val!r}")
    if misses:
        return CheckResult("A3", False, "; ".join(misses), "blocker")
    return CheckResult("A3", True)


# ─────────────────────────────────────────────────────────────────────────────
# A5 — Resurface opener contains CRM date/time + touch count
# ─────────────────────────────────────────────────────────────────────────────

DATE_RE = re.compile(
    r"\b\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    re.IGNORECASE,
)


def check_a5_resurface_opener(agent_output: str, touch_count: int, trigger: str) -> CheckResult:
    if (touch_count or 0) < 1:
        return CheckResult("A5", True, "not a resurface, N/A")
    if trigger not in ("followup_touch", "returning_customer", "multi_inquiry", "claude_generated"):
        return CheckResult("A5", True, "not a resurface trigger, N/A")
    text = agent_output or ""
    has_date = bool(DATE_RE.search(text))
    has_touch = "touch" in text.lower() and "/4" in text
    if not has_date:
        return CheckResult("A5", False, "opener missing CRM date", "blocker")
    if not has_touch:
        return CheckResult("A5", False, "opener missing 'Touch N/4' marker", "blocker")
    return CheckResult("A5", True)


# ─────────────────────────────────────────────────────────────────────────────
# A6 — Session completion (aggregate, not per-turn)
# ─────────────────────────────────────────────────────────────────────────────

def check_a6_session_completion(covered: int, queued: int) -> CheckResult:
    if queued == 0:
        return CheckResult("A6", True, "empty queue, N/A")
    rate = covered / queued
    if rate < 0.80:
        return CheckResult("A6", False, f"{covered}/{queued} = {rate:.0%}", "high")
    return CheckResult("A6", True, f"{covered}/{queued} = {rate:.0%}")


# ─────────────────────────────────────────────────────────────────────────────
# A7 — High-POV flag fires on qualifying leads
# ─────────────────────────────────────────────────────────────────────────────

from hard_junk import extract_pov_inr, extract_quantity, POV_FORCE_RESURFACE


def check_a7_high_pov(agent_output: str, original_requirement: str) -> CheckResult:
    pov = extract_pov_inr(original_requirement or "")
    qty = extract_quantity(original_requirement or "")
    triggers = (pov is not None and pov >= POV_FORCE_RESURFACE) \
             or (qty is not None and qty > 5)
    if not triggers:
        return CheckResult("A7", True, "not a high-POV lead, N/A")
    text = (agent_output or "").lower()
    mentions = any(w in text for w in ("₹", " rs", "lakh", "crore", "piece", "pcs", "quantity"))
    if not mentions:
        detail = f"POV={pov} qty={qty}, opener does not mention value"
        return CheckResult("A7", False, detail, "blocker")
    return CheckResult("A7", True)


# ─────────────────────────────────────────────────────────────────────────────
# B1 — Summary count accuracy
# ─────────────────────────────────────────────────────────────────────────────

def check_b1_summary_counts(summary_text: str, ground_truth: dict) -> CheckResult:
    """ground_truth: {'contacted': int, 'details_sent': int, 'junked': int, ...}

    Narrative summaries express counts multiple ways:
      - "Contacted by Pratibha | 12"      (table cell)
      - "4 of 19 reached details-sent"   (ratio)
      - "12 contacted"                   (adjective)
    Any of these should count. We look for the expected number appearing
    within 40 chars of a label synonym OR the label word appearing anywhere.
    Zero is a valid value — we still require it to be found."""
    misses = []
    LABEL_ALIASES = {
        "contacted":         ("contacted", "contact"),
        "details_sent":      ("details.sent", "details-sent", "quoted", "quotation"),
        "junked":            ("junk", "junked", "closed as junk"),
        "pending":           ("pending", "unreached", "retry"),
        "orders_today":      ("order", "orders", "confirmed"),
        "auto_junked_today": ("auto.junked", "auto-junked", "touch.4", "touch-4"),
        "total_leads":       ("leads", "reviewed"),
        "cold":              ("cold", "first contact"),
    }
    text = (summary_text or "").lower()
    for label, expected in (ground_truth or {}).items():
        aliases = LABEL_ALIASES.get(label, (label.replace("_", " "),))
        exp_str = str(expected)
        # Pass conditions (any of):
        #   (a) number appears within 60 chars of a label alias (either dir)
        #   (b) number appears in an "N of M" or "N/M" ratio phrase
        #   (c) if any alias appears AND the number appears anywhere in text
        found = False
        for alias in aliases:
            if re.search(rf"{alias}[\s\S]{{0,60}}\b{exp_str}\b", text):
                found = True; break
            if re.search(rf"\b{exp_str}\b[\s\S]{{0,60}}{alias}", text):
                found = True; break
        if not found:
            # ratio "N of M" or "N/M" — if either side matches expected, count it
            ratio_match = re.search(rf"\b{exp_str}\b\s*(?:of|/)\s*\d+", text) \
                       or re.search(rf"\d+\s*(?:of|/)\s*\b{exp_str}\b", text)
            if ratio_match and any(alias in text for alias in aliases):
                found = True
        if not found and any(alias in text for alias in aliases):
            # Alias present, number present anywhere — accept as loose match
            if re.search(rf"\b{exp_str}\b", text):
                found = True
        if not found:
            misses.append(f"{label}={expected} not found (aliases {aliases})")
    if misses:
        return CheckResult("B1", False, "; ".join(misses), "blocker")
    return CheckResult("B1", True)


# ─────────────────────────────────────────────────────────────────────────────
# B2 — Summary format conformance
# ─────────────────────────────────────────────────────────────────────────────

SECTION_PATTERNS = {
    "money":  ("money committed", "money moved", "money"),
    "gut":    ("gut-check", "gut check", "quick gut"),
    "raise":  ("worth raising", "raising with", "two things", "raise", "team"),
}


def check_b2_summary_format(summary_text: str) -> CheckResult:
    text = (summary_text or "").lower()
    missing = []
    for section, patterns in SECTION_PATTERNS.items():
        if not any(p in text for p in patterns):
            missing.append(section)
    if missing:
        return CheckResult(
            "B2", False,
            f"missing sections: {missing}", "blocker",
        )
    return CheckResult("B2", True)


# ─────────────────────────────────────────────────────────────────────────────
# Runner — evaluate one case
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CaseOutcome:
    case_id: str
    bucket: str
    results: list = field(default_factory=list)

    def any_blocker_failed(self) -> bool:
        return any(not r.passed and r.severity == "blocker" for r in self.results)


def evaluate_case(case: dict, agent_output: str, extracted: dict = None,
                  prior_agent_msgs: list = None, agent_next: str = None) -> CaseOutcome:
    """Run all deterministic checks that apply to this case.

    agent_next (02 Jul 2026): the REAL next agent message after Pratibha's
    reply — either live-simulated via agent.py's is_terminal_answer() gate,
    or the frozen candidate_output in static mode. Runs whenever the case has
    a user_reply, regardless of whether the dataset row bothered to declare
    an expected['agent_next'] — that field is no longer read at all."""
    outcome = CaseOutcome(case_id=case.get("id", "?"), bucket=case.get("bucket", "?"))
    inp = case.get("input_state") or {}
    exp = case.get("expected") or {}

    outcome.results.append(check_a1_repeat(agent_output, prior_agent_msgs or []))

    if case.get("user_reply") and agent_next is not None:
        outcome.results.append(check_a2_first_turn_acceptance(
            case["user_reply"], agent_next))

    if extracted is not None and exp.get("extracted_fields"):
        outcome.results.append(check_a3_extraction(
            case.get("user_reply", ""), extracted, exp["extracted_fields"]))

    current = inp.get("current_question") or {}
    outcome.results.append(check_a5_resurface_opener(
        agent_output,
        inp.get("touch_count") or current.get("touch_count") or 0,
        current.get("trigger") or "",
    ))

    outcome.results.append(check_a7_high_pov(
        agent_output,
        current.get("original_requirement") or "",
    ))

    return outcome
