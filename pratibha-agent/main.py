import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langchain_core.messages import HumanMessage

from agent import build_graph
from csv_parser import parse_and_load_exports, extract_date_from_filename, build_question_queue, get_db_conn, ensure_tables
from scheduler import start_scheduler
from summary_writer import generate_daily_summary

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]

graph_app = None
scheduler = None
_pg_conn = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global graph_app, scheduler, _pg_conn
    _pg_conn = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    checkpointer = AsyncPostgresSaver(_pg_conn)
    await checkpointer.setup()
    # Run idempotent migrations on every startup so the pratibha_daily_board
    # view and all Migration #003 columns exist before the 6 PM scheduler fires,
    # even on days when no CSV has been uploaded yet.
    _sync_conn = get_db_conn()
    ensure_tables(_sync_conn)
    _sync_conn.close()
    graph_app = build_graph(checkpointer)
    logger.info("LangGraph agent ready")
    scheduler = start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown()
    if _pg_conn:
        await _pg_conn.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    thread_id: str
    date: str


class ParseRequest(BaseModel):
    activities_filename: str
    activities_content: str
    sourcewise_filename: str
    sourcewise_content: str
    active_filename: str
    active_content: str


class SummaryRequest(BaseModel):
    date: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parse-exports")
def parse_exports(req: ParseRequest):
    try:
        export_date = extract_date_from_filename(req.activities_filename)

        # Write the received content to this service's own local disk. The
        # caller (pratibha-backend) may be a completely separate machine, so
        # only file CONTENTS can be trusted — a path from the caller's disk
        # means nothing here.
        date_dir = os.path.join("/app/uploads", export_date.isoformat())
        os.makedirs(date_dir, exist_ok=True)
        activities_path = os.path.join(date_dir, req.activities_filename)
        sourcewise_path = os.path.join(date_dir, req.sourcewise_filename)
        active_path = os.path.join(date_dir, req.active_filename)
        with open(activities_path, "w", encoding="utf-8") as f:
            f.write(req.activities_content)
        with open(sourcewise_path, "w", encoding="utf-8") as f:
            f.write(req.sourcewise_content)
        with open(active_path, "w", encoding="utf-8") as f:
            f.write(req.active_content)

        count = parse_and_load_exports(
            activities_path, sourcewise_path, active_path, export_date,
        )
        conn = get_db_conn()
        queue = build_question_queue(export_date, conn)
        conn.close()
        return {
            "status": "ready",
            "date": export_date.isoformat(),
            "leads_loaded": count,
            "question_count": len(queue),
        }
    except Exception as e:
        logger.exception("parse-exports failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat")
async def chat(req: ChatRequest):
    if graph_app is None:
        raise HTTPException(status_code=503, detail="Agent not ready")
    try:
        config = {"configurable": {"thread_id": req.thread_id}}

        # FR-2: only initialize fresh state if no existing checkpoint. Previously
        # "start"/"begin" always wiped progress — closing the browser + reopening +
        # Upload (which auto-sends "start") erased everything. Check the
        # checkpointer first; if a session is in flight, just pass the new message
        # and let classify_input route to resume.
        if req.message.strip().lower() in ("start", "begin"):
            snapshot = await graph_app.aget_state(config)
            existing = snapshot.values if (snapshot and snapshot.values) else {}
            has_active = bool(existing.get("question_queue")) and not existing.get("digest_generated")
            if has_active:
                input_state = {"messages": [HumanMessage(content=req.message)]}
            else:
                input_state = {
                    "messages": [HumanMessage(content=req.message)],
                    "date": req.date,
                    "responses_saved": 0,
                    "digest_generated": False,
                    "session_summary": "",
                    "consecutive_vague": 0,
                    "question_queue": [],
                    "current_question": {},
                }
        else:
            input_state = {"messages": [HumanMessage(content=req.message)]}

        result = await graph_app.ainvoke(input_state, config=config)
        last_ai = next(
            (m for m in reversed(result["messages"])
             if hasattr(m, "content") and not isinstance(m, HumanMessage)),
            None,
        )
        reply = last_ai.content if last_ai else "..."
        done = result.get("digest_generated", False)
        return {"reply": reply, "done": done}
    except Exception as e:
        logger.exception("chat failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/save-summary")
def save_summary(req: SummaryRequest):
    try:
        path = generate_daily_summary(req.date)
        return {"status": "saved", "path": path}
    except Exception as e:
        logger.exception("save-summary failed")
        raise HTTPException(status_code=500, detail=str(e))
