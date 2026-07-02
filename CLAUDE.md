# Pratibha Accountability Chatbot — Developer Guide for Claude

> **Migration #004 — LIVE as of 01 Jul 2026** (container rebuilt and running)
>
> All infrastructure from Migration #004 is now deployed in the running container:
> - ✅ `pratibha_agent_traces` table + auto-flags on every LLM turn (`traces.py`)
> - ✅ Two-window scheduler: 6 PM primary + 10 AM backup with idempotency (`scheduler.py`)
> - ✅ Claude-first summary narrative with Groq/template fallback (`summary_writer.py`) — syntax fix applied (nested f-string)
> - ✅ Hard-junk classifier + touch-4 consent flow (`hard_junk.py`)
> - ✅ Resurface openers with Cratio date/time prefix (`csv_parser_queue.py`)
> - ✅ Claude-based question queue construction, off by default (`queue_builder_claude.py`)
> - ✅ Daily monitor markdown alongside summary (`monitor_writer.py`)
> - ✅ Offline eval harness with 50 labelled seed cases (`eval/`)
> - ✅ Render deploy config + `DEPLOY.md` checklist
>
> **What's still BROKEN in agent behaviour** — infrastructure measures these but fixes were NOT written yet:
> 1. **Rigid 4-question follow-up loop** — `required_fields.FOLLOWUP_QUESTIONS` fires same 4-question template regardless of note or reply. This is the 26-Jun bug where Pratibha typed "just told you" 7 times.
> 2. **Extraction misses model/price** — `tools_quality.py` doesn't reliably parse `"dy 6800-ds"` or `"36000 + gst"` when Pratibha replies casually.
> 3. **`hard_junk.must_force_resurface()` is not wired in** — function exists in `hard_junk.py` but `csv_parser_queue.build_question_queue` doesn't call it, so ₹1Cr+ leads still don't get force-flagged.
> 4. **Follow-up templates ignore the note** — no "note-aware question rewrite" step yet. `CLAUDE_QUEUE_ENABLED=false` by default.
>
> Path forward: eval harness catches all four as failures. Fix each, re-run `python eval/run_eval.py`, deploy.
>
> See MIGRATIONS.md #004 for schema, DEPLOY.md for deploy steps, `eval/README.md` for the harness, `eval/baseline_scorecard.md` for the starting metric readout.

---

## What This Project Is

An internal agentic chatbot for **Pratibha** (HCA sales rep) that cross-checks her daily lead activity against Cratio CRM exports. The agent reads each day's export, identifies gaps and vague entries, and asks Pratibha specific questions about each lead. Her answers are stored in Postgres and surfaced as a daily digest for management.

**Problem it solves:** Cratio statuses are unreliable (marks leads as "Win" prematurely), activity notes are vague ("sent details" with no specifics), and manual report review by management misses things. The agent never forgets yesterday's leads and asks the exact question a manager would ask — but consistently, every day, for every lead.

**This is NOT the sales chatbot.** The HCA sales chatbot (`rag.html`) serves customers. This serves internal accountability. Separate interface, separate agent, shared Postgres instance (different tables).

**Node.js is kept** because it already runs for the HCA sales portfolio (pricing, catalog, auth, static files). It stays as a thin proxy/router — no business logic. All agent logic lives in Python FastAPI.

---

## System Architecture — Full

```
Browser (pratibha.html)
    ↕ REST (HTTP)
Node.js Express (port 3001) ← already running for HCA sales portfolio
    │  [THIN LAYER — routing, file handling, auth only. No business logic.]
    ├── POST /api/pratibha/upload-export  → parse CSVs → load into pratibha_leads table
    ├── POST /api/pratibha/chat           → proxy to Python agent on port 8001
    ├── GET  /api/pratibha/digest         → fetch today's digest from pratibha_digest table
    ├── GET  /api/pratibha/leads/:date    → fetch leads for a given date
    └── GET  /api/pratibha/summary/:date  → fetch saved daily conversation summary file
    ↕ HTTP (internal)
Python FastAPI agent (port 8001) ← new Docker service: pratibha-agent
    │  [FAT LAYER — all LangGraph logic, tool execution, Postgres reads/writes]
    ├── POST /chat    → LangGraph ReAct agent entry point
    ├── POST /save-summary → called at 6:00 PM IST by scheduler to write daily summary
    └── GET  /health
    ↕ psycopg2 (direct connection)
Postgres (port 5432) ← shared Docker instance with sales agent
    ├── pratibha_leads          — parsed daily CRM exports
    ├── pratibha_responses      — Pratibha's answers to agent questions
    ├── pratibha_digest         — daily management summaries (structured)
    └── pratibha_conversations  — full message-by-message conversation log per day
    ↕ file system write
Daily Summary Files (saved to ./summaries/ inside pratibha-agent container,
                     mounted to the Pratibha Chatbot project folder on host)
    └── summary_YYYY-MM-DD.md   — detailed + summarized daily report, one file per day
```

---

## Request Flow — Step by Step

### Upload flow (Pratibha uploads CSVs)
1. Browser sends 3 CSV files to `POST /api/pratibha/upload-export`
2. Node.js saves files to disk (`uploads/pratibha/YYYY-MM-DD/`)
3. Node.js calls Python agent `POST /parse-exports` with file paths
4. Python agent: runs `parse_and_load_exports()` → reads all 3 CSVs → deduplicates → inserts into `pratibha_leads`
5. Python agent: runs `build_question_queue()` → cross-references tables → returns ordered question list
6. Node.js stores question queue in session → returns `{status: "ready", question_count: N}` to browser
7. Browser shows chat interface and automatically sends first message to start conversation

### Chat flow (each message)
1. Browser sends `POST /api/pratibha/chat` with `{message, thread_id, date}`
2. Node.js proxies to Python agent `POST /chat`
3. Python agent: LangGraph ReAct loop runs
   - Classifies message (answer to question / free text / done signal)
   - If answer: calls `save_response()` tool → saves to `pratibha_responses`
   - Fetches next question from queue via `get_next_question()` tool
   - If queue empty: calls `generate_digest()` tool → saves to `pratibha_digest` → returns summary
4. Agent returns `{reply, done: bool}`
5. Node.js returns to browser
6. Browser renders reply in chat

### 6:00 PM IST daily summary (automated)
1. Scheduler (APScheduler inside Python agent, cron: `30 12 * * *` UTC = 6:00 PM IST) fires
2. Calls `generate_daily_summary(date=today)` internally
3. Fetches all rows from `pratibha_conversations` for today
4. Fetches `pratibha_digest` for today
5. LLM generates two versions: full detailed transcript + condensed management summary
6. Writes `summary_YYYY-MM-DD.md` to `./summaries/` (mounted to Pratibha Chatbot folder on host)
7. Also saves to `pratibha_digest.raw_summary` in Postgres (overwrites/updates)
8. If conversation is still in progress at 6 PM: saves partial summary, marks as `partial: true`

---

## File: `pratibha-agent/agent.py` — LangGraph Agent

### Graph structure
```
START → classify_input → [route]
    → answer_received → save_response → get_next_question → respond
    → queue_empty → generate_digest → respond
    → free_text → respond_directly
END
```

### LangGraph state
```python
class PratibhaState(TypedDict):
    messages: Annotated[list, add_messages]
    date: str                    # export date being reviewed
    question_queue: list[dict]   # ordered list of questions yet to ask
    current_question: dict       # question currently being asked
    responses_saved: int         # count of answers saved so far
    digest_generated: bool       # whether digest has been built
    session_summary: str         # compact running log of leads covered so far
                                 # e.g. "- Ramesh, Surendranagar — will retry tomorrow\n..."
                                 # passed to model instead of full message history
                                 # updated by save_response() after each answer
```

### Rate limit management — rolling session summary

Pratibha's session runs 20+ turns (one per lead). Two approaches that don't work:

- **Pass full message history** → token count grows every turn → hits Groq TPM cap by lead 10-15 (the 413 error the sales agent had)
- **Trim to just the latest message** → agent loses cross-session awareness → can't spot patterns, can't cross-question, misses things like "you've said 'will check' for 5 leads in a row"

**The fix: a rolling session summary passed instead of raw message history.**

After each answer is saved to Postgres, the agent maintains a compact structured log in state:

```
Session so far (Jun 17):
- Ramesh, Surendranagar — disconnected call, will retry tomorrow
- Goverdhan, Jodhpur — sent DY-1201 details, no response yet
- Basu Dutta, Dhekiajuli — sent details, customer asked for price revision
- Parmeshwar, Pune — 3 call attempts, no answer, marking junk
```

This is ~200 tokens regardless of whether it's lead 3 or lead 19. The model gets full cross-session awareness to spot patterns and cross-question, without the token count growing unboundedly. Raw message history is never passed to the model — only this compact log plus the current question.

**What this enables:**
- "You said you'd call back 3 people — it's the same answer every time. What's actually blocking you?"
- "You sent details to Goverdhan and now to this customer too — did you send the same model? They asked for different things."
- Pattern detection: agent can notice if Pratibha is giving vague answers across multiple leads and push back

**Implementation in `pre_model_hook`:**

```python
def pre_model_hook(state: PratibhaState):
    system = SystemMessage(content=SYSTEM_PROMPT)

    # Build compact session log from saved responses (not raw messages)
    session_log = state.get("session_summary", "No leads covered yet.")

    # Only the session summary + current question context + Pratibha's latest reply
    context = SystemMessage(content=f"Session so far:\n{session_log}")
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
    )
    trimmed = [system, context]
    if last_human:
        trimmed.append(last_human)
    return {"llm_input_messages": trimmed}
```

**`session_summary` field in state** is updated by `save_response()` after each answer — it appends one line per lead covered. Never grows beyond ~20 lines (one per lead per day). Full conversation is preserved in `pratibha_conversations` table for the 6 PM summary.

**Return `llm_input_messages` not `messages`** — same rule as the sales agent. Returning `messages` would append into the checkpoint on every turn and corrupt persisted state. `llm_input_messages` is input-only, not stored.

Tool results from `get_question_queue` and `get_next_question` must also be slimmed before reaching the model — pass only `{lead_id, contact_name, city, question}`, not the full lead row. Full data stays in Postgres.

### System prompt rules (hardcode these, never the data)
- Ask one question at a time — never show the full queue
- Always name the lead and city in the question — never generic
- If answer is "don't know" / "will check" / "dekhunga" → log as-is, move on, do not nag
- Tone: matter-of-fact colleague, not interrogating manager
- Never answer machine/price/spec questions — redirect to sales chatbot
- Do NOT put lead names, machine names, counts, or stages in this system prompt — always load from tools

---

## File: `pratibha-agent/tools.py` — All Agent Tools

### `get_question_queue(date: str) → list[dict]`
- Fetches all leads from `pratibha_leads` for given date
- Fetches stale leads from previous 2 days still in Yet To Talk or Followup
- Cross-references `pratibha_leads` with Sourcewise data (original_requirement field)
- Applies trigger logic (see Question Logic section)
- Returns ordered list: `[{lead_id, contact_name, city, trigger, question, original_requirement}]`
- Priority order: blank notes first, then stale multi-day leads, then vague notes, then junk without reason

### `get_next_question(date: str, responses_saved: int) → dict | None`
- Returns next unanswered question from the queue
- Returns `None` when all questions answered (signals digest generation)

### `save_response(lead_id, question, answer, date) → bool`
- Extracts structured fields from Pratibha's free-text answer using LLM:
  - `machine_sent` — any machine/model name mentioned
  - `call_attempts` — any number mentioned in context of calls
  - `follow_up_plan` — any next step mentioned
- Inserts into `pratibha_responses`
- Also appends to `pratibha_conversations` (full message log)

### `get_stale_leads(days_back: int = 2) → list[dict]`
- Queries `pratibha_leads` for leads where `export_date < today` AND `lead_stage IN ('Yet To Talk', 'Followup')`
- Returns leads with day count since first seen

### `generate_digest(date: str) → dict`
- Aggregates all `pratibha_responses` for the date
- Counts: total leads, contacted, details sent, junked, pending
- Collects: which models were sent, junk reasons, pending reasons
- LLM writes `raw_summary` paragraph (2-3 sentences, management-facing)
- Inserts/updates `pratibha_digest` for the date
- Returns the digest dict for agent to display to Pratibha

### `generate_daily_summary(date: str) → str`
- Called by scheduler at 6:00 PM IST (not by agent during conversation)
- Pulls full `pratibha_conversations` log for the date
- Pulls `pratibha_digest` for the date
- Generates two sections:
  - **Detailed**: full Q&A transcript, lead by lead, with original requirement and Pratibha's exact answer
  - **Summary**: 1-paragraph management digest + structured counts table
- Writes to `./summaries/summary_YYYY-MM-DD.md`
- Returns file path

---

## File: `pratibha-agent/csv_parser.py` — CSV Parsing Logic

### `parse_and_load_exports(activities_path, sourcewise_path, active_path, export_date) → int`
Reads all 3 CSVs, merges on mobile number, deduplicates, inserts into `pratibha_leads`.

**Step 1 — Parse `Lead_Activities_Details_Report`**
```
Columns: Lead Date, Assigned To, Company Name, Contact Name,
         Mobile Number, City, Lead Stage, Last Activity Date/Time, Last Activity Notes
Key field: Last Activity Notes (the vague note Pratibha logs)
```

**Step 2 — Parse `Sourcewise_Lead_Detailed_Report`**
```
Columns: Lead Source, Lead Date, Contact Name, Company Name, Mobile Number,
         Email, Assigned To, City, Lead Stage, Description
Key field: Description (original IndiaMart inquiry — has specific machine/model)
Clean HTML tags from Description: strip <br>, &amp; etc.
```

**Step 3 — Parse `Active_Leads`**
```
Columns: Assigned To, Lead Date, Company Name, Contact Name,
         Mobile Number, Lead Stage, Lead Source, Description, Lead Score, Engagement Score
Used for: Lead Score field only (not available in Activities report)
```

**Step 4 — Merge on mobile number**
- Primary source: Activities report (has city, activity note, activity timestamp)
- Enrich with: Sourcewise `Description` → stored as `original_requirement`
- Enrich with: Active Leads `Lead Score`
- If mobile number missing in one file: match on Contact Name + Lead Date as fallback

**Step 5 — Deduplicate**
- If same mobile_number + export_date already exists in `pratibha_leads`: UPDATE, do not INSERT
- This allows re-upload on same day without duplicate rows

**Step 6 — Clean activity notes**
- Strip trailing/leading whitespace
- Lowercase for trigger matching
- Store original raw note in `activity_note` field unchanged

### `extract_date_from_filename(filename: str) → date`
Handles all observed Cratio naming patterns:
- `Active_Leads_17june.csv` → June 17, current year
- `Active_Leads_17June26.csv` → June 17, 2026
- `Lead_Activities_Details_Report_17june.csv` → same
- Falls back to `date.today()` if no pattern matches

---

## File: `pratibha-agent/scheduler.py` — Daily Summary Scheduler

Uses **APScheduler** (runs inside the FastAPI process — no separate cron service needed).

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
scheduler.add_job(
    func=trigger_daily_summary,
    trigger=CronTrigger(hour=18, minute=0, timezone="Asia/Kolkata"),
    id="daily_summary",
    replace_existing=True
)
```

`trigger_daily_summary()`:
1. Gets today's date in IST
2. Calls `generate_daily_summary(date=today)`
3. Logs success/failure to stdout
4. If `pratibha_digest` for today does not exist (conversation never happened): writes a "no conversation today" summary file and logs it

---

## File: `pratibha-agent/summary_writer.py` — Daily Summary File Format

Summary files are saved to `./summaries/summary_YYYY-MM-DD.md` inside the container.
The `./summaries/` directory is Docker volume-mounted to the host `Pratibha Chatbot` project folder.

### File format: `summary_YYYY-MM-DD.md`

```markdown
# Pratibha Daily Summary — [DATE]
Generated: [TIMESTAMP IST]

---

## Management Summary

[2-3 sentence LLM-generated digest for management]

| Metric | Count |
|---|---|
| Total leads reviewed | N |
| Successfully contacted | N |
| Details sent | N |
| Marked junk | N |
| Still pending | N |

**Models quoted today:** [list or "None"]
**Junk reasons:** [list or "None"]

---

## Detailed Transcript

### Lead 1 — [Contact Name], [City]
**Original requirement:** [from IndiaMart description]
**Cratio stage:** [stage]
**Activity note:** "[raw note]"
**Question asked:** "[exact question agent asked]"
**Pratibha's answer:** "[exact answer]"
**Extracted — Machine sent:** [value or blank]
**Extracted — Call attempts:** [value or blank]
**Extracted — Follow-up plan:** [value or blank]

### Lead 2 — ...
[repeats for every lead in the conversation]

---

## Leads Not Discussed
[Any leads in today's export that were not reached in the conversation]
```

---

## Postgres Schema — Full

```sql
-- Parsed daily CRM exports (one row per lead per day)
CREATE TABLE pratibha_leads (
  id                   SERIAL PRIMARY KEY,
  export_date          DATE NOT NULL,
  contact_name         TEXT,
  company_name         TEXT,
  mobile_number        TEXT,
  city                 TEXT,
  lead_stage           TEXT,
  lead_source          TEXT,
  original_requirement TEXT,        -- cleaned Description from Sourcewise report
  last_activity_time   TIMESTAMPTZ,
  activity_note        TEXT,        -- raw note from Cratio, stored unchanged
  lead_score           INTEGER,
  created_at           TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(mobile_number, export_date) -- deduplication key
);

-- Pratibha's answers to agent questions
CREATE TABLE pratibha_responses (
  id              SERIAL PRIMARY KEY,
  export_date     DATE NOT NULL,
  lead_id         INTEGER REFERENCES pratibha_leads(id),
  contact_name    TEXT,
  question        TEXT,             -- exact question the agent asked
  answer          TEXT,             -- Pratibha's raw answer, unchanged
  machine_sent    TEXT,             -- LLM-extracted: machine/model mentioned
  call_attempts   INTEGER,          -- LLM-extracted: number of call attempts mentioned
  follow_up_plan  TEXT,             -- LLM-extracted: next step mentioned
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Daily management digests (structured counts)
CREATE TABLE pratibha_digest (
  id                   SERIAL PRIMARY KEY,
  digest_date          DATE NOT NULL UNIQUE,
  total_leads          INTEGER,
  contacted            INTEGER,
  details_sent         INTEGER,
  details_sent_models  TEXT[],       -- models mentioned across all "sent details" responses
  junked               INTEGER,
  junk_reasons         TEXT[],
  pending              INTEGER,
  pending_reasons      TEXT[],
  raw_summary          TEXT,         -- LLM-generated paragraph, management-facing
  partial              BOOLEAN DEFAULT FALSE,  -- true if saved before conversation ended
  created_at           TIMESTAMPTZ DEFAULT NOW()
);

-- Full message-by-message conversation log (for 6 PM summary generation)
CREATE TABLE pratibha_conversations (
  id          SERIAL PRIMARY KEY,
  conv_date   DATE NOT NULL,
  role        TEXT NOT NULL,        -- 'agent' or 'pratibha'
  content     TEXT NOT NULL,        -- exact message text
  lead_id     INTEGER REFERENCES pratibha_leads(id),  -- null for non-lead messages
  created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON pratibha_conversations(conv_date);
```

---

## Daily Data Inputs (Cratio CRM Exports)

Three CSV files exported from Cratio each day. All three cover the same date range.

### 1. `Lead_Activities_Details_Report_[date].csv` — PRIMARY FILE
The most important file. Shows what Pratibha actually did with each lead.

| Column | Description |
|---|---|
| Lead Date | When the lead came in |
| Assigned To | Always "Pratibha" |
| Company Name | Buyer's company (often blank) |
| Contact Name | Buyer's name |
| Mobile Number | Buyer's number |
| City | Buyer's city |
| Lead Stage | Current stage in Cratio |
| Last Activity Date/Time | When Pratibha last touched this lead |
| Last Activity Notes | What she logged — the key field, always vague |

**Known activity note patterns and what they mean:**
- `"we have sent details"` / `"we have sent details to customer"` → Which machine? Which model? What price? Did customer respond?
- `"we have sent details customer will visit on [date]"` → Which details/model was sent? Is the visit confirmed? What's the plan for it?
- `"customer not responding the call"` / `"customer not attend the call"` → How many attempts? Plan to retry or junk?
- `"customer disconnected the call"` → Will retry? Junk?
- `"customer not required"` / `"customer not required any machine"` → What were they actually looking for? Any future potential? — **flag if original requirement was high-value (bulk order / specific brand)**
- `"Switched off"` → First attempt or repeated?
- `"connected with [name] sir."` / `"Customer send to [name]"` → Who is that person? What was discussed? What's the next step?
- `"call after [time]"` → Did you call back after that time? What was the outcome?
- `"language issue"` → Acceptable junk reason, no further questioning needed
- `"customer need [specific requirement]"` → Customer described their need directly — did you identify the right machine? Did you send details?
- `""` (blank) → No activity at all — agent must ask why

### 2. `Sourcewise_Lead_Detailed_Report_[date].csv` — CROSS-REFERENCE
Contains the original customer inquiry from IndiaMart. Used to cross-check whether Pratibha's activity addressed the actual requirement.

| Column | Description |
|---|---|
| Lead Source | Always "Indiamart" currently |
| Lead Date | |
| Contact Name | |
| Company Name | |
| Mobile Number | |
| Email | Customer email (sometimes present) |
| Assigned To | |
| City | |
| Lead Stage | |
| Description | Original IndiaMart inquiry — has the specific machine/model requested |

### 3. `Active_Leads_[date].csv` — STAGE TRACKING
Used for Lead Score enrichment and cross-day stage comparison.

| Column | Description |
|---|---|
| Assigned To | |
| Lead Date | |
| Company Name | |
| Contact Name | |
| Mobile Number | |
| Lead Stage | Yet To Talk / Junk / Followup / New / Win |
| Lead Source | |
| Description | |
| Lead Score | Cratio score (0-100) |
| Engagement Score | |

**Lead Stage values observed:**
- `Yet To Talk` — lead assigned, no contact made
- `New` — first contact attempted
- `Followup` — Pratibha is following up
- `Junk` — marked irrelevant
- `Win` — Cratio marks this prematurely — DO NOT treat as actual conversion

---

## Question Logic — Trigger → Question Mapping

The agent cross-references all three files, builds the queue before conversation starts, asks one question at a time.

| Trigger | Agent question |
|---|---|
| Blank activity note | "No activity logged for [Name] from [City] who asked about [original_requirement]. Did you call them? What happened?" |
| Note contains "sent details" (any variant) | "For [Name] — you sent details. Which machine/model did you send? What was the price quoted? Have they responded?" |
| Note contains "sent details" + "will visit on [date]" | "For [Name] — details sent and visit planned for [date]. Which model did you send details for? Is the visit confirmed?" |
| Note contains "not responding" / "not attend the call" | "For [Name] — how many times have you tried calling? Will you try again or mark as junk?" |
| Note contains "disconnected" | "For [Name] who enquired about [original_requirement] — they disconnected. Will you follow up?" |
| Note contains "not required" | "For [Name] — what did they actually need? Any future potential or permanently junk?" |
| Note contains "not required" AND original_requirement has bulk quantity or high order value | "**FLAG:** [Name] was junked as 'not required' but their IndiaMart inquiry was for [quantity/value]. Are you sure this is junk? What did they actually say?" |
| Note contains "call after [time]" | "For [Name] — you noted to call after [time]. Did you call back? What was the outcome?" |
| Note contains "send to [person]" / "sent to [person]" | "For [Name] — you forwarded this to [person]. Who is that? What happened with it? Are you still tracking this?" |
| Note contains "customer need [description]" | "For [Name] — customer described their need as '[note]'. Did you identify the right machine for this? Did you send details?" |
| Note contains "language issue" | Skip — acceptable junk reason, no question needed |
| Note mentions a person name (e.g. "subhash sir") | "You mentioned connecting with someone — who are they? What was discussed? What's the next step?" |
| Stage = Junk, note blank or vague | "Why was [Name] marked junk? Bad contact info or genuinely not a buyer?" |
| Stage unchanged from yesterday (Yet To Talk → Yet To Talk) | "This lead from [original_date] is still 'Yet To Talk' after [N] days. What's blocking contact?" |
| Stage = Followup, last_activity_time > 1 day ago | "[Name] (Followup) was last updated [X] days ago. What's the current status?" |

### Cross-reference check
When note = "sent details" AND `original_requirement` has a specific model:
→ "The customer asked specifically about [model]. Did you send details for that, or a different model?"

When note = "sent details" AND original_requirement mentions **second-hand** machine:
→ "The customer was looking for a second-hand [model]. Did you send details for a second-hand machine or a new one?"

### High-value junk flag (critical)
When stage = Junk AND original_requirement contains any of:
- A quantity > 5 pieces
- "Probable Order Value" field
- Bulk-sounding terms ("thaan", "pieces", "units")

→ Agent must explicitly flag: "This lead had [quantity/value] in their inquiry. Before marking junk, are you sure they're not a buyer?"

Example from June 19: LALU, Mandsaur — junked as "customer not required" but inquiry was for 95 pieces of Zoje HS Lockstitch, probable order value Rs 17.6–18.5 lakh.

---

## Frontend — `pratibha.html`

Separate HTML page. Pratibha does not see `rag.html`. Same visual style for consistency.

**Page layout:**
- Top section: CSV upload panel with 3 labeled file slots (Activities, Sourcewise, Active Leads) + "Upload & Begin" button
- Below: chat interface (same card/bubble style as rag.html)
- Header: date label showing which day's leads are being reviewed
**Frontend logic:**
- On upload → POST to `/api/pratibha/upload-export` → on success, auto-send first message "start" to trigger question queue
- Each chat message → POST to `/api/pratibha/chat` → render reply
- When agent returns `done: true` → show "Session complete. Summary saved." message
- No thread sidebar needed — one conversation per day

---

## Backend Routes — `backend/server.js` additions

```javascript
// Upload and parse the 3 Cratio CSV exports
POST /api/pratibha/upload-export
  - Accepts multipart form: activities_file, sourcewise_file, active_file
  - Saves to Pratibha Chatbot/uploads/YYYY-MM-DD/
  - Calls Python agent POST /parse-exports with file paths
  - Returns { status: "ready", question_count: N, date: "YYYY-MM-DD" }

// Proxy chat messages to Python agent
POST /api/pratibha/chat
  - Body: { message, thread_id, date }
  - Proxies to PRATIBHA_AGENT_URL/chat
  - Returns { reply, done }

// Fetch today's management digest
GET /api/pratibha/digest
  - Optional query: ?date=YYYY-MM-DD (defaults to today)
  - Queries pratibha_digest table directly
  - Returns structured digest JSON

// Fetch leads for a given date (for management review)
GET /api/pratibha/leads/:date
  - Returns all pratibha_leads rows for that date with joined pratibha_responses

// Fetch saved daily summary file
GET /api/pratibha/summary/:date
  - Reads summary_YYYY-MM-DD.md from summaries/ folder
  - Returns file contents as text
```

---

## Docker — `docker-compose.yml` additions

```yaml
pratibha-agent:
  build: ./pratibha-agent
  ports:
    - "8001:8001"
  environment:
    - DATABASE_URL=postgresql://hca:hca_secret@postgres:5432/hca_agent
    - GROQ_API_KEY=${GROQ_API_KEY}
    - API_BASE=http://host.docker.internal:3001
  volumes:
    - /path/to/Pratibha Chatbot/summaries:/app/summaries     # ← daily summary .md files
    - /path/to/Pratibha Chatbot/uploads:/app/uploads         # ← uploaded CSVs accessible to agent
  depends_on:
    - postgres
```

Volume `./pratibha-summaries` on the host → maps to `/app/summaries` in container → summary `.md` files written here are accessible on the host machine directly.

---

## Environment Variables — `backend/.env` additions

```env
PRATIBHA_AGENT_URL=http://localhost:8001
```

---

## Upload Storage & Cross-Day Data Flow

### Where uploaded files are saved
Node.js saves all 3 CSV files to a dated folder inside the Pratibha Chatbot project (this folder), not inside main-portfolio — these are two separate projects:

```
Pratibha Chatbot/
  uploads/
    2026-06-17/
      Lead_Activities_Details_Report_17june.csv
      Sourcewise_Lead_Detailed_Report_17june.csv
      Active_Leads_17june.csv
    2026-06-18/
      (next day's files)
    2026-06-19/
      ...
  summaries/
    summary_2026-06-17.md
    summary_2026-06-18.md
    ...
  CLAUDE.md
```

One folder per day. Raw CSVs are kept permanently as an archive. Summaries land in the same project folder via Docker volume mount.

### How the agent cross-checks across days

The agent does NOT re-read old CSV files. The moment a CSV is uploaded, the parser reads it and loads every lead into the `pratibha_leads` Postgres table with an `export_date` column. Postgres is the memory — CSVs are just the daily input.

```
Day 1 upload → CSVs saved to disk → parsed → loaded into Postgres (export_date = Jun 17)
Day 2 upload → CSVs saved to disk → parsed → loaded into Postgres (export_date = Jun 18)
                                           ↓
                 Agent queries Postgres: leads from Jun 17 still in "Yet To Talk" on Jun 18
                 → asks Pratibha about them in today's session
```

**What this means practically:**
- If you skip a day's upload, the agent still has those leads in Postgres and will flag them the next time you do upload
- Stale lead tracking (`get_stale_leads` tool) looks back 2 days by default — configurable
- The agent builds context over time: if a lead appears across 3 consecutive days with no progress, it knows that and adjusts its question accordingly
- No manual cross-referencing needed — the database accumulates automatically with each daily upload

---

## File Naming Convention for Cratio Exports

Parser must handle both naming patterns Cratio uses:
- `Active_Leads_17june.csv`
- `Active_Leads_17June26.csv`
- `Lead_Activities_Details_Report_17june.csv`
- `Sourcewise_Lead_Detailed_Report_17june.csv`

Function `extract_date_from_filename()` tries regex patterns in order, falls back to `date.today()`.

---

## Running Locally

```bash
# 1. Create pratibha-agent/ directory with Dockerfile, agent.py, tools.py, csv_parser.py, scheduler.py, summary_writer.py
# 2. Add pratibha-agent service to docker-compose.yml (see above)
# 3. Add to backend/.env: PRATIBHA_AGENT_URL
# 4. Create pratibha-summaries/ folder in project root (for volume mount)

docker compose up -d --build

# 5. Open pratibha.html in browser
# 6. Upload today's 3 CSVs → chat starts automatically
# 7. At 6:00 PM IST → summary_YYYY-MM-DD.md appears in pratibha-summaries/
```

---

## Known Constraints

1. **Cratio "Win" stage is unreliable** — never treat as confirmed conversion. Accounts data (Phase 2) is the only ground truth.
2. **Activity notes are always vague** — the question logic is built around this. Do not expect the notes to improve.
3. **Blank notes are common** — 5 out of 20 leads on 17 June had no activity note at all.
4. **One export per day** — if Pratibha uploads multiple times, deduplicate by `mobile_number + export_date`, keep latest.
5. **Accounts cross-check deferred** — Phase 2 adds invoice data to verify actual conversions. Do not build yet.
6. **Port 8001** — Pratibha agent runs on 8001. HCA sales agent stays on 8000. Never mix them.
7. **Summary at 6 PM is best-effort** — if conversation is still ongoing, saves partial summary marked `partial: true` and re-runs at end of conversation automatically.

---

## Pending Tasks (in priority order)

### Migration #004 phase 1 — LIVE (01 Jul 2026)

All infrastructure deployed. Container rebuilt and running as of 01 Jul 2026. `summary_writer.py` nested f-string bug also fixed in this deploy.

- ✅ Migration #004 schema (`csv_parser.ensure_tables` + `MIGRATIONS.md`)
- ✅ Two-window scheduler with idempotency (`scheduler.py`)
- ✅ Claude-first summary narrative + Groq/template fallback (`summary_writer.py`)
- ✅ Hard-junk classifier + touch-4 consent flow (`hard_junk.py`)
- ✅ Resurface openers with Cratio date/time (`csv_parser_queue.py`)
- ✅ Trace writer on every turn (`traces.py`, hooked into `agent.py::answer_received_node`)
- ✅ Note-aware Claude queue construction, feature-flagged off (`queue_builder_claude.py`)
- ✅ Daily monitor writer (`monitor_writer.py`)
- ✅ Eval harness — 50 labelled seed cases, deterministic + Claude judge layers (`eval/`)
- ✅ Render config + deploy checklist (`render.yaml`, `DEPLOY.md`)
- ✅ Manual "Generate Today's Narrative" button (Cowork task `pratibha-daily-narrative` + artifact `pratibha-narrative-button`)

### Migration #004 phase 2 — DONE (02 Jul 2026)

All 4 agent behaviour fixes shipped. See `MIGRATIONS.md #004 phase 2` for full change log.

1. ✅ **Rigid follow-up loop fixed.** `agent.py` now imports `MAX_FOLLOWUPS = 2` from `required_fields.py` (was hardcoded 3). `asked_fields` loop-guard prevents re-asking a field that extraction failed on. Hard cap is 2 follow-ups per lead.
2. ✅ **Extraction of casual replies fixed.** `tools_quality.py EXTRACTION_PROMPT` now has 7 few-shot examples: "dy 6800-ds overlock" → machine_sent, "36000 + gst" → price, bare "2" → call_attempts, "just told you above" → null (parroting), "0" → price 0, combined model+price+status reply.
3. ✅ **`must_force_resurface` wired into queue.** `build_question_queue` runs a second pass fetching `auto_junked`/`declined` customers (last 7 days, `resurface_blocked=FALSE`), checks `must_force_resurface`, and injects `⚠ DIRECTOR FLAG` items at the front of the queue.
4. ✅ **Touch-4 consent flow live.** `_build_question_for_customer` uses `touch_4_prompt` + `trigger=touch_4_consent` when `touch_count >= 3`. `agent.py::answer_received_node` routes these through `_handle_touch_4_answer` → `handle_touch_4_reply`.

**Next step:** `python eval/run_eval.py --layer 1` — expect A1, A3, A7 to now pass. Save scorecard. Then follow `DEPLOY.md` to ship.

### Phase 3 (deferred)

- Accounts / invoice cross-check (Phase 2 of the original spec)
- Full LangGraph agent-invoke replay in `run_eval.py` so `candidate_output` is regenerated from the live agent instead of static text
- Claude LLM-judge (A4 + B3) — needs `ANTHROPIC_API_KEY`
