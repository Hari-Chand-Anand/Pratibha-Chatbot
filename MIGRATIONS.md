# Pratibha Chatbot — Migrations

Append-only history of every schema change. Newest at the bottom. Anything written here is also reflected in `pratibha-agent/csv_parser.py::ensure_tables` (which is idempotent — runs on every boot).

Rule: all migrations are **additive**. No `DROP`, no destructive `ALTER`. Backfills use `IS NULL` guards so they only touch rows missed by earlier runs. Rollback = ignore the new tables/columns; old code continues to work.

---

## #001 — Initial schema (20 Jun 2026)

Tables created:
- `pratibha_leads` — per-day intake log, `UNIQUE(mobile_number, export_date)`
- `pratibha_responses` — Pratibha's answers (the sacrosanct log per C-2)
- `pratibha_digest` — daily management digest, `UNIQUE(digest_date)`
- `pratibha_conversations` — full Q&A log for the 6 PM summary

---

## #002 — Customer lifecycle + multi-inquiry (24 Jun 2026)

Added by the Memory Fix. See `MEMORY_FIX_PLAN.md` for the rationale and `CHANGES.md` for the deploy log.

New tables:
- `pratibha_customers` — the **lifecycle entity** (one row per mobile, owns `touch_count`, `next_touch_date`, `lifecycle_status`). This is the BR-2 "a lead is a customer, not a row" enforcement point.
- `pratibha_touches` — audit trail of each surfacing (touch 1..4).
- `pratibha_customer_inquiries` — one row per IndiaMart inquiry. `UNIQUE(mobile_number, inquiry_text, inquired_on)` so re-ingesting the same CSV is a no-op. This is the source of truth for the multi-inquiry case ("same mobile, different requirements").

Altered:
- `pratibha_responses ADD COLUMN mobile_number TEXT` (nullable, additive). Backfilled from `pratibha_leads.mobile_number` via `lead_id` join, idempotent — `WHERE pr.mobile_number IS NULL`.

One-shot backfill on first boot after Migration #002:
- Every mobile in `pratibha_leads` becomes a `pratibha_customers` row with `next_touch_date = NULL`. This means historical customers DO NOT surface on first run. Only customers from new CSV uploads (which set `next_touch_date = export_date`) appear in the queue.

Feature flag: `MEMORY_FIX_ENABLED` (default `true`). Setting to `false` in `backend/.env` falls back to the pre-Memory-Fix queue builder. New tables remain populated but unused — fully reversible.

---

## #003 — Data quality + owner report (24 Jun 2026)

Migration #003 turns vague answers into structured data and reshapes the 6 PM summary into a numeric owner report.

Altered:
- `pratibha_responses` adds 14 nullable columns (`price_quoted_inr`, `customer_response_status`, `visit_date`, `next_action`, `next_action_date`, `why_not_required`, `future_potential`, `actual_customer_response`, `junk_reason`, `forwarded_to_name`, `handoff_status`, `callback_outcome`, `trigger_type`, `completeness_score`). All `ADD COLUMN IF NOT EXISTS` — idempotent.

Created:
- `pratibha_daily_board` view — aggregates per-day counts + ₹ totals + avg completeness. The owner report reads this directly. Brain ingests it.

Feature flag: `DATA_QUALITY_ENABLED` (default `true`). Setting to `false` falls back to Memory-Fix-only behaviour — the new columns stay populated as NULL.

Code wiring:
- `required_fields.py` declares trigger→fields contract + templated follow-up questions.
- `tools_quality.py` owns the expanded LLM extraction prompt, type-safe parsing of price/date/int, persistence helper, and the missing-field evaluator.
- `tools.py::save_response` now accepts `trigger`, runs the data-quality persistence, and returns `quality_followup` in the result.
- `agent.py::answer_received_node` reads `result["quality_followup"]` first; falls back to legacy `evaluate_answer` only if the data-quality layer is disabled or has nothing to ask. `MAX_FOLLOWUPS` raised from 2 → 3.
- `summary_writer.py` rewritten to emit the numbers-first owner report from `pratibha_daily_board` + auxiliary queries.

---

## #004 — Evaluation harness + hard-junk + summary status (01 Jul 2026)

Purpose: enable proper LLM evaluation (dataset + eval harness + production feedback loop), migrate summary generation from Groq to Claude, and unlock the two-window (6 PM + 10 AM backup) scheduler.

New table:
- `pratibha_agent_traces` — one row per LLM turn. `input_state` JSONB, `llm_output`, `user_reply`, `auto_flags TEXT[]` populated by deterministic checks in real-time. GIN index on `auto_flags` so the daily monitor can scan `WHERE 'repeat_question' = ANY(auto_flags)` cheaply. Also indexed by `session_date` and `mobile_number`.

Altered tables (all `ADD COLUMN IF NOT EXISTS`, all nullable except explicit DEFAULT):

- `pratibha_digest`
  - `status TEXT DEFAULT 'pending'` — `pending | partial | complete | failed`
  - `last_attempt_at TIMESTAMPTZ`
  - `attempt_count INTEGER DEFAULT 0` — capped at 3
  - `failure_reason TEXT`
  - `generated_by TEXT` — `claude | template | groq` (audit trail)

- `pratibha_customers`
  - `resurface_blocked BOOLEAN DEFAULT FALSE` — hard-junk gate: queue builder skips these forever
  - `hard_junk_reason TEXT` — deterministic tag: `language_barrier`, `wrong_product`, `explicit_non_buyer`, `invalid_contact`, `duplicate`, `touch_4_consented`
  - `hard_junked_at TIMESTAMPTZ`

- `pratibha_customer_inquiries`
  - `first_seen_time TIMESTAMPTZ` — Cratio original Lead-Date/time. Used in resurface openers.

Feature flags (all default enabled; falling back to prior behaviour is safe):
- `TRACES_ENABLED=true` — trace writer on every turn
- `CLAUDE_QUEUE_ENABLED=true` — Claude builds question queue (falls back to `csv_parser_queue.build_question_queue` on error)
- `MONITOR_ENABLED=true` — daily monitor report writer runs at 6 PM + 10 AM
- `SUMMARY_LLM=claude` — narrative generation. `groq` = legacy path.

Rollback: setting all feature flags to `false` reverts behaviour completely. New tables/columns stay populated but unused. No `DROP` anywhere.

Code touched by this migration:
- `csv_parser.py::ensure_tables` — additive DDL block for 4a–4d
- `scheduler.py` — split into 6 PM + 10 AM jobs with idempotency guard via `pratibha_digest.status`
- `summary_writer.py` — Claude call replaces Groq for narrative (deterministic counts unchanged)
- `agent.py::answer_received_node` — writes to `pratibha_agent_traces` when `TRACES_ENABLED`
- `csv_parser_queue.py` — resurface opener includes Cratio `first_seen_time`
- `hard_junk.py` (new) — deterministic hard-junk classifier + touch-4 consent flow
- `queue_builder_claude.py` (new) — Claude-driven queue construction with fallback
- `monitor_writer.py` (new) — daily aggregation over `pratibha_agent_traces`
- `eval/` (new folder) — dataset + deterministic checks + LLM-judge + `run_eval.py`

---

## #004 phase 2 — Agent behaviour fixes (02 Jul 2026)

No schema changes. All fixes are in Python only. Infrastructure from phase 1 measures these; this phase makes them pass.

**Fix 1 — Kill rigid 4-question follow-up loop (`agent.py`)**
- Removed local `MAX_FOLLOWUPS = 3`. Now imports `MAX_FOLLOWUPS = 2` from `required_fields.py`.
- Hard cap drops from 3 to 2 follow-ups per lead, matching the `required_fields.py` contract.
- `asked_fields` loop-guard (already wired in phase 1) prevents re-asking a field if extraction failed — this is what stops the 26-Jun "just told you" × 7 loop.
- Added `mobile_number` to `slim_queue` so touch-4 consent handler can look up the customer.

**Fix 2 — Extraction of casual replies (`tools_quality.py`)**
- Added 7 few-shot examples to `EXTRACTION_PROMPT` covering: `"dy 6800-ds overlock"` → machine_sent, `"36000 + gst"` → price_quoted_inr, `"0"` → price 0 (not missing), `"just told you above"` → machine_sent null (parroting guard), bare `"2"` → call_attempts, `"did not responded"` → no_answer status, combined reply with model + price + status.
- `extract_from_context` (already in place from phase 1) handles deterministic pre-extraction before the LLM runs. Few-shot examples improve the LLM fallback for cases the regex can't cover.

**Fix 3 — Wire `must_force_resurface()` into queue construction (`csv_parser_queue.py`)**
- `build_question_queue` now runs a second pass after the active-customer loop.
- Fetches `lifecycle_status IN ('auto_junked', 'declined')` customers where `resurface_blocked = FALSE` and `last_resolution_at >= today - 7 days`.
- For each, checks `must_force_resurface(original_requirement)`. If True, injects a `⚠ DIRECTOR FLAG` question at the **front** of the queue.
- Trigger: `high_value_junk_flag`. Covered by `REQUIRED_FIELDS` and `FOLLOWUP_QUESTIONS` in `required_fields.py`.
- Eval metric A7 (`high_pov_flag_missed`) should now pass.

**Fix 4 — Touch-4 consent flow end-to-end**
- `csv_parser_queue._build_question_for_customer`: when `touch_count >= 3`, uses `hard_junk.touch_4_prompt(customer)` as the question and sets `trigger = "touch_4_consent"`. Checked before returning_customer and all other trigger paths.
- `agent.py::answer_received_node`: detects `trigger == "touch_4_consent"` and routes to new `_handle_touch_4_answer()` helper instead of `save_response`.
- `_handle_touch_4_answer`: calls `hard_junk.handle_touch_4_reply(conn, mobile, answer)`, logs the exchange to `pratibha_conversations`, returns `consented` / `plan_provided` / `ambiguous`. On ambiguous, sets `followup_pending` to re-ask without advancing the queue.

---

## Fresh-start queue filter (02 Jul 2026)

No schema changes. Pipeline had sat idle 27 Jun – 1 Jul, so on restart the overdue backlog (all customers whose `next_touch_date` fell anywhere in that gap) dumped into a single queue alongside the day's genuinely new leads — 116 items at once, unworkable in one sitting. This isn't a bug; it's `pratibha_customers`-based resurfacing (Migration #002) doing exactly what it's designed to do after an unusually long gap.

Decision: leave every existing Postgres row untouched (no `UPDATE`, no `DELETE`) and instead add a query-time cutoff so the queue builder simply never looks at anything from before the cutoff date, the same "Day Zero" pattern used on the sales-CRM side of the business.

- `csv_parser_queue.py` — new `DAY_ZERO_DATE` env var (default unset = no filter). When set, both the active-customer query and the force-resurface (`⚠ DIRECTOR FLAG`) query add `AND first_seen_date >= DAY_ZERO_DATE`. Customers first seen before the cutoff are excluded from every pass, including the high-value-junk safety net — no exceptions carved out.
- `.env` / `docker-compose.yml` — `DAY_ZERO_DATE=2026-07-02`. Set to empty string to disable and restore full-backlog surfacing.
- All resurfacing/priority/touch-count logic is unchanged for anyone first seen on or after the cutoff — this only narrows *which* customers ever enter the queue, not how they're prioritised once they do.

---

## Auto chat transcript (02 Jul 2026)

`chat_YYYY-MM-DD.md` previously only existed on days someone (Claude, manually) reconstructed it by hand from Postgres — no code path wrote it. The `pratibha-daily-narrative` Cowork task assumed the container wrote this file automatically; it didn't.

- `summary_writer.py::generate_daily_summary` now also writes `chat_{date}.md` — same `conversation_rows` query already used for the summary's CONVERSATION LOG section, formatted via the existing `_format_conversation_log()`, written at the same moment as `summary_{date}.md`/`.html`. Fires on every path that calls `generate_daily_summary`: the 6 PM primary window, the 10 AM backup window, and a manual `POST /save-summary`.
- No new schema, no new dependency — reuses data already being fetched.

---

## Eval harness: continuous learning from production (02 Jul 2026)

The "someone reviews traces every Monday and hand-labels failures" workflow in `eval/README.md` never actually ran once since it launched (01 Jul). Three real defects found and fixed along the way:

**1. A2 was structurally incapable of failing.** `deterministic.py::evaluate_case` graded `expected["agent_next"]` — a field hardcoded to `""` in every single dataset row — instead of anything the code actually produced. It reported 100% the whole time regardless of what the agent did. Fixed: `run_eval.py` now has `simulate_agent_next()`, which replays `agent.py`'s own `is_terminal_answer()` gate (imported directly, not duplicated) to decide what the agent would really say next. `evaluate_case` grades that. Sample size for A2 went from 2 cases (the only two that happened to declare `expected.agent_next`) to 23 (every case with a `user_reply`) — still 100% pass, but now a real 100%.

**2. `deterministic.py` kept its own terminal-phrase list, separately from `agent.py`'s `TERMINAL_ANSWER_PATTERNS`.** The two had drifted (deterministic.py's list was an 11-phrase subset of agent.py's 30+). Fixed by importing `agent.is_terminal_answer` directly — eval and production now share one definition, permanently.

**3. A1 (repeat-question) always passed in LIVE mode** because `run_eval.py` hardcoded `prior_agent_messages = []` for every live case — nothing to compare against, so the check was a no-op. Fixed: `traces.py` already captures `prior_agent_messages_on_lead` (last 6 agent turns) on every real production trace; `get_agent_output()` now reads it from `input_state` instead of discarding it.

**New: `eval/promote_from_traces.py`.** Scans `pratibha_agent_traces` for a given date, converts every flagged row into a properly-shaped case, appends to `eval/dataset/regressions.jsonl` (deduped by trace id). Wired into `scheduler.py::_attempt_summary`, right after the summary/monitor/chat-log are written — so every day's real flagged behaviour automatically grows the permanent regression suite, then gets immediately re-checked against current code. Feature-flagged via `EVAL_AUTO_PROMOTE_ENABLED` (default `true`).

Honest limitation: the `expected.extracted_fields` this script derives for A3 cases is a best-effort heuristic (the same lightweight regex `traces.py` used to raise the flag), not a human-confirmed ideal answer. Treat auto-promoted cases as "this input is worth permanently testing," not gospel — nothing stops hand-editing a row in `regressions.jsonl` later.

Verified: `python eval/run_eval.py --layer 1 --verbose` — all 7 Blocker metrics still 100% after the harness changes (A1 40/40, A2 23/23, A3 2/2, A5 40/40, A7 40/40, B1 9/9, B2 9/9).
