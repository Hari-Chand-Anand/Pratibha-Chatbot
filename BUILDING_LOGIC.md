# Pratibha Chatbot — Building Logic

Complete implementation guide. Build in the order listed — each step depends on the previous.

---

## Build Order

```
Step 1 → pratibha-agent/csv_parser.py       (parse + load CSVs into Postgres)
Step 2 → pratibha-agent/tools.py            (all 5 agent tools)
Step 3 → pratibha-agent/agent.py            (LangGraph graph + state + pre_model_hook)
Step 4 → pratibha-agent/scheduler.py        (APScheduler at 6 PM IST)
Step 5 → pratibha-agent/summary_writer.py   (daily .md file generator)
Step 6 → pratibha-agent/Dockerfile          (Docker image)
Step 7 → main-portfolio/docker-compose.yml  (add pratibha-agent service)
Step 8 → main-portfolio/backend/server.js   (add /api/pratibha/* routes)
Step 9 → main-portfolio/pratibha.html       (frontend UI)
Step 10 → Test end-to-end with 17 June exports
```

---

## Step 1 — `pratibha-agent/csv_parser.py`

### Purpose
Reads the 3 Cratio CSV exports, merges them on mobile number, deduplicates, and loads into `pratibha_leads`. Also extracts the export date from the filename.

### Functions to build

#### `extract_date_from_filename(filename: str) → date`
```
Input:  "Active_Leads_17june.csv"  OR  "Active_Leads_17June26.csv"
Output: datetime.date(2026, 6, 17)

Logic:
  - Try regex: r'(\d{1,2})(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(\d{2})?'
  - If 2-digit year found → prepend "20"
  - If no year → use current year
  - If no match at all → return date.today()
  - Month matching is case-insensitive
```

#### `clean_html(text: str) → str`
```
Input:  "Requirement for DUKE-DK933<br>Brand : DUKE<br>"
Output: "Requirement for DUKE-DK933 Brand : DUKE"

Logic:
  - Replace <br>, <br/>, <BR> with space
  - Strip &amp; → &, &nbsp; → space
  - Strip any remaining HTML tags with regex r'<[^>]+>'
  - Collapse multiple spaces
  - Strip leading/trailing whitespace
```

#### `parse_and_load_exports(activities_path, sourcewise_path, active_path, export_date) → int`
```
Returns: number of rows inserted/updated

Step 1 — Read activities CSV
  df_act = pd.read_csv(activities_path)
  Columns used:
    Contact Name, Mobile Number, Company Name, City,
    Lead Stage, Last Activity Date/ Time, Last Activity Notes
  Clean: strip whitespace from all string columns

Step 2 — Read sourcewise CSV
  df_src = pd.read_csv(sourcewise_path)
  Columns used: Mobile Number, Description
  Clean: run clean_html() on Description column
  Rename Description → original_requirement

Step 3 — Active Leads CSV
  SKIP for data enrichment. Lead Score is always 0 or 10 with no real differentiation — confirmed
  across both June 17 and June 19 exports. Do not import Lead Score into pratibha_leads.
  Active Leads file is accepted in the upload UI for completeness but not parsed.

Step 4 — Merge
  base = df_act
  base = base.merge(df_src[['Mobile Number','original_requirement']],
                    on='Mobile Number', how='left')
  # No merge from Active Leads — Lead Score dropped

Step 5 — Upsert into Postgres
  For each row:
    INSERT INTO pratibha_leads (export_date, contact_name, company_name,
      mobile_number, city, lead_stage, original_requirement,
      last_activity_time, activity_note)
    VALUES (...)
    ON CONFLICT (mobile_number, export_date)
    DO UPDATE SET
      lead_stage = EXCLUDED.lead_stage,
      activity_note = EXCLUDED.activity_note,
      last_activity_time = EXCLUDED.last_activity_time

Step 6 — Return count of rows processed
```

#### `build_question_queue(export_date: date, conn) → list[dict]`
```
Returns: [{lead_id, contact_name, city, trigger, question, original_requirement}]

Priority order (highest first):
  1. Blank activity note
  2. Stage unchanged from yesterday (Yet To Talk → Yet To Talk)
  3. Note contains "sent details" (any variant)
  4. Note contains "not responding"
  5. Note contains "disconnected"
  6. Note contains "not required"
  7. Note mentions a person name (regex: "[a-z]+ sir")
  8. Junk stage with vague/blank note
  9. Followup not updated in >1 day

Trigger logic for each lead:
  note_lower = activity_note.lower().strip()

  if not note_lower:
      trigger = "blank_note"
      question = f"No activity logged for {name} from {city} who asked about {req}. Did you call them? What happened?"

  elif "sent details" in note_lower or "sent detail" in note_lower:
      trigger = "sent_details"
      if original_requirement and len(original_requirement) > 10:
          question = f"For {name} — you sent details. Which machine/model did you send? The customer asked about {req}. Did you send that specifically? Have they responded?"
      else:
          question = f"For {name} — you sent details. Which machine/model did you send? What was the price? Have they responded?"

  elif "not responding" in note_lower or "not respond" in note_lower:
      trigger = "not_responding"
      question = f"For {name} — how many times have you tried calling? Will you try again or should we mark as junk?"

  elif "disconnected" in note_lower:
      trigger = "disconnected"
      question = f"For {name} who enquired about {req} — they disconnected. Will you follow up?"

  elif "not required" in note_lower:
      trigger = "not_required"
      question = f"For {name} — what did they actually need? Any future potential or permanently junk?"

  elif "send to" in note_lower or "sent to" in note_lower:
      # e.g. "Customer send to ms Roopa" — forwarded to someone else
      person = note_lower.replace("customer send to", "").replace("sent to", "").strip().title()
      trigger = "forwarded_to_person"
      question = f"For {name} — you forwarded this to {person}. Who is that? What happened with it? Are you still tracking this lead?"

  elif "call after" in note_lower:
      # e.g. "call after 2pm", "call after 1pm"
      time_ref = note.lower().replace("call after", "").strip()
      trigger = "callback_pending"
      question = f"For {name} — you noted to call after {time_ref}. Did you call back? What was the outcome?"

  elif "visit" in note_lower and "sent detail" in note_lower:
      # e.g. "we have sent details customer will visit on 24th june"
      trigger = "sent_details_visit_planned"
      question = f"For {name} — details sent and a visit is planned. Which model did you send details for? Is the visit confirmed?"

  elif re.search(r'customer need|customer needs|customer want', note_lower):
      # Customer described requirement directly, not just a vague note
      trigger = "customer_described_need"
      question = f"For {name} — customer described their need as '{note}'. Did you identify the right machine for this? Did you send them details?"

  elif "language issue" in note_lower:
      # Acceptable junk reason, skip
      trigger = "skip_language_issue"
      # Do not add to question queue

  elif re.search(r'\b\w+ sir\b', note_lower):
      trigger = "person_mentioned"
      question = f"You mentioned connecting with someone at {name}'s end. Who are they, what was discussed, and what is the next step?"

  elif lead_stage.lower() == "junk":
      trigger = "junk_no_reason"
      question = f"Why was {name} marked junk? Bad contact info or genuinely not a buyer?"

# High-value junk flag — ALWAYS check regardless of other triggers
# If stage = Junk AND original_requirement suggests bulk/high-value order
if lead_stage.lower() == "junk" and original_requirement:
    req_lower = original_requirement.lower()
    bulk_signals = re.search(r'\d+\s*(piece|pcs|unit|nos)|probable order value|thaan|bulk', req_lower)
    if bulk_signals:
        trigger = "high_value_junk_flag"
        question = (
            f"IMPORTANT: {name} from {city} was marked junk as '{note}' but their "
            f"IndiaMart inquiry was for: '{original_requirement[:100]}'. "
            f"Are you sure this is junk? What did they actually say when you spoke to them?"
        )

Also check stale leads:
  SELECT * FROM pratibha_leads
  WHERE export_date < current_date
    AND lead_stage IN ('Yet To Talk', 'Followup')
    AND mobile_number NOT IN (
      SELECT mobile_number FROM pratibha_leads WHERE export_date = current_date
    )
  → prepend these to queue with trigger "stale_lead"
  → question: "This lead from {original_date} is still '{stage}' after {N} days. What's blocking contact?"
```

---

## Step 2 — `pratibha-agent/tools.py`

### Purpose
All 5 tools the LangGraph agent calls during conversation. Each tool is a Python function decorated with `@tool`.

### `get_question_queue`
```python
@tool
def get_question_queue(date: str) -> list[dict]:
    """
    Builds and returns the ordered question list for the given export date.
    Call this once at the start of the session.
    Returns slim dict only: {lead_id, contact_name, city, question}
    Full lead data stays in Postgres.
    """
    conn = get_db_conn()
    export_date = datetime.strptime(date, "%Y-%m-%d").date()
    queue = build_question_queue(export_date, conn)
    conn.close()
    # Slim before returning — don't send full lead row to model
    return [{"lead_id": q["lead_id"], "contact_name": q["contact_name"],
             "city": q["city"], "question": q["question"]} for q in queue]
```

### `get_next_question`
```python
@tool
def get_next_question(date: str, responses_saved: int) -> dict | None:
    """
    Returns the next unanswered question from the queue.
    Returns None when all questions have been answered.
    responses_saved: how many have been answered so far (used as offset).
    """
    conn = get_db_conn()
    export_date = datetime.strptime(date, "%Y-%m-%d").date()
    queue = build_question_queue(export_date, conn)
    conn.close()
    if responses_saved >= len(queue):
        return None
    q = queue[responses_saved]
    return {"lead_id": q["lead_id"], "contact_name": q["contact_name"],
            "city": q["city"], "question": q["question"]}
```

### `save_response`
```python
@tool
def save_response(lead_id: int, question: str, answer: str, date: str) -> bool:
    """
    Saves Pratibha's answer to pratibha_responses.
    Extracts structured fields via LLM: machine_sent, call_attempts, follow_up_plan.
    Also logs the Q&A to pratibha_conversations for the 6 PM summary.
    """
    # LLM extraction (mini prompt, not the full agent)
    extraction_prompt = f"""
    From this sales rep answer, extract:
    1. machine_sent: any machine model name mentioned (or null)
    2. call_attempts: number of call attempts mentioned (or null)
    3. follow_up_plan: any next step mentioned (or null)

    Answer: "{answer}"

    Return JSON only: {{"machine_sent": ..., "call_attempts": ..., "follow_up_plan": ...}}
    """
    extracted = call_groq_mini(extraction_prompt)  # use smaller model for extraction

    conn = get_db_conn()
    cur = conn.cursor()

    # Save structured response
    cur.execute("""
        INSERT INTO pratibha_responses
          (export_date, lead_id, question, answer, machine_sent, call_attempts, follow_up_plan)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (date, lead_id, question, answer,
          extracted.get("machine_sent"), extracted.get("call_attempts"),
          extracted.get("follow_up_plan")))

    # Log to conversations
    cur.execute("""
        INSERT INTO pratibha_conversations (conv_date, role, content, lead_id)
        VALUES (%s, 'agent', %s, %s)
    """, (date, question, lead_id))
    cur.execute("""
        INSERT INTO pratibha_conversations (conv_date, role, content, lead_id)
        VALUES (%s, 'pratibha', %s, %s)
    """, (date, answer, lead_id))

    conn.commit()
    conn.close()
    return True
```

### `get_stale_leads`
```python
@tool
def get_stale_leads(days_back: int = 2) -> list[dict]:
    """
    Returns leads from previous days still in Yet To Talk or Followup.
    These are leads Pratibha has not progressed.
    """
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, contact_name, city, lead_stage, export_date, original_requirement
        FROM pratibha_leads
        WHERE export_date >= CURRENT_DATE - %s
          AND export_date < CURRENT_DATE
          AND lead_stage IN ('Yet To Talk', 'Followup')
        ORDER BY export_date ASC
    """, (days_back,))
    rows = cur.fetchall()
    conn.close()
    return [{"lead_id": r[0], "contact_name": r[1], "city": r[2],
             "lead_stage": r[3], "export_date": str(r[4]),
             "original_requirement": r[5]} for r in rows]
```

### `generate_digest`
```python
@tool
def generate_digest(date: str) -> dict:
    """
    Aggregates all pratibha_responses for the date.
    Writes structured counts + LLM summary to pratibha_digest.
    Called by agent when question queue is empty.
    """
    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT answer, machine_sent, follow_up_plan
        FROM pratibha_responses WHERE export_date = %s
    """, (date,))
    responses = cur.fetchall()

    total = len(responses)
    details_sent_models = [r[1] for r in responses if r[1]]
    pending = [r[2] for r in responses if r[2] and "follow" in (r[2] or "").lower()]

    # LLM summary paragraph
    summary_prompt = f"""
    Write a 2-3 sentence management summary of today's lead activity.
    Total leads reviewed: {total}
    Models sent: {details_sent_models}
    Pending follow-ups: {pending}
    Be factual and concise.
    """
    raw_summary = call_groq_mini(summary_prompt)

    cur.execute("""
        INSERT INTO pratibha_digest
          (digest_date, total_leads, details_sent, details_sent_models,
           pending, pending_reasons, raw_summary)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (digest_date) DO UPDATE SET
          total_leads = EXCLUDED.total_leads,
          raw_summary = EXCLUDED.raw_summary
    """, (date, total, len(details_sent_models), details_sent_models,
          len(pending), pending, raw_summary))

    conn.commit()
    conn.close()

    return {"total_leads": total, "details_sent": len(details_sent_models),
            "models_sent": details_sent_models, "raw_summary": raw_summary}
```

---

## Step 3 — `pratibha-agent/agent.py`

### Purpose
LangGraph ReAct agent. Manages the conversation state, routes messages, calls tools, maintains the rolling session summary.

### Key design decisions
- `pre_model_hook` keeps token count flat (~600/turn regardless of session length)
- `session_summary` in state gives the model cross-session pattern awareness
- `llm_input_messages` returned (not `messages`) to avoid checkpoint corruption
- Tool results are slimmed before reaching the model

### State definition
```python
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages

class PratibhaState(TypedDict):
    messages:         Annotated[list, add_messages]
    date:             str
    question_queue:   list[dict]
    current_question: dict
    responses_saved:  int
    digest_generated: bool
    session_summary:  str   # compact log, ~10 tokens/lead, never grows past ~200 tokens
```

### System prompt
```python
SYSTEM_PROMPT = """
You are an accountability assistant reviewing Pratibha's daily lead activity.

Your job:
1. Ask ONE question at a time about a specific lead
2. When she answers, save it and move to the next question
3. Always address leads by name and city — never ask generic questions
4. If she says "don't know", "will check", or "dekhunga" — log it, move on, do not nag
5. Tone: matter-of-fact colleague, not an interrogating manager
6. If she asks about machines, prices or specs — tell her to use the sales chatbot
7. Do NOT make up lead names, models, or numbers — always use what the tools return

You have a session summary showing what's been covered so far. Use it to notice patterns
and ask smarter follow-up questions when relevant. For example:
- If she has given "will check" for 3 leads in a row, push back gently
- If she sent details to two customers who asked for different machines, ask if she sent the right one
"""
```

### pre_model_hook
```python
def pre_model_hook(state: PratibhaState):
    from langchain_core.messages import SystemMessage, HumanMessage

    system = SystemMessage(content=SYSTEM_PROMPT)

    session_log = state.get("session_summary", "No leads covered yet today.")
    context = SystemMessage(content=f"Session so far:\n{session_log}")

    # Only the last thing Pratibha said — prior turns are already in session_summary
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
    )
    trimmed = [system, context]
    if last_human:
        trimmed.append(last_human)

    # Return llm_input_messages — input only, NOT stored in checkpoint
    return {"llm_input_messages": trimmed}
```

### Graph nodes
```python
def classify_input(state):
    # Classify last human message:
    # "answer" — responding to the current question
    # "free_text" — off-topic or conversational
    # "start" — first message, load queue
    ...

def answer_received(state):
    # Calls save_response() tool
    # Updates session_summary: appends one line
    #   "- {contact_name}, {city} — {brief summary of answer}"
    ...

def get_next_question_node(state):
    # Calls get_next_question() tool
    # If None → route to generate_digest
    # If question → update current_question in state, route to respond
    ...

def generate_digest_node(state):
    # Calls generate_digest() tool
    # Sets digest_generated = True
    # Returns {reply: "All done! Here's today's summary: ...", done: True}
    ...

def respond(state):
    # Formats and returns the next question to Pratibha
    ...

def respond_directly(state):
    # Free text response, no tool call
    ...
```

### Graph wiring
```python
from langgraph.graph import StateGraph, START, END

graph = StateGraph(PratibhaState)
graph.add_node("classify_input", classify_input)
graph.add_node("answer_received", answer_received)
graph.add_node("get_next_question", get_next_question_node)
graph.add_node("generate_digest", generate_digest_node)
graph.add_node("respond", respond)
graph.add_node("respond_directly", respond_directly)

graph.add_edge(START, "classify_input")
graph.add_conditional_edges("classify_input", route_classify, {
    "answer": "answer_received",
    "free_text": "respond_directly",
    "start": "get_next_question"
})
graph.add_edge("answer_received", "get_next_question")
graph.add_conditional_edges("get_next_question", route_next_question, {
    "has_question": "respond",
    "queue_empty": "generate_digest"
})
graph.add_edge("generate_digest", "respond")
graph.add_edge("respond", END)
graph.add_edge("respond_directly", END)

app = graph.compile(checkpointer=postgres_checkpointer)
```

### FastAPI endpoint
```python
@router.post("/chat")
async def chat(req: ChatRequest):
    config = {"configurable": {"thread_id": req.thread_id}}
    result = await app.ainvoke(
        {"messages": [HumanMessage(content=req.message)], "date": req.date},
        config=config
    )
    last_ai = result["messages"][-1]
    done = result.get("digest_generated", False)
    return {"reply": last_ai.content, "done": done}
```

---

## Step 4 — `pratibha-agent/scheduler.py`

### Purpose
Fires at exactly 6:00 PM IST every day to generate the daily summary file.
Runs inside the FastAPI process using APScheduler — no external cron or separate service.

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date
import logging

async def trigger_daily_summary():
    today = date.today().isoformat()
    logging.info(f"[Scheduler] Generating daily summary for {today}")
    try:
        path = generate_daily_summary(today)
        logging.info(f"[Scheduler] Summary saved to {path}")
    except Exception as e:
        logging.error(f"[Scheduler] Failed: {e}")

def start_scheduler():
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(
        func=trigger_daily_summary,
        trigger=CronTrigger(hour=18, minute=0, timezone="Asia/Kolkata"),
        id="daily_summary",
        replace_existing=True
    )
    scheduler.start()
    return scheduler
```

Call `start_scheduler()` inside FastAPI's `@app.on_event("startup")`.

---

## Step 5 — `pratibha-agent/summary_writer.py`

### Purpose
Generates the daily `.md` summary file — two sections: management summary + detailed transcript.
Called by the scheduler at 6 PM and also when `generate_digest()` runs (end of conversation).

```python
def generate_daily_summary(date: str) -> str:
    """
    Pulls full conversation + digest from Postgres.
    Writes summary_YYYY-MM-DD.md to /app/summaries/ (volume-mounted to host).
    Returns the file path.
    """
    conn = get_db_conn()
    cur = conn.cursor()

    # Get digest
    cur.execute("SELECT * FROM pratibha_digest WHERE digest_date = %s", (date,))
    digest = cur.fetchone()

    # Get full conversation with lead details
    cur.execute("""
        SELECT pc.role, pc.content, pc.lead_id,
               pl.contact_name, pl.city, pl.lead_stage,
               pl.activity_note, pl.original_requirement,
               pr.question, pr.answer, pr.machine_sent,
               pr.call_attempts, pr.follow_up_plan
        FROM pratibha_conversations pc
        LEFT JOIN pratibha_leads pl ON pc.lead_id = pl.id
        LEFT JOIN pratibha_responses pr ON pr.lead_id = pl.id AND pr.export_date = %s
        WHERE pc.conv_date = %s
        ORDER BY pc.created_at ASC
    """, (date, date))
    rows = cur.fetchall()
    conn.close()

    # Build file content
    content = build_summary_markdown(date, digest, rows)

    path = f"/app/summaries/summary_{date}.md"
    with open(path, "w") as f:
        f.write(content)

    return path
```

### Markdown format
```
# Pratibha Daily Summary — {date}
Generated: {timestamp} IST

---

## Management Summary

{LLM-generated 2-3 sentence paragraph}

| Metric        | Count |
|---------------|-------|
| Total leads   | N     |
| Details sent  | N     |
| Marked junk   | N     |
| Pending       | N     |

Models quoted today: [list]
Junk reasons: [list]

---

## Detailed Transcript

### Lead 1 — {contact_name}, {city}
Original requirement: {original_requirement}
Cratio stage: {lead_stage}
Activity note: "{activity_note}"
Question asked: "{question}"
Pratibha's answer: "{answer}"
Extracted — Machine sent: {machine_sent}
Extracted — Call attempts: {call_attempts}
Extracted — Follow-up plan: {follow_up_plan}

### Lead 2 — ...

---

## Leads Not Discussed
{any leads in export not reached in conversation}
```

---

## Step 6 — `pratibha-agent/Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/summaries /app/uploads

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
```

### requirements.txt
```
fastapi
uvicorn
langgraph
langchain-core
langchain-groq
psycopg2-binary
pandas
apscheduler
python-multipart
httpx
```

---

## Step 7 — `docker-compose.yml` addition

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
    - C:/Users/ADMIN/Desktop/Pratibha Chatbot/summaries:/app/summaries
    - C:/Users/ADMIN/Desktop/Pratibha Chatbot/uploads:/app/uploads
  depends_on:
    - postgres
  extra_hosts:
    - "host.docker.internal:host-gateway"   # needed on Linux
```

Rebuild after any code change: `docker compose up -d --build pratibha-agent`
There is NO volume mount for the source code — it is COPIED at build time.

---

## Step 8 — `backend/server.js` additions

```javascript
const PRATIBHA_AGENT_URL = process.env.PRATIBHA_AGENT_URL || 'http://localhost:8001';
const multer = require('multer');
const path   = require('path');
const fs     = require('fs');

// --- CSV upload storage ---
const pratibhaStorage = multer.diskStorage({
  destination: (req, file, cb) => {
    const date = new Date().toISOString().split('T')[0];
    const dir  = path.join('C:/Users/ADMIN/Desktop/Pratibha Chatbot/uploads', date);
    fs.mkdirSync(dir, { recursive: true });
    cb(null, dir);
  },
  filename: (req, file, cb) => cb(null, file.originalname)
});
const pratibhaUpload = multer({ storage: pratibhaStorage });

// --- Routes ---

// Upload 3 CSVs → parse → load into Postgres → return question count
app.post('/api/pratibha/upload-export',
  pratibhaUpload.fields([
    { name: 'activities_file', maxCount: 1 },
    { name: 'sourcewise_file', maxCount: 1 },
    { name: 'active_file',     maxCount: 1 }
  ]),
  async (req, res) => {
    try {
      const files = req.files;
      const response = await fetch(`${PRATIBHA_AGENT_URL}/parse-exports`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          activities_path: files.activities_file[0].path,
          sourcewise_path: files.sourcewise_file[0].path,
          active_path:     files.active_file[0].path
        })
      });
      const data = await response.json();
      res.json(data);
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  }
);

// Proxy chat to Python agent
app.post('/api/pratibha/chat', async (req, res) => {
  try {
    const response = await fetch(`${PRATIBHA_AGENT_URL}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body)
    });
    const data = await response.json();
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Fetch digest for a date
app.get('/api/pratibha/digest', async (req, res) => {
  const date = req.query.date || new Date().toISOString().split('T')[0];
  const result = await pool.query(
    'SELECT * FROM pratibha_digest WHERE digest_date = $1', [date]
  );
  res.json(result.rows[0] || {});
});

// Fetch leads for a date
app.get('/api/pratibha/leads/:date', async (req, res) => {
  const result = await pool.query(
    `SELECT pl.*, pr.question, pr.answer, pr.machine_sent, pr.follow_up_plan
     FROM pratibha_leads pl
     LEFT JOIN pratibha_responses pr ON pr.lead_id = pl.id
     WHERE pl.export_date = $1
     ORDER BY pl.id ASC`,
    [req.params.date]
  );
  res.json(result.rows);
});

// Fetch saved summary .md file
app.get('/api/pratibha/summary/:date', (req, res) => {
  const filePath = path.join(
    'C:/Users/ADMIN/Desktop/Pratibha Chatbot/summaries',
    `summary_${req.params.date}.md`
  );
  if (!fs.existsSync(filePath)) return res.status(404).json({ error: 'Not found' });
  res.sendFile(filePath);
});
```

---

## Step 9 — `pratibha.html`

### Page structure
```html
<div id="main-page">

  <!-- Upload section -->
  <div id="upload-section">
    <h3>Upload today's Cratio exports</h3>
    <label>Activities report: <input type="file" id="activities-file" accept=".csv"></label>
    <label>Sourcewise report: <input type="file" id="sourcewise-file" accept=".csv"></label>
    <label>Active leads:      <input type="file" id="active-file"     accept=".csv"></label>
    <button onclick="uploadAndBegin()">Upload & Begin</button>
    <div id="upload-status"></div>
  </div>

  <!-- Chat section (hidden until upload complete) -->
  <div id="chat-section" style="display:none">
    <div id="date-label"></div>
    <div id="messages"></div>
    <input type="text" id="user-input" placeholder="Type your answer...">
    <button onclick="sendMessage()">Send</button>
  </div>

</div>
```

### JavaScript logic
```javascript
let threadId = null;
let exportDate = null;

async function uploadAndBegin() {
  const form = new FormData();
  form.append('activities_file', document.getElementById('activities-file').files[0]);
  form.append('sourcewise_file', document.getElementById('sourcewise-file').files[0]);
  form.append('active_file',     document.getElementById('active-file').files[0]);

  const res = await fetch('/api/pratibha/upload-export', { method: 'POST', body: form });
  const data = await res.json();

  exportDate = data.date;
  threadId = `pratibha-${exportDate}`;
  document.getElementById('date-label').textContent = `Reviewing: ${exportDate}`;
  document.getElementById('upload-section').style.display = 'none';
  document.getElementById('chat-section').style.display = 'block';

  // Auto-start conversation
  await sendMessage("start");
}

async function sendMessage(text = null) {
  const message = text || document.getElementById('user-input').value;
  if (!message.trim()) return;
  document.getElementById('user-input').value = '';

  appendMessage('pratibha', message);

  const res = await fetch('/api/pratibha/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, thread_id: threadId, date: exportDate })
  });
  const data = await res.json();

  appendMessage('agent', data.reply);

  if (data.done) {
    appendMessage('system', 'Session complete. Summary saved.');
    document.getElementById('user-input').disabled = true;
  }
}

function appendMessage(role, content) {
  const div = document.createElement('div');
  div.className = `message ${role}`;
  div.textContent = content;
  document.getElementById('messages').appendChild(div);
}
```

---

## Step 10 — Testing with 17 June Exports

```bash
# 1. Start all services
docker compose up -d --build

# 2. Verify Postgres tables created
docker exec -it postgres psql -U hca -d hca_agent -c "\dt pratibha*"

# 3. Open pratibha.html in browser

# 4. Upload the 3 June 17 CSVs:
#    - Lead_Activities_Details_Report_17june.csv
#    - Sourcewise_Lead_Detailed_Report_17june.csv
#    - Active_Leads_17june.csv

# 5. Verify question queue loads (should be ~15-20 questions)

# 6. Go through 3-4 questions, check:
#    - Questions reference correct lead names and cities
#    - "sent details" questions mention original requirement from Sourcewise report
#    - Answers save to pratibha_responses table

# 7. Check session_summary growing after each answer:
docker exec -it postgres psql -U hca -d hca_agent \
  -c "SELECT * FROM pratibha_responses WHERE export_date = '2026-06-17';"

# 8. Check conversations log:
docker exec -it postgres psql -U hca -d hca_agent \
  -c "SELECT role, content FROM pratibha_conversations WHERE conv_date = '2026-06-17';"

# 9. Trigger summary manually (don't wait for 6 PM):
curl -X POST http://localhost:8001/save-summary -H "Content-Type: application/json" \
  -d '{"date": "2026-06-17"}'

# 10. Verify summary_2026-06-17.md appears in Pratibha Chatbot/summaries/
```

---

## Known Constraints

1. **Cratio "Win" stage is unreliable** — never treat as confirmed conversion
2. **One export per day** — deduplicate by `mobile_number + export_date`
3. **Port 8001** — Pratibha agent only. HCA sales agent stays on 8000
4. **No volume mount for source code** — always rebuild: `docker compose up -d --build pratibha-agent`
5. **Summary at 6 PM is best-effort** — saves partial if conversation ongoing, re-saves at end
6. **Accounts cross-check deferred** — Phase 2 only, do not build yet
