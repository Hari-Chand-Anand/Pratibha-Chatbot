# Pratibha Chatbot — Changes

Append-only deploy log. Newest at the bottom. One line per behaviour change. If something breaks two weeks from now, look here first to see what shipped recently.

---

## 24 Jun 2026 — Memory Fix shipped (local)

**What changed (behaviour):**
- Sessions now resume. Closing the browser mid-day and reopening no longer wipes progress. Greetings ("hi", "hello", "start") show `"Continuing — N done, M to go"` instead of restarting at lead #1.
- Follow-up cadence enforced. A lead surfaces Day 1, then Day 3, Day 5, Day 7. Max 4 touches.
- Auto-junk after 4 unsuccessful touches with no model addressed. Auto-junked customers appear in the daily digest.
- Drop-out detection. Answers containing "ordered" / "placed order" / "payment received" → customer marked `ordered`. Answers containing "not interested" / "declined" / "mana kar diya" → `declined`. Both remove the customer from the queue permanently (until FR-7).
- Returning-customer recognition (FR-7). A previously closed customer (ordered / declined / auto_junked) who comes back with a new IndiaMART inquiry is reopened. The agent surfaces a "Returning customer alert" question naming prior status + prior product + today's new inquiry.
- Multi-inquiry handling. Same mobile with multiple inquiries (same day or different days) now stored individually. The agent question lists every open inquiry and asks which one Pratibha is updating.

**Files touched (all under `C:\Users\ADMIN\Desktop\Pratibha Chatbot\`):**
- `pratibha-agent/csv_parser.py` — rewritten. Schema migration #002. CSV parser now also writes `pratibha_customers` (Loop A) and `pratibha_customer_inquiries` (Loop B). FR-7 reopen pass + `last_product` recompute appended.
- `pratibha-agent/csv_parser_queue.py` — **NEW**. Question-queue builder. Reads from `pratibha_customers` (lifecycle entity). Per-customer priority: FR-7 returning > multi-inquiry > follow-up touch > first-touch trigger.
- `pratibha-agent/csv_parser_legacy.py` — **NEW**. First-touch trigger classifier (lifted from old code, behaviour-preserved) + legacy queue builder for the rollback path. Both files split out of `csv_parser.py` to stay under the file-size limit; functionality identical to a single big file.
- `pratibha-agent/tools.py` — `save_response` extended: increments `touch_count`, logs `pratibha_touches`, matches inquiries to extracted model, flips `lifecycle_status` on drop-out, auto-junks at touch 4. `pratibha_responses` write remains sacrosanct (C-2): wrapped in a try around the rest so the answer log persists even if the memory-fix layer errors. `generate_digest` extended to count auto-junked customers.
- `pratibha-agent/agent.py` — `classify_input` routes greetings to a new `resume_node` if a session is in flight (FR-2). `resume_node` returns "Continuing — N done, M to go". Build_graph wires the new node + edge.
- `pratibha-agent/main.py` — `POST /chat` no longer force-resets state on "start"/"begin". Checks the LangGraph checkpointer for an active session first; if one exists, just passes the new message and lets `classify_input` route to resume.
- `MEMORY_FIX_PLAN.md` — plan + audit trail (§10).
- `MIGRATIONS.md` — **NEW** (this folder).
- `CHANGES.md` — **NEW** (this file).

**Feature flag:** `MEMORY_FIX_ENABLED` in `backend/.env`. Default `true`. Set to `false` and `docker compose up -d --build python-agent` to revert to pre-Memory-Fix behaviour. New tables remain populated but unused.

**Deploy ritual:**
1. `docker compose up -d --build python-agent`
2. Watch logs for `LangGraph agent ready`.
3. Upload today's 3 CSVs in `pratibha.html` and walk through 2 leads.
4. Close browser, reopen, re-upload same CSVs. The chat should say "Continuing — 2 done, X to go".
5. If anything misbehaves: set `MEMORY_FIX_ENABLED=false` in `backend/.env` → `docker compose up -d --build python-agent`. Behaviour reverts in ~60 seconds. File a one-line note here describing what went wrong.

**Known follow-ups (not blocking Friday deploy):**
- The `pri or_label` in the FR-7 returning-customer template currently says "previously closed" instead of naming the exact prior status (ordered / declined / auto_junked). A small enhancement — store prior status in `pratibha_customers.prior_status` next migration.
- Smoke test (`pratibha-agent/smoke_test.py`) is scoped for the next iteration.

---

## 25 Jun 2026 — Data Quality + Owner Report shipped (local)

**What changed (behaviour):**
- Pratibha can no longer slip past with "yes sent details". Each trigger declares its required fields; the agent asks one deterministic templated question per missing field, up to 3 follow-ups per lead.
- Every answer gets a `completeness_score` (0–10) on `pratibha_responses`. Surfaced in the 6 PM report and queryable for trend.
- 6 PM digest is now a numbers-first owner report: Pipeline / Money / Active Pipeline / Red Flags / Conversions / Week-so-far / Pratibha-today. Reads `pratibha_daily_board` view.

**Files touched (all under `C:\Users\ADMIN\Desktop\Pratibha Chatbot\`):**
- `pratibha-agent/csv_parser.py` — Migration #003 SQL appended to `ensure_tables` (14 new columns + view). Now delegates Loop A/B/FR-7 to `csv_parser_ingest.py` (size-cap workaround).
- `pratibha-agent/csv_parser_ingest.py` — **NEW**. Loop A (customers upsert), Loop B (inquiries insert), FR-7 reopen pass, last_product recompute.
- `pratibha-agent/required_fields.py` — **NEW**. `REQUIRED_FIELDS` dict + `FOLLOWUP_QUESTIONS` templates + helpers (`missing_fields`, `compute_completeness_score`, `next_followup_question`). `MAX_FOLLOWUPS = 3`.
- `pratibha-agent/tools_quality.py` — **NEW**. Expanded LLM extraction prompt (14 fields), type-safe parsers (price/date/int), persistence helper, evaluator.
- `pratibha-agent/tools.py` — slimmer rewrite. `save_response` now takes `trigger`, calls the quality layer, returns `quality_followup` + `completeness_score` + `missing_fields`. `generate_digest` reads the daily board.
- `pratibha-agent/agent.py` — `MAX_FOLLOWUPS=3`. `answer_received_node` reads `result["quality_followup"]` before falling back to legacy `evaluate_answer`. Passes `trigger` to `save_response`.
- `pratibha-agent/summary_writer.py` — rewritten. Numbers-first owner report from `pratibha_daily_board`.
- `MIGRATIONS.md` — appended #003 entry.
- `DATA_QUALITY_PLAN.md` — **NEW**. Plan-before-code spec.

**Feature flag:** `DATA_QUALITY_ENABLED` in `backend/.env`. Default `true`. Set to `false` → reverts to Memory-Fix-only behaviour. New columns stay NULL on new rows but everything keeps working.

**Deploy ritual** (in order):
1. `docker compose up -d --build python-agent`
2. `docker compose exec python-agent python smoke_test.py` — expect `8/8 checks passed` (the existing smoke test still applies; quality layer is non-blocking).
3. Upload today's 3 CSVs in `pratibha.html`. Answer one lead with a vague reply ("yes sent details") and watch the agent ask "Which exact model — DY-1201, ZOJE HS, something else?". Provide the model. Watch it ask for price. Provide. Watch it ask for customer response. Provide. Confirm it then moves to the next lead.
4. Wait until 6:00 PM IST (or trigger the scheduler manually via `POST /save-summary`). Open `summaries/summary_YYYY-MM-DD.md` and read the owner report.

**If anything misbehaves:** set `DATA_QUALITY_ENABLED=false` in `backend/.env`, then `docker compose up -d --build python-agent`. Behaviour reverts in ~60 seconds. Log the issue here.

**Known follow-ups (not blocking):**
- Quote-aging buckets in the active-pipeline section (split by 0-2d / 3-7d / >7d) — listed in design but not in v1 report. Add when needed.
- Brand-mix vs inquiry-mix comparison in the weekly summary. Listed in design.
- Conversion lag by source — currently always "Indiamart"; bucket comes free once `lead_source` diversifies.
