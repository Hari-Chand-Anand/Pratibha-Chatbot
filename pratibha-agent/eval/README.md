# Pratibha Chatbot — Evaluation Harness

Automated, reproducible evaluation of the Pratibha agent and Claude summary writer. Runs locally before any change ships. Same deterministic checks run on production traces in real-time (see `monitor_writer.py`), so production failures feed back into this dataset.

## Layout

```
eval/
├── dataset/
│   ├── seed.jsonl              ← 50 labelled cases (see build order below)
│   └── regressions.jsonl       ← promoted production failures (starts empty, grows)
├── checks/
│   ├── deterministic.py        ← Layer 1: A1-A3, A5-A7, B1-B2 (regex/rule)
│   └── judge.py                ← Layer 2: A4, B3 (Claude LLM-judge)
├── failures/                   ← per-run drop-box of failing cases
├── reports/                    ← eval_report_YYYYMMDD_HHMMSS.md + scorecard.json
├── run_eval.py                 ← the runner
└── eval_runs.jsonl             ← append-only log (date, prompt version, scores)
```

## Metrics (Blocker = must pass before deploy)

| ID | Metric | Target | Layer | Severity |
|---|---|---|---|---|
| A1 | Repeat-question rate per session | 0 | 1 | Blocker |
| A2 | First-turn answer acceptance | ≥ 90% | 1 | Blocker |
| A3 | Field extraction accuracy | ≥ 90% | 1 | Blocker |
| A4 | Note-aware question rate | ≥ 90% | 2 | High |
| A5 | Resurface opener has CRM date/time | 100% | 1 | Blocker |
| A6 | Session completion (covered/queued) | ≥ 80% | 1 | High |
| A7 | High-POV flag fires | 100% | 1 | Blocker |
| B1 | Summary count accuracy vs Postgres | 100% | 1 | Blocker |
| B2 | Summary format conformance | 100% | 1 | Blocker |
| B3 | Summary narrative quality | ≥ 8/10 | 2 | High |

## Usage

```bash
# Full run (seed + regressions)
python eval/run_eval.py

# Only seed set
python eval/run_eval.py --dataset seed

# Only Layer 1 (no Claude API calls, free)
python eval/run_eval.py --layer 1

# Verbose per-case output
python eval/run_eval.py --verbose
```

Exit code is non-zero if any **Blocker** metric fails — so this can gate CI.

## Dataset build order (Task #9)

Populate `dataset/seed.jsonl` in this order:
1. 15 note-comprehension cases — one per note pattern in `CLAUDE.md`
2. 10 resurface cases — touch_count 1..4, verify opener contains Cratio date/time
3. 10 loop-avoidance cases — Pratibha's reply already contains the answer
4. 5 edge cases — Hindi, typos, "…", mixed languages
5. 10 summary cases — synthesised end-of-day states + reference summaries

Each row schema:
```json
{
  "id": "note-blank-001",
  "bucket": "note_comprehension",
  "severity": "blocker",
  "input_state": { ...trimmed agent state at turn N... },
  "user_reply": "...",              // for turn evaluation
  "expected": {
    "question_contains": ["mantha", "cratio"],
    "question_not_contains": ["how many times"],
    "extracted_fields": { "call_attempts": 2 },
    "flags_should_fire": ["high_pov_flag_missed"]  // negative examples
  },
  "notes": "26-Jun bug — asked call count twice"
}
```

## Production feedback loop — now automatic (02 Jul 2026)

The manual "review traces every Monday" version below never actually ran once
since Migration #004 shipped. Replaced with `eval/promote_from_traces.py`,
wired into `scheduler.py` right after each day's summary/monitor/chat-log are
written:

1. Every turn writes to `pratibha_agent_traces` with `auto_flags` populated.
2. Same day, right after the summary is generated: `promote_from_traces.py`
   scans that date's flagged traces, converts each into a case (deduped by
   trace id), and appends to `dataset/regressions.jsonl`.
3. If anything new was promoted, `run_eval.py --dataset regressions --layer 1`
   re-runs immediately and logs whether today's real failures are still
   failing under current code.
4. Feature flag: `EVAL_AUTO_PROMOTE_ENABLED` (default `true`). Set `false` to
   go back to the fully manual version below.

Run it by hand any time: `python eval/promote_from_traces.py --date YYYY-MM-DD`.

Honest limit: this only works cleanly for A1/A3/A5/A7 (rule-graded against
live output) and A2 (now live-simulated via `agent.py`'s own
`is_terminal_answer()`). A4/B3 stay judgment calls — auto-promoting doesn't
make them self-labelling; they still need `ANTHROPIC_API_KEY` for Layer 2 or
a human to grade them.

**Original manual version (still works if the flag above is off):**
1. Weekly (Monday), scan `WHERE array_length(auto_flags, 1) > 0 AND session_date >= now() - '7 days'`.
2. For each notable failure: read the trace, label the ideal output, add a row to `dataset/regressions.jsonl`.
3. Next `run_eval.py` catches the regression class permanently.
