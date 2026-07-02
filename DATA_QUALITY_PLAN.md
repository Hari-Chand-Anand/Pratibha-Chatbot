# Pratibha Chatbot — Data Quality + Owner Report (Migration #003)

**Status:** plan + code. Code lands in same change set per user direction "make changes now".
**Folder (confirmed):** `C:\Users\ADMIN\Desktop\Pratibha Chatbot\`
**Feature flag:** `DATA_QUALITY_ENABLED` in `backend/.env`. Default `true`. Flip to `false` → falls back to Memory-Fix-only behaviour.
**Reversibility:** schema additions are nullable + additive. Drop the flag, old code keeps working.

---

## 1. Why this exists

Memory Fix made the system remember leads. Data Quality makes the data **useful** — to the owner (numeric daily report) and to brain (structured, queryable, trustworthy).

Two problems Memory Fix didn't solve:
1. Pratibha can still answer "yes sent details" and the LLM-driven follow-up sometimes lets it pass. Result: `machine_sent=null, price_quoted=null` — the lead is logged but uncaptured.
2. The 6 PM digest is prose, not numbers. Owner can't see today's quote value, conversion rate, or money at risk.

---

## 2. Required-fields contract (the spine)

Each trigger type declares what data MUST be on `pratibha_responses` before the lead is "done for the day". Lives in `pratibha-agent/required_fields.py`.

```python
REQUIRED_FIELDS = {
    "sent_details":         ["machine_sent", "price_quoted_inr", "customer_response_status"],
    "sent_details_visit":   ["machine_sent", "visit_date", "customer_response_status"],
    "not_responding":       ["call_attempts", "next_action", "next_action_date"],
    "disconnected":         ["call_attempts", "next_action"],
    "not_required":         ["why_not_required", "future_potential"],
    "high_value_junk_flag": ["actual_customer_response", "junk_reason"],
    "customer_described_need": ["machine_sent", "price_quoted_inr"],
    "forwarded_to_person":  ["forwarded_to_name", "handoff_status"],
    "callback_pending":     ["callback_outcome", "next_action"],
    "returning_customer":   ["machine_sent", "price_quoted_inr", "next_action"],
    "followup_touch":       ["customer_response_status", "next_action"],
    "multi_inquiry":        ["machine_sent", "customer_response_status"],
    "blank_note":           ["call_attempts", "next_action"],
    "person_mentioned":     ["forwarded_to_name", "handoff_status"],
    "junk_no_reason":       ["junk_reason"],
    "followup_stale":       ["customer_response_status", "next_action"],
}

FOLLOWUP_QUESTIONS = {
    "machine_sent":            "Which exact model — DY-1201, ZOJE HS, something else? I need the model number.",
    "price_quoted_inr":        "What price did you quote — exact figure in rupees?",
    "customer_response_status":"Has the customer replied — yes positively, no response, revision requested, or declined?",
    "visit_date":              "When is the visit planned — exact date (e.g. 28 Jun)?",
    "call_attempts":           "How many times did you try calling — exact number?",
    "next_action":             "What is the next action — call back, send revision, schedule visit, or junk?",
    "next_action_date":        "When will you do the next action — date please.",
    "why_not_required":        "Why didn't they need it — what did they actually want?",
    "future_potential":        "Is there future potential or permanently junk?",
    "actual_customer_response":"What did the customer actually say when you spoke to them?",
    "junk_reason":             "Why was this junked — bad contact, language issue, or no real need?",
    "forwarded_to_name":       "Who exactly did you forward it to — name?",
    "handoff_status":          "What happened after you forwarded — did they take it forward?",
    "callback_outcome":        "What was the outcome when you called back?",
}
```

Helper:
```python
def missing_fields(trigger: str, response_row: dict) -> list[str]:
    required = REQUIRED_FIELDS.get(trigger, [])
    return [f for f in required if not response_row.get(f)]

def compute_completeness_score(trigger: str, response_row: dict) -> int:
    required = REQUIRED_FIELDS.get(trigger, [])
    if not required:
        return 10
    filled = sum(1 for f in required if response_row.get(f))
    return round(10 * filled / len(required))
```

---

## 3. Expanded LLM extraction (in `save_response`)

One prompt change. Extracts 14 fields instead of 5:

```
{
  "machine_sent": ..., "price_quoted_inr": ...,
  "customer_response_status": "awaiting|positive|revision_requested|visit_planned|declined|null",
  "visit_date": "YYYY-MM-DD or null",
  "call_attempts": int|null,
  "next_action": "call|visit|quote_revision|junk|null",
  "next_action_date": "YYYY-MM-DD or null",
  "follow_up_plan": ..., "dropout_status": "ordered|declined|null",
  "why_not_required": ..., "future_potential": ...,
  "actual_customer_response": ..., "junk_reason": ...,
  "forwarded_to_name": ..., "handoff_status": ..., "callback_outcome": ...,
  "summary_line": ...
}
```

Drop-out keyword fallback unchanged (already covers `ordered`/`declined`).

---

## 4. Deterministic follow-up (replaces `evaluate_answer`)

After `save_response` runs the LLM extraction and writes pratibha_responses, the agent checks `missing_fields(trigger, row)`. If non-empty AND `followup_count < MAX_FOLLOWUPS (=3)`:
- Pick the FIRST missing field (in declaration order)
- Look up its question in `FOLLOWUP_QUESTIONS`
- Stay on the same lead, ask that one targeted question

No more "is satisfied?" LLM judgement call. Behaviour is deterministic, debuggable, and the same field is always asked the same way.

---

## 5. Schema (Migration #003 — additive only)

```sql
ALTER TABLE pratibha_responses
  ADD COLUMN IF NOT EXISTS price_quoted_inr           NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS customer_response_status   TEXT,
  ADD COLUMN IF NOT EXISTS visit_date                 DATE,
  ADD COLUMN IF NOT EXISTS next_action                TEXT,
  ADD COLUMN IF NOT EXISTS next_action_date           DATE,
  ADD COLUMN IF NOT EXISTS why_not_required           TEXT,
  ADD COLUMN IF NOT EXISTS future_potential           TEXT,
  ADD COLUMN IF NOT EXISTS actual_customer_response   TEXT,
  ADD COLUMN IF NOT EXISTS junk_reason                TEXT,
  ADD COLUMN IF NOT EXISTS forwarded_to_name          TEXT,
  ADD COLUMN IF NOT EXISTS handoff_status             TEXT,
  ADD COLUMN IF NOT EXISTS callback_outcome           TEXT,
  ADD COLUMN IF NOT EXISTS trigger_type               TEXT,
  ADD COLUMN IF NOT EXISTS completeness_score         INTEGER;

CREATE OR REPLACE VIEW pratibha_daily_board AS
SELECT
  pr.export_date                                                AS report_date,
  COUNT(DISTINCT pr.lead_id)                                    AS contacted,
  COUNT(*) FILTER (WHERE pr.machine_sent IS NOT NULL)           AS quotes_sent,
  COALESCE(SUM(pr.price_quoted_inr)
           FILTER (WHERE pr.machine_sent IS NOT NULL), 0)       AS quote_value_inr,
  COUNT(DISTINCT pc.mobile_number)
    FILTER (WHERE pc.lifecycle_status='ordered'
            AND pc.last_resolution_at::date = pr.export_date)   AS orders_today,
  COUNT(DISTINCT pc.mobile_number)
    FILTER (WHERE pc.lifecycle_status='declined'
            AND pc.last_resolution_at::date = pr.export_date)   AS declined_today,
  COUNT(DISTINCT pc.mobile_number)
    FILTER (WHERE pc.lifecycle_status='auto_junked'
            AND pc.last_resolution_at::date = pr.export_date)   AS auto_junked_today,
  ROUND(AVG(pr.completeness_score)::numeric, 1)                 AS avg_completeness
FROM pratibha_responses pr
LEFT JOIN pratibha_customers pc ON pc.mobile_number = pr.mobile_number
GROUP BY pr.export_date;
```

No DROP. No destructive ALTER. Old `pratibha_responses` rows continue to work (new columns just NULL on them).

---

## 6. Owner report at 6 PM IST

`summary_writer.generate_daily_summary(date)` rewritten. Reads `pratibha_daily_board` view + several auxiliary queries. Emits `summaries/summary_YYYY-MM-DD.md` with:

1. Pipeline today (counts, vs yesterday)
2. Money moved today (quote ₹, order ₹, lost ₹)
3. Active pipeline (open quote value, broken down by awaiting/revision/visit)
4. Red flags (high-value junk, near-auto-junk, mismatch asked-vs-sent)
5. Today's conversions (names + amounts)
6. Week so far (orders count, ₹, conversion rate, avg days-to-quote, completeness trend)
7. Pratibha today (leads reviewed, follow-up rate, vague-answer count, completeness score)

Format already approved in chat. Saved to mounted `summaries/` folder.

---

## 7. Acceptance — verifiable behaviours

- "Yes sent details" answer triggers a model-number follow-up, then a price follow-up, then a customer-response follow-up. Three deterministic questions, all targeting specific data gaps.
- `pratibha_responses.completeness_score` is set on every row.
- 6 PM digest writes a numeric report. Owner can read "today: 3 quotes ₹4.85 L, 1 order ₹17.6 L, 2 auto-junked" in 60 seconds.
- `pratibha_daily_board` view is queryable from psql / brain.
- Flipping `DATA_QUALITY_ENABLED=false` reverts to Memory-Fix-only behaviour without touching data.

---

## 8. Files changed (this migration)

All under `C:\Users\ADMIN\Desktop\Pratibha Chatbot\`:

- `pratibha-agent/csv_parser.py` — append Migration #003 SQL to ensure_tables.
- `pratibha-agent/required_fields.py` — NEW. Contract + helpers.
- `pratibha-agent/tools.py` — expanded extraction + completeness + missing-field surfacing in save_response. Numeric counts in generate_digest.
- `pratibha-agent/agent.py` — replace evaluate_answer with deterministic missing-field follow-up. MAX_FOLLOWUPS=3.
- `pratibha-agent/summary_writer.py` — rewritten for numbers-first report.
- `MIGRATIONS.md` — append #003.
- `CHANGES.md` — append deploy entry.
- `MEMORY_FIX_PLAN.md` — append §10.4 audit entry.
