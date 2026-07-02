# Pratibha Chatbot — Deploy Checklist (Migration #004)

Follow this before every push to Render.

## Pre-deploy gate (blocking)

1. **Run the eval harness locally**
   ```bash
   cd pratibha-agent
   python eval/run_eval.py
   ```
   Exit code must be `0`. If any Blocker metric fails, deploy is blocked. Fix or add regressions row before continuing.

2. **Verify env vars are populated** in `.env` for local, in Render dashboard for prod:
   - `GROQ_API_KEY` — **required** (Qwen agent loop + fallback narrative)
   - `ANTHROPIC_API_KEY` — **optional**. Leave empty if you don't have one yet;
     everything Claude-touching (summary narrative, queue builder, LLM-judge)
     falls back to Groq/Qwen automatically. When you have a Claude key, paste
     it in and set `SUMMARY_LLM=claude`.
   - `DATABASE_URL` — populated by Render from the `pratibha-db` linkage
   - `SUMMARY_LLM=groq` **default while no Claude key** (switch to `claude` when key available)
   - `TRACES_ENABLED=true`
   - `MONITOR_ENABLED=true`
   - `CLAUDE_QUEUE_ENABLED=false` (also requires Claude key when enabled)

   **Without an Anthropic key you lose only:**
   - Layer 2 of the eval harness (A4 note-aware judge + B3 summary quality
     judge). Layer 1 still runs — 8 of the 10 metrics still gate deploy.
   - The narrative "two things worth raising" prose becomes Qwen-quality
     instead of Claude-quality (readable, less sharp).
   - Note-aware Claude queue phrasing (already off by default).

3. **Migrations run automatically on boot** via `csv_parser.ensure_tables`. Verify by watching the pratibha-agent startup logs for:
   ```
   [Scheduler] 6 PM primary + 10 AM backup active (IST)
   ```
   No `psycopg2.errors` in the first 60 s = migrations OK.

4. **Sanity check on staging**
   - POST to `/health` → `{"status":"ok"}`
   - Upload the 26-Jun test CSVs, run a session
   - After 5 turns, `SELECT auto_flags FROM pratibha_agent_traces LIMIT 5` — flags array should be present (empty for good turns, populated for known-bug turns)

## Deploy

```bash
git commit -am "eval harness + Claude summary (Migration #004)"
git push origin main       # Render auto-deploys
```

Watch the Render deploy log for the `pratibha-agent` service. Expected sequence:
1. Docker build succeeds
2. `psycopg2` connects to `pratibha-db`
3. `ensure_tables` runs (idempotent)
4. `LangGraph agent ready`
5. `Scheduler` starts

## Post-deploy verification

- Open `pratibha.html` on the Render URL (once known)
- Upload today's CSVs
- Do a 3-lead test conversation
- Watch `pratibha_agent_traces` populate:
  ```sql
  SELECT session_date, COUNT(*), array_agg(DISTINCT unnest(auto_flags))
  FROM pratibha_agent_traces
  WHERE session_date = CURRENT_DATE
  GROUP BY session_date;
  ```

## First 6 PM window (production)

- `pratibha_digest` gets `status='complete'` and `generated_by='claude'`
- `summary_2026-XX-XX.md` appears in the mounted disk
- `summary_2026-XX-XX.html` also appears (director narrative view)
- `monitor_2026-XX-XX.md` appears alongside — this is the daily eval readout

If any of those are missing, check:
```
docker logs pratibha_agent | grep -E "Scheduler|Monitor|Claude"
```

## First 10 AM backup window

- Only runs if `status != 'complete'` on the previous day
- Look for `[10am] Nothing to reprocess` in the log = healthy
- If it reprocesses something, `attempt_count` increments and `failure_reason` records why

## Rollback

Every feature flag defaults to disabled-when-off. To revert:

```
SUMMARY_LLM=groq            # narrative back to Qwen
CLAUDE_QUEUE_ENABLED=false  # queue back to legacy csv_parser_queue
TRACES_ENABLED=false        # stop writing traces (existing rows preserved)
MONITOR_ENABLED=false       # stop daily monitor
```

Schema changes from Migration #004 are all additive — rolling back means only that new columns / new tables stay populated but unused. No `DROP` is ever required.
