# Pratibha Chatbot — System Architecture

## Service Map

```
┌─────────────────────────────────────────────────────────────┐
│                    USER'S BROWSER                           │
│                   pratibha.html                             │
│          [Upload CSVs]  →  [Chat interface]                 │
└───────────────────────┬─────────────────────────────────────┘
                        │ HTTP REST
                        ▼
┌─────────────────────────────────────────────────────────────┐
│           NODE.JS EXPRESS  (port 3001)                      │
│           THIN LAYER — routing + file handling only         │
│                                                             │
│  POST /api/pratibha/upload-export                           │
│  POST /api/pratibha/chat           → proxy only             │
│  GET  /api/pratibha/digest                                  │
│  GET  /api/pratibha/leads/:date                             │
│  GET  /api/pratibha/summary/:date                           │
└───────────────────────┬─────────────────────────────────────┘
                        │ HTTP (internal, port 8001)
                        ▼
┌─────────────────────────────────────────────────────────────┐
│        PYTHON FASTAPI  (port 8001)                          │
│        pratibha-agent Docker service                        │
│        FAT LAYER — all agent logic lives here               │
│                                                             │
│  POST /chat          → LangGraph ReAct agent                │
│  POST /parse-exports → CSV parser + Postgres loader         │
│  GET  /health                                               │
│                                                             │
│  APScheduler (runs inside this process)                     │
│  └── 6:00 PM IST → generate_daily_summary()                │
└──────────┬────────────────────────────┬─────────────────────┘
           │ psycopg2                   │ file write
           ▼                            ▼
┌──────────────────────┐   ┌────────────────────────────────┐
│  POSTGRES (port 5432)│   │  HOST FILE SYSTEM              │
│  shared with         │   │  Pratibha Chatbot/             │
│  HCA sales agent     │   │  ├── uploads/                  │
│                      │   │  │   └── YYYY-MM-DD/            │
│  pratibha_leads      │   │  │       ├── Activities.csv     │
│  pratibha_responses  │   │  │       ├── Sourcewise.csv     │
│  pratibha_digest     │   │  │       └── Active_Leads.csv   │
│  pratibha_           │   │  └── summaries/                 │
│    conversations     │   │      └── summary_YYYY-MM-DD.md  │
└──────────────────────┘   └────────────────────────────────┘
```

---

## Port Allocation

| Service | Port | Notes |
|---|---|---|
| Node.js Express | 3001 | Shared with HCA sales portfolio |
| HCA sales Python agent | 8000 | DO NOT touch |
| Pratibha Python agent | 8001 | New service |
| Postgres | 5432 | Shared, different tables |

---

## Data Flow — Upload

```
Pratibha opens pratibha.html
    │
    └── Uploads 3 CSVs via multipart form
            │
            ▼
    Node.js POST /api/pratibha/upload-export
            │
            ├── Saves files to:
            │   Pratibha Chatbot/uploads/YYYY-MM-DD/
            │     ├── Lead_Activities_Details_Report_[date].csv
            │     ├── Sourcewise_Lead_Detailed_Report_[date].csv
            │     └── Active_Leads_[date].csv
            │
            └── Calls Python agent POST /parse-exports
                        │
                        ▼
            csv_parser.parse_and_load_exports()
                        │
                        ├── Reads Activities CSV  → primary data
                        ├── Reads Sourcewise CSV  → original_requirement
                        ├── Reads Active Leads CSV → lead_score
                        ├── Merges on mobile_number
                        ├── Deduplicates (UPSERT on mobile + export_date)
                        └── Inserts into pratibha_leads table
                                    │
                                    ▼
                        build_question_queue()
                                    │
                                    ├── Fetches today's leads
                                    ├── Fetches stale leads (2 days back)
                                    ├── Applies trigger logic
                                    └── Returns ordered question list
                                                │
                                                ▼
                        Returns {status: "ready", question_count: N}
                                                │
                                                ▼
                        Browser auto-sends "start" → chat begins
```

---

## Data Flow — Chat Turn

```
Pratibha types answer
    │
    ▼
Browser POST /api/pratibha/chat
  {message, thread_id, date}
    │
    ▼
Node.js → proxies to Python agent POST /chat
    │
    ▼
LangGraph ReAct loop
    │
    ├── pre_model_hook() runs first
    │     ├── Builds: [SystemMessage, SessionSummaryContext, LastHumanMessage]
    │     ├── Does NOT pass full message history
    │     └── Returns {llm_input_messages: [...]}  ← input only, not stored
    │
    ├── Model classifies input
    │     ├── "answer" → save_response() → get_next_question() → ask it
    │     ├── "queue_empty" → generate_digest() → show summary, done: true
    │     └── "free_text" → respond directly, no tool call
    │
    ├── save_response() called on each answer
    │     ├── LLM extracts: machine_sent, call_attempts, follow_up_plan
    │     ├── Inserts into pratibha_responses
    │     ├── Appends to pratibha_conversations (full log)
    │     └── Updates session_summary in state (+1 line for this lead)
    │
    └── Returns {reply: "...", done: bool}
            │
            ▼
    Node.js returns to browser
            │
            ▼
    Browser renders reply
    If done: true → shows "Session complete. Summary saved."
```

---

## Data Flow — 6 PM IST Summary

```
APScheduler fires at 18:00 IST (12:30 UTC)
    │
    ▼
trigger_daily_summary(date=today)
    │
    ├── Fetches all pratibha_conversations for today (full Q&A log)
    ├── Fetches pratibha_digest for today (structured counts)
    │
    ├── LLM generates:
    │     ├── Detailed section: lead-by-lead transcript
    │     │     (original requirement + Cratio note + question + Pratibha's answer + extracted fields)
    │     └── Summary section: 2-3 sentence management paragraph + counts table
    │
    ├── Writes summary_YYYY-MM-DD.md
    │     → /app/summaries/ inside container
    │     → volume-mounted to Pratibha Chatbot/summaries/ on host
    │
    ├── Updates pratibha_digest.raw_summary in Postgres
    │
    └── If conversation not done yet:
          marks partial: true, will re-run when conversation ends
```

---

## LangGraph Agent Graph

```
START
  │
  ▼
classify_input
  │
  ├── "answer"     ──────────────────────────────────────────┐
  │                                                          │
  ▼                                                          │
save_response                                               │
  │ (saves to DB, updates session_summary in state)         │
  ▼                                                          │
get_next_question                                           │
  │                                                          │
  ├── question exists ──► respond (ask next question)       │
  │                          │                              │
  └── None (queue empty) ──► generate_digest ──► respond   │
                                  │               (done)    │
                                  ▼                         │
                             pratibha_digest saved          │
                             summary_YYYY-MM-DD.md written  │
                                                            │
  ├── "free_text" ──────────────────────────────────────────┘
  │
  ▼
respond_directly (no tool call, just conversational reply)
  │
  ▼
END (loop back to START on next message)
```

---

## State Object

```python
class PratibhaState(TypedDict):
    messages:         Annotated[list, add_messages]  # full history in checkpoint
    date:             str          # export date being reviewed (e.g. "2026-06-17")
    question_queue:   list[dict]   # [{lead_id, contact_name, city, trigger, question}]
    current_question: dict         # question currently in flight
    responses_saved:  int          # how many answers saved so far
    digest_generated: bool         # True once generate_digest() has run
    session_summary:  str          # compact running log, ~200 tokens max
                                   # passed to model instead of full message history
```

**Critical:** `session_summary` is the anti-rate-limit mechanism. It grows by one line per lead answered (~10 tokens/lead). At 20 leads, it's ~200 tokens — flat, not exponential.

---

## Token Budget Per Turn

| Component | Approx tokens |
|---|---|
| System prompt | ~300 |
| Session summary (20 leads max) | ~200 |
| Current question + Pratibha's answer | ~100 |
| **Total per turn** | **~600** |

Compare to full history approach: turn 1 = 100 tokens, turn 20 = 2000+ tokens. Rolling summary keeps it flat.

---

## Postgres Tables — Relationship Map

```
pratibha_leads (one row per lead per day)
    │
    ├──── pratibha_responses (Pratibha's answers, FK → pratibha_leads.id)
    │
    └──── pratibha_conversations (full message log, FK → pratibha_leads.id nullable)

pratibha_digest (one row per day, aggregated from pratibha_responses)
```

---

## File System Layout (Host Machine)

```
Pratibha Chatbot/          ← this folder, on Desktop
    CLAUDE.md              ← main developer reference
    ARCHITECTURE.md        ← this file
    BUILDING_LOGIC.md      ← implementation guide
    uploads/
        2026-06-17/
            Lead_Activities_Details_Report_17june.csv
            Sourcewise_Lead_Detailed_Report_17june.csv
            Active_Leads_17june.csv
        2026-06-18/
            ...
    summaries/
        summary_2026-06-17.md
        summary_2026-06-18.md
        ...
```

```
main-portfolio/            ← HCA sales portfolio (SEPARATE project)
    pratibha-agent/        ← Docker service source code lives here
        Dockerfile
        agent.py
        tools.py
        csv_parser.py
        scheduler.py
        summary_writer.py
        requirements.txt
    backend/
        server.js          ← new /api/pratibha/* routes added here
    pratibha.html          ← Pratibha's chat UI
    docker-compose.yml     ← pratibha-agent service added here
```

---

## Docker Compose — Full Service Picture

```yaml
services:

  postgres:                    # shared — already exists
    image: postgres:15
    ports: ["5432:5432"]

  hca-agent:                   # HCA sales agent — already exists, DO NOT TOUCH
    build: ./python-agent
    ports: ["8000:8000"]

  pratibha-agent:              # NEW
    build: ./pratibha-agent
    ports: ["8001:8001"]
    environment:
      - DATABASE_URL=postgresql://hca:hca_secret@postgres:5432/hca_agent
      - GROQ_API_KEY=${GROQ_API_KEY}
      - API_BASE=http://host.docker.internal:3001
    volumes:
      - C:/Users/ADMIN/Desktop/Pratibha Chatbot/summaries:/app/summaries
      - C:/Users/ADMIN/Desktop/Pratibha Chatbot/uploads:/app/uploads
    depends_on:
      - postgres
```
