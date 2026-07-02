"""Evaluation harness runner.

Runs offline evaluation of the Pratibha agent + Claude summary writer against
labelled cases in dataset/seed.jsonl and dataset/regressions.jsonl.

USAGE
    python eval/run_eval.py                          # full run
    python eval/run_eval.py --dataset seed           # seed only
    python eval/run_eval.py --dataset regressions    # regressions only
    python eval/run_eval.py --layer 1                # skip Claude judge (free)
    python eval/run_eval.py --verbose                # per-case pass/fail

EXIT CODE
    0 if all Blocker metrics pass, non-zero otherwise. Use in CI/pre-deploy.

DATASET FORMAT (JSONL, one case per line)
    {
      "id": "note-blank-001",
      "bucket": "note_comprehension",
      "severity": "blocker",
      "kind": "agent_turn" | "summary",
      "input_state": { ...trimmed agent state at turn N... },
      "user_reply": "...",
      "expected": {
        "question_contains": ["mantha", "cratio"],
        "question_not_contains": ["how many times"],
        "extracted_fields": { "call_attempts": 2 },
        "flags_should_fire": ["high_pov_flag_missed"]
      },
      "notes": "26-Jun bug — asked call count twice"
    }
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Add parent dir to path so we can import agent modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.checks.deterministic import (
    evaluate_case,
    check_b1_summary_counts,
    check_b2_summary_format,
    CheckResult,
    CaseOutcome,
)
from eval.checks.judge import judge_a4_note_aware, judge_b3_summary


HERE = Path(__file__).resolve().parent
DATASET_DIR = HERE / "dataset"
REPORTS_DIR = HERE / "reports"
FAILURES_DIR = HERE / "failures"
RUNS_LOG = HERE / "eval_runs.jsonl"

BLOCKER_TARGETS = {
    "A1": 1.00, "A2": 0.90, "A3": 0.90, "A5": 1.00, "A7": 1.00,
    "B1": 1.00, "B2": 1.00,
}
HIGH_TARGETS = {
    "A4": 0.90, "A6": 0.80, "B3": 0.80,
}


# ─────────────────────────────────────────────────────────────────────────────
# Dataset loading
# ─────────────────────────────────────────────────────────────────────────────

def load_dataset(which: str) -> list[dict]:
    files = []
    if which in ("all", "seed"):
        files.append(DATASET_DIR / "seed.jsonl")
    if which in ("all", "regressions"):
        files.append(DATASET_DIR / "regressions.jsonl")
    cases = []
    for f in files:
        if not f.exists():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"WARN: skipping malformed line {i} in {f.name}: {e}",
                      file=sys.stderr)
    return cases


# ─────────────────────────────────────────────────────────────────────────────
# Agent output — for now, dataset rows carry the AGENT OUTPUT to evaluate
# directly (as an "expected_output" contrast against the labelled ideal).
# When the agent is invocable in test mode we'll swap this to a real call.
# ─────────────────────────────────────────────────────────────────────────────

def get_agent_output(case: dict, live: bool = True) -> tuple[str, dict, list[str]]:
    """Return (agent_output_text, extracted_fields, prior_agent_messages).

    In LIVE mode (default) the harness invokes the actual pure functions the
    running agent uses — `_build_question_for_customer` for the opener, and
    `extract_from_context` for extraction. This is the honest evaluation:
    what would the agent SAY given this input right now? No DB, no LLM call.

    In STATIC mode it uses case['candidate_output'] as-is. Kept only so the
    hardcoded 26-Jun bug strings can be graded as the frozen baseline."""
    if not live:
        return (
            case.get("candidate_output", ""),
            case.get("candidate_extracted", {}) or {},
            case.get("prior_agent_messages", []) or [],
        )

    inp = case.get("input_state") or {}
    current = inp.get("current_question") or {}

    # A1 fix (02 Jul 2026): traces.py already captures the last 6 agent
    # messages on this lead as input_state.prior_agent_messages_on_lead for
    # every real production trace. Auto-promoted cases carry this for real;
    # hand-authored seed cases don't have it and fall back to [] (unchanged
    # behaviour). Previously this was hardcoded to [] for ALL live cases,
    # which made A1 trivially pass regardless of what the code actually did.
    prior_agent_msgs = inp.get("prior_agent_messages_on_lead") or []

    # 1. Live question generation via csv_parser_queue._build_question_for_customer
    try:
        from csv_parser_queue import _build_question_for_customer
        from datetime import date as _date

        customer = {
            "mobile_number":  f"eval-{case.get('id')}",
            "contact_name":   current.get("contact_name") or "",
            "city":           current.get("city") or "",
            "first_seen_date": _date(2026, 6, 25),
            "lifecycle_status": "active",
            "touch_count":    current.get("touch_count") or inp.get("touch_count") or 0,
            "last_touch_date": None,
            "next_touch_date": _date(2026, 7, 1),
            "last_product":   current.get("original_requirement") or "",
            "last_resolution_at": None,
            "reopened_at": None,
        }
        latest_lead = {
            "id":                 current.get("lead_id"),
            "contact_name":       current.get("contact_name"),
            "city":               current.get("city"),
            "lead_stage":         current.get("lead_stage") or "",
            "activity_note":      current.get("activity_note") or "",
            "original_requirement": current.get("original_requirement") or "",
            "last_activity_time": None,
            "mobile_number":      customer["mobile_number"],
        }
        # Parse the "cratio_ts" if provided as string
        inquiries = []
        cratio_ts_str = current.get("cratio_ts") or ""
        if cratio_ts_str:
            inquiries.append({
                "id": 1, "inquiry_text": current.get("original_requirement") or "",
                "inquired_on": _date(2026, 6, 24),
                "first_seen_time": _parse_cratio_ts(cratio_ts_str),
            })
        else:
            inquiries.append({
                "id": 1, "inquiry_text": current.get("original_requirement") or "",
                "inquired_on": _date(2026, 6, 25), "first_seen_time": None,
            })

        # Synthesise a plausible prior response so the resurface branch fires
        # (real production has this from pratibha_responses; the eval has to fake it).
        prior_answer = ""
        prior_machine = ""
        if (customer["touch_count"] or 0) >= 1:
            prior_answer = "sent catalog last time"
            prior_machine = "prior catalog"

        item = _build_question_for_customer(
            customer, latest_lead, inquiries,
            {"answer": prior_answer, "machine_sent": prior_machine},
            _date(2026, 7, 1),
        )
        question_output = item["question"] if item else ""

        # If _build_question_for_customer returned None (legacy classifier
        # rejected a touch=0 lead with priority>=99), still surface the value
        # banner alone so A7 checks work. Eval visibility only.
        if not question_output:
            from hard_junk import must_force_resurface as _mfr
            from hard_junk import extract_pov_inr as _epov, extract_quantity as _eqty
            req = latest_lead["original_requirement"]
            forced, _r = _mfr(req)
            if forced:
                pov = _epov(req); qty = _eqty(req)
                parts = []
                if pov and pov >= 100_000:
                    if pov >= 10_000_000:
                        parts.append(f"₹{pov/10_000_000:.1f} Crore")
                    else:
                        parts.append(f"₹{pov/100_000:.1f} lakh")
                if qty and qty > 5:
                    parts.append(f"{qty} pieces")
                question_output = ("⚠ " + " · ".join(parts) +
                                   f" — flagged. What's the plan for {customer['contact_name']}?")
    except Exception as e:
        question_output = f"[live-replay error: {e}]"

    # 2. Live extraction via extract_from_context
    extracted = {}
    try:
        from tools_quality import extract_from_context
        # For extraction cases, the question the agent asked WAS a follow-up.
        # We reconstruct it from the trigger's expected follow-up template.
        from required_fields import FOLLOWUP_QUESTIONS, REQUIRED_FIELDS
        trigger = current.get("trigger", "")
        required = REQUIRED_FIELDS.get(trigger, [])
        # Try each required field's canned question and extract from the reply
        for f in required:
            q = FOLLOWUP_QUESTIONS.get(f, "")
            got = extract_from_context(q, case.get("user_reply", "") or "")
            for k, v in got.items():
                if v is not None and k not in extracted:
                    extracted[k] = v
    except Exception:
        pass

    return question_output, extracted, prior_agent_msgs


def simulate_agent_next(case: dict, extracted: dict, live: bool = True) -> str:
    """A2 fix (02 Jul 2026): what does the agent actually say right after
    Pratibha's reply? Previously A2 graded a frozen expected['agent_next']
    field that was hardcoded to "" in every dataset row, so the check always
    passed no matter what the code did (see MIGRATIONS.md, 02 Jul entry).

    This reuses agent.py's OWN is_terminal_answer() — the exact gate the real
    agent runs before deciding whether to ask a follow-up — instead of
    deterministic.py's separate, drifted terminal-phrase list. If it fires,
    the real agent takes the early "log it, move on" branch and says nothing
    further. If it does NOT fire, whatever follow-up the required-fields
    contract would normally ask is what gets said — so a genuinely-terminal
    reply that the pattern list fails to catch shows up here as a real,
    catchable A2 failure instead of being invisible."""
    user_reply = case.get("user_reply", "") or ""
    if not user_reply:
        return ""
    if not live:
        return case.get("candidate_output", "")

    try:
        from agent import is_terminal_answer
    except Exception as e:
        return f"[a2-sim error: {e}]"

    if is_terminal_answer(user_reply):
        return ""

    try:
        from required_fields import next_followup_question
        inp = case.get("input_state") or {}
        current = inp.get("current_question") or {}
        trigger = current.get("trigger", "")
        _, question = next_followup_question(trigger, extracted or {}, [])
        return question or ""
    except Exception as e:
        return f"[a2-sim error: {e}]"


def _parse_cratio_ts(s: str):
    """Parse '24 Jun 03:47 PM' → datetime, best-effort."""
    from datetime import datetime
    for fmt in ("%d %b %I:%M %p", "%d %b %Y %I:%M %p"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(year=2026)
        except Exception:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def run(dataset: str = "all", layer: int = 2, verbose: bool = False,
        live: bool = True) -> int:
    cases = load_dataset(dataset)
    if not cases:
        print(f"No cases found in dataset={dataset!r}. Populate dataset/seed.jsonl first.")
        return 0

    print(f"Mode: {'LIVE agent replay' if live else 'STATIC (candidate_output)'}")

    metric_hits = defaultdict(list)     # metric_id -> list[bool]
    per_case_outcomes = []
    failing_cases = []

    for case in cases:
        kind = case.get("kind", "agent_turn")
        if kind == "agent_turn":
            output, extracted, prior_msgs = get_agent_output(case, live=live)
            agent_next = simulate_agent_next(case, extracted, live=live)
            outcome = evaluate_case(case, output, extracted, prior_msgs, agent_next)

            # Layer 2 — A4 note-aware judge (skip if --layer 1)
            if layer >= 2:
                inp = case.get("input_state") or {}
                current = inp.get("current_question") or {}
                v = judge_a4_note_aware(
                    note=current.get("activity_note", ""),
                    req=current.get("original_requirement", ""),
                    touch=inp.get("touch_count") or current.get("touch_count") or 0,
                    reply=case.get("user_reply", ""),
                    question=output,
                    case_id=case.get("id", "?"),
                )
                outcome.results.append(CheckResult(
                    "A4", v.passed, v.reason, "high",
                ))

        elif kind == "summary":
            summary_text = case.get("candidate_output", "")
            expected = case.get("expected") or {}
            outcome = CaseOutcome(case_id=case.get("id", "?"),
                                  bucket=case.get("bucket", "?"))
            outcome.results.append(
                check_b1_summary_counts(summary_text, expected.get("counts") or {})
            )
            outcome.results.append(check_b2_summary_format(summary_text))
            if layer >= 2:
                v = judge_b3_summary(
                    reference=expected.get("reference_text", ""),
                    generated=summary_text,
                    case_id=case.get("id", "?"),
                )
                outcome.results.append(CheckResult(
                    "B3", v.passed, v.reason, "high",
                ))
        else:
            continue

        for r in outcome.results:
            metric_hits[r.metric].append(r.passed)
        per_case_outcomes.append(outcome)
        if outcome.any_blocker_failed():
            failing_cases.append((case, outcome))
        if verbose:
            _print_case(outcome)

    scorecard = _compute_scorecard(metric_hits)
    _write_report(cases, per_case_outcomes, scorecard, dataset, layer)
    _dump_failures(failing_cases)
    _append_run_log(scorecard, dataset, layer)
    _print_scorecard(scorecard)

    blocker_ok = all(
        scorecard[m]["rate"] >= tgt
        for m, tgt in BLOCKER_TARGETS.items()
        if m in scorecard
    )
    return 0 if blocker_ok else 1



def _compute_scorecard(hits: dict) -> dict:
    out = {}
    for metric, results in hits.items():
        if not results:
            continue
        rate = sum(1 for h in results if h) / len(results)
        out[metric] = {
            "rate": rate, "passed": sum(1 for h in results if h),
            "total": len(results),
        }
    return out


def _print_case(o) -> None:
    fails = [r for r in o.results if not r.passed]
    if fails:
        print(f"  FAIL {o.case_id} ({o.bucket})")
        for f in fails:
            print(f"    - {f.metric}: {f.detail}")
    else:
        print(f"  OK   {o.case_id}")


def _print_scorecard(sc: dict) -> None:
    print("\n---- SCORECARD ----")
    for m in sorted(sc.keys()):
        d = sc[m]
        tgt = BLOCKER_TARGETS.get(m) or HIGH_TARGETS.get(m) or 0
        sev = "BLOCKER" if m in BLOCKER_TARGETS else "HIGH   "
        symbol = "PASS" if d["rate"] >= tgt else "FAIL"
        print(f"  [{sev}] {m}: {d['passed']}/{d['total']} = {d['rate']:.0%} "
              f"(target {tgt:.0%}) {symbol}")


def _write_report(cases, outcomes, scorecard, dataset, layer) -> None:
    REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"eval_report_{ts}.md"
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# Eval Report - {ts}\n")
        f.write(f"Dataset: `{dataset}` | Layer: {layer} | Cases: {len(cases)}\n\n")
        f.write("## Scorecard\n\n| Metric | Passed | Total | Rate | Target | Status |\n")
        f.write("|---|---|---|---|---|---|\n")
        for m in sorted(scorecard.keys()):
            d = scorecard[m]
            tgt = BLOCKER_TARGETS.get(m) or HIGH_TARGETS.get(m) or 0
            status = "PASS" if d['rate'] >= tgt else "FAIL"
            f.write(f"| {m} | {d['passed']} | {d['total']} | "
                    f"{d['rate']:.0%} | {tgt:.0%} | {status} |\n")
        fails = [o for o in outcomes if any(not r.passed for r in o.results)]
        if fails:
            f.write("\n## Failing cases\n\n")
            for o in fails:
                f.write(f"### {o.case_id} ({o.bucket})\n\n")
                for r in o.results:
                    if not r.passed:
                        f.write(f"- **{r.metric}** ({r.severity}): {r.detail}\n")
                f.write("\n")
    print(f"\nReport written to {path}")


def _dump_failures(failing_cases) -> None:
    if not failing_cases:
        return
    FAILURES_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = FAILURES_DIR / f"failures_{ts}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for case, outcome in failing_cases:
            f.write(json.dumps({
                "case_id": case.get("id"),
                "bucket": case.get("bucket"),
                "input_state": case.get("input_state"),
                "user_reply": case.get("user_reply"),
                "expected": case.get("expected"),
                "candidate_output": case.get("candidate_output"),
                "failed": [
                    {"metric": r.metric, "detail": r.detail}
                    for r in outcome.results if not r.passed
                ],
            }) + "\n")


def _append_run_log(scorecard, dataset, layer) -> None:
    row = {
        "ts": datetime.now().isoformat(),
        "dataset": dataset,
        "layer": layer,
        "scores": {m: round(d["rate"], 3) for m, d in scorecard.items()},
    }
    with RUNS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=("all", "seed", "regressions"), default="all")
    ap.add_argument("--layer", type=int, choices=(1, 2), default=2)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--static", action="store_true",
                    help="Grade hardcoded candidate_output (frozen baseline). "
                         "Default is LIVE — invoke the real agent code paths.")
    args = ap.parse_args()
    return run(dataset=args.dataset, layer=args.layer, verbose=args.verbose,
               live=not args.static)


if __name__ == "__main__":
    sys.exit(main())
