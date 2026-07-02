"""Promote flagged production traces into permanent regression test cases
(02 Jul 2026).

Replaces the manual "someone reviews traces every Monday and hand-labels
failures" step described in eval/README.md's production feedback loop. That
step never actually ran once since it launched (01 Jul) — this automates it.

Every row in pratibha_agent_traces with a non-empty auto_flags array for the
target date becomes one row appended to eval/dataset/regressions.jsonl,
tagged with which flag(s) fired. Deduplicated by trace id (case id
"auto-<trace_id>"), so re-running for the same date is a no-op after the
first pass.

WHAT THIS CAN AND CAN'T DO (be honest about it):
  - A3 (extraction), A5 (resurface date), A7 (high-value flag) — these grade
    the agent's output against rule-based checks. Promoting a real flagged
    trace and replaying it against current code is a genuine regression test.
  - A1 (repeat-question) — now meaningful too, since traces.py already
    captures prior_agent_messages_on_lead and run_eval.py's LIVE mode reads
    it (02 Jul fix).
  - A2 (terminal-answer handling) — meaningful since run_eval.py's
    simulate_agent_next() replays agent.py's real is_terminal_answer() gate
    (02 Jul fix), not a frozen dataset value.
  - The "expected" fields this script derives for A3 are a best-effort
    heuristic (same lightweight regex traces.py used to raise the flag in
    the first place) — not a substitute for a human confirming the ideal
    answer. Treat auto-promoted cases as "this input is worth permanently
    testing," not "this exact expected value is gospel." Nothing stops you
    from hand-editing a row in regressions.jsonl later if the heuristic
    guessed wrong.

USAGE
    python eval/promote_from_traces.py                  # today (IST-agnostic; pass --date explicitly if it matters)
    python eval/promote_from_traces.py --date 2026-07-02
"""
import argparse
import json
import re
from datetime import date as date_cls
from pathlib import Path

from csv_parser import get_db_conn

HERE = Path(__file__).resolve().parent
REGRESSIONS_PATH = HERE / "dataset" / "regressions.jsonl"

# Same model-code shape traces.py's _flag_extraction_missed uses to detect a
# missed model mention — kept in sync manually since traces.py's version is
# inline, not exported. If you change one, change both.
_MODEL_CODE_RE = re.compile(
    r"\b(?:dy|dlr|dfb|ls|lu|es|zoje|kansai|b\d+|f5|f7|cm|lew|hw|hikari|juki|jack)"
    r"[-\s]?\d{2,5}[a-z]{0,3}(?:-[a-z0-9]+)?\b",
    re.IGNORECASE,
)

_FLAG_SEVERITY = {
    "repeat_question":        "blocker",
    "extraction_missed":      "blocker",
    "resurface_missing_date": "blocker",
    "high_pov_flag_missed":   "blocker",
    "terminal_ignored":       "blocker",
}


def _already_promoted_ids() -> set:
    if not REGRESSIONS_PATH.exists():
        return set()
    ids = set()
    for line in REGRESSIONS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ids.add(json.loads(line).get("id"))
        except json.JSONDecodeError:
            continue
    return ids


def _derive_expected(flags: list, user_reply: str) -> dict:
    """Best-effort 'expected' derivation. Only A3 needs one — A1/A2/A5/A7
    grade directly against rules + live/candidate output, no frozen value
    required (see deterministic.py)."""
    expected = {}
    if "extraction_missed" not in flags:
        return expected
    reply_l = (user_reply or "").lower()

    m = _MODEL_CODE_RE.search(user_reply or "")
    if m:
        expected["machine_sent"] = m.group(0).strip()

    price_m = re.search(r"\b\d{4,7}\b", reply_l)
    if price_m and any(w in reply_l for w in ("price", "rs", "rupee", "₹", "gst", "quote")):
        try:
            expected["price_quoted_inr"] = float(price_m.group(0))
        except ValueError:
            pass

    attempts_m = re.search(r"\b([1-5]|once|twice|thrice)\b", reply_l)
    if attempts_m and any(w in reply_l for w in ("call", "attempt", "tried", "try", "rung")):
        word = attempts_m.group(0)
        word_map = {"once": 1, "twice": 2, "thrice": 3}
        expected["call_attempts"] = word_map.get(word, None)
        if expected["call_attempts"] is None:
            try:
                expected["call_attempts"] = int(word)
            except ValueError:
                del expected["call_attempts"]

    return expected


def promote(target_date: str, verbose: bool = True) -> int:
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, input_state, llm_output, user_reply, auto_flags, trigger_type
        FROM pratibha_agent_traces
        WHERE session_date = %s AND array_length(auto_flags, 1) > 0
        ORDER BY id
    """, (target_date,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    existing = _already_promoted_ids()
    new_cases = []
    skipped = 0

    for trace_id, input_state, llm_output, user_reply, flags, trigger_type in rows:
        case_id = f"auto-{trace_id}"
        if case_id in existing:
            skipped += 1
            continue

        inp = input_state if isinstance(input_state, dict) else json.loads(input_state or "{}")
        flags = list(flags or [])
        severity = "blocker" if any(_FLAG_SEVERITY.get(f) == "blocker" for f in flags) else "high"

        case = {
            "id": case_id,
            "bucket": "production_regression",
            "severity": severity,
            "kind": "agent_turn",
            "input_state": inp,
            "user_reply": user_reply,
            "candidate_output": llm_output,
            "expected": {"extracted_fields": _derive_expected(flags, user_reply)}
                        if "extraction_missed" in flags else {},
            "notes": f"auto-promoted {target_date} from trace #{trace_id} — "
                     f"flags={flags} trigger={trigger_type}",
        }
        new_cases.append(case)

    if new_cases:
        REGRESSIONS_PATH.parent.mkdir(exist_ok=True)
        with REGRESSIONS_PATH.open("a", encoding="utf-8") as f:
            for c in new_cases:
                f.write(json.dumps(c) + "\n")

    if verbose:
        print(f"[promote_from_traces] {target_date}: {len(rows)} flagged trace(s) found, "
              f"{len(new_cases)} newly promoted, {skipped} already known.")
    return len(new_cases)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date_cls.today().isoformat(),
                     help="Session date to scan (YYYY-MM-DD). Defaults to today.")
    args = ap.parse_args()
    promote(args.date)


if __name__ == "__main__":
    main()
