import os
from typing import Annotated, Optional
from typing_extensions import TypedDict
from datetime import datetime

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from tools import save_response, generate_digest
from csv_parser import get_db_conn, build_question_queue
from traces import write_trace
from required_fields import MAX_FOLLOWUPS  # hard cap of 2 follow-ups per lead (required_fields.py)

# Terminal answers — when Pratibha says any of these, the lead is done.
# We do NOT keep asking templated follow-ups about call_attempts / price / etc.
# Order matters slightly: longer phrases first so substrings don't shadow.
TERMINAL_ANSWER_PATTERNS = (
    # ---- wrong industry / not for us ----
    "not related to our garment",
    "not related to garment",
    "not garment industry",
    "we do not sell",
    "we don't sell",
    "we dont sell",
    "do not sell these",
    "dont sell these",
    "not our line",
    "not our product",
    "wrong industry",
    # ---- explicit junk verdicts ----
    "junk for us",
    "junk call",
    "marking junk",
    "marking as junk",
    "mark as junk",
    "marked junk",
    "marked as junk",
    "this is junk",
    "it is junk",
    # ---- not a buyer / no need ----
    "not a buyer",
    "no requirement",
    "not need any machine",
    "customer not need",
    "just checking",
    # ---- no-followup signals ----
    "no followup",
    "no follow up",
    "no follow-up",
    "wont be taken",
    "will not be taken",
    "won't be taken",
    "no further action",
    "permanently junk",
    # ---- communication blockers ----
    "language issue",
    "language barrier",
)


def is_terminal_answer(answer: str) -> bool:
    """True if Pratibha's answer means 'this lead is done, stop asking'."""
    if not answer:
        return False
    a = answer.lower()
    return any(p in a for p in TERMINAL_ANSWER_PATTERNS)

SYSTEM_PROMPT = """You are an accountability assistant reviewing Pratibha's daily lead activity.

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
- If she has given "will check" for 3 leads in a row, push back gently on the third
- If she sent details to two customers who asked for different machines, ask if she sent the right one
"""


class PratibhaState(TypedDict):
    messages: Annotated[list, add_messages]
    date: str
    question_queue: list[dict]
    current_question: dict
    responses_saved: int
    digest_generated: bool
    session_summary: str
    consecutive_vague: int   # count of consecutive vague answers — resets on a good answer
    nudge_shown: bool        # has the "vague answers" nudge been shown this session? (fires once)
    followup_pending: str    # non-empty = a follow-up question is waiting to be asked
    followup_count: int      # how many follow-ups asked on current lead (max MAX_FOLLOWUPS)
    # Per-lead extraction accumulator. Fields captured for the CURRENT lead
    # across turn-1 + follow-ups, so the missing-field check doesn't keep
    # re-asking things Pratibha already answered. Reset to {} when the agent
    # advances to the next lead.
    extracted_so_far: dict
    # Migration #004 phase 2 — per-lead set of fields the agent has ALREADY
    # asked a follow-up about on this lead. Prevents the 26-Jun loop where
    # the same missing field is re-asked because extraction failed. Reset to
    # [] when the agent advances to the next lead.
    asked_fields: list
    _route: Optional[str]
    _next_route: Optional[str]


def get_llm():
    return ChatGroq(
        model="llama-3.3-70b-versatile",
        api_key=os.environ["GROQ_API_KEY"],
        temperature=0.3,
    )


def pre_model_hook(state: PratibhaState):
    system = SystemMessage(content=SYSTEM_PROMPT)
    session_log = state.get("session_summary") or "No leads covered yet today."
    context = SystemMessage(content=f"Session so far:\n{session_log}")
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
    )
    trimmed = [system, context]
    if last_human:
        trimmed.append(last_human)
    return {"llm_input_messages": trimmed}


def classify_input(state: PratibhaState) -> dict:
    last = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
    )
    text = (last.content if last else "").strip().lower()
    greetings = ("start", "begin", "शुरू", "shuru", "hello", "hi", "hii", "namaste")

    # FR-2 — "hi" does NOT wipe progress. If a session is already in flight,
    # route to resume instead of reloading the queue from scratch.
    if text in greetings:
        if state.get("question_queue") and not state.get("digest_generated"):
            return {**state, "_route": "resume"}
        return {**state, "_route": "start"}

    if state.get("digest_generated"):
        return {**state, "_route": "done"}

    if state.get("current_question"):
        return {**state, "_route": "answer"}

    return {**state, "_route": "free_text"}


def resume_node(state: PratibhaState) -> dict:
    """FR-2 — produce a 'Continuing — N done, M to go' message and re-issue the current question."""
    queue = state.get("question_queue", []) or []
    responses_saved = state.get("responses_saved", 0) or 0
    remaining = max(0, len(queue) - responses_saved)
    current = state.get("current_question") or {}
    current_q = current.get("question", "")
    if remaining == 0 or not current_q:
        # Edge case — session was supposed to be in flight but queue is exhausted.
        return {
            **state,
            "messages": state["messages"] + [
                AIMessage(content="Continuing — but no questions left for today. Run generate_digest to wrap up.")
            ],
        }
    msg = (f"Continuing — {responses_saved} done, {remaining} to go.\n\n"
           f"{current_q}")
    return {
        **state,
        "messages": state["messages"] + [AIMessage(content=msg)],
    }


def route_classify(state: PratibhaState) -> str:
    return state.get("_route", "free_text")


def load_queue_node(state: PratibhaState) -> dict:
    conn = get_db_conn()
    export_date = datetime.strptime(state["date"], "%Y-%m-%d").date()
    queue = build_question_queue(export_date, conn)

    # How many leads has Pratibha already covered earlier today (prior sessions)?
    # We surface this in the opening so the count in the opening message can be
    # reconciled with the digest at the end — otherwise the user sees "4 leads"
    # at the start and "Reviewed: 16" at the end with no explanation.
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(DISTINCT lead_id) FROM pratibha_responses WHERE export_date = %s",
        (export_date,),
    )
    already_covered_today = cur.fetchone()[0] or 0
    cur.close()
    conn.close()

    # CRITICAL: include `trigger` so save_response can apply the data-quality
    # required-fields contract. Without this, the quality layer is silently
    # inert and the agent falls back to loose LLM-driven follow-ups.
    slim_queue = [
        {"lead_id": q["lead_id"], "contact_name": q["contact_name"],
         "city": q["city"], "question": q["question"],
         "trigger": q.get("trigger", ""),
         "mobile_number": q.get("mobile_number", "")}
        for q in queue
    ]

    if not slim_queue:
        return {
            **state,
            "question_queue": [],
            "current_question": {},
            "messages": state["messages"] + [
                AIMessage(content="No leads found for this date. Please check the uploaded files.")
            ],
            "_route": "queue_empty",
        }

    first = slim_queue[0]
    n = len(slim_queue)
    if already_covered_today:
        opener = (
            f"Got it — {n} lead{'s' if n != 1 else ''} queued for this session "
            f"({already_covered_today} already covered earlier today). Let's start."
        )
    else:
        opener = (
            f"Got it — {n} lead{'s' if n != 1 else ''} to review today. Let's start."
        )
    return {
        **state,
        "question_queue": slim_queue,
        "current_question": first,
        "responses_saved": 0,
        "consecutive_vague": 0,
        "followup_pending": "",
        "followup_count": 0,
        "extracted_so_far": {},
        "asked_fields": [],
        "session_summary": "No leads covered yet today.",
        "messages": state["messages"] + [
            AIMessage(content=f"{opener}\n\n{first['question']}")
        ],
    }


VAGUE_PATTERNS = ("will check", "dekhunga", "pata nahi", "don't know", "nahi pata",
                  "no idea", "check karenge", "dekhta hun", "dekhenge", "baad mein",
                  "bhool gaya", "yaad nahi", "abhi nahi", "baad dekhta")

# Clear short answers that should NEVER be treated as vague.
# Without this, "no" or "yes" (a perfectly clear answer to "did you call?")
# was tripping the "you've given 3 vague answers in a row" nudge.
CLEAR_SHORT_ANSWERS = {
    "yes", "no", "haan", "nahi", "nahin",
    "yep", "yeah", "nope", "y", "n",
    "junk", "done", "ok", "okay",
    "called", "not called", "didnt call", "didn't call",
}


def _is_vague(answer: str) -> bool:
    a = answer.strip().lower().rstrip(".!?")
    if not a:
        return True
    # Answers that START with a clear yes/no token are not vague even if short.
    # "no i didnt", "yes called him" all carry real signal.
    first = a.split()[0]
    if first in CLEAR_SHORT_ANSWERS:
        return False
    # Explicit "will check" / "don't know" phrases — always vague.
    if any(p in a for p in VAGUE_PATTERNS):
        return True
    # Clear short answers — never vague.
    if a in CLEAR_SHORT_ANSWERS:
        return False
    # Terminal answers ("junk", "we don't sell these") — clear, not vague.
    if is_terminal_answer(a):
        return False
    # Short answers WITHOUT a digit AND without a clear yes/no/junk root → vague.
    # Now we only trip the nudge when the answer is genuinely contentless.
    if len(a.split()) <= 3 and not any(c.isdigit() for c in a):
        # Already filtered out clear yes/no/junk above, so anything left here
        # (e.g. "kya", "what", "haan ji") is vague.
        return True
    return False


def _handle_touch_4_answer(state: PratibhaState, current: dict, answer: str) -> dict:
    """Handle Pratibha's reply to the touch-4 consent question.
    Routes to hard_junk.handle_touch_4_reply — does NOT go through save_response."""
    from hard_junk import handle_touch_4_reply, touch_4_prompt

    mobile = current.get("mobile_number")
    name = current.get("contact_name", "this customer")
    city = current.get("city", "")
    lead_id = current.get("lead_id")
    outcome = "ambiguous"

    if mobile:
        conn = get_db_conn()
        try:
            outcome = handle_touch_4_reply(conn, mobile, answer)
            # Log the consent exchange to the conversation table.
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO pratibha_conversations (conv_date, role, content, lead_id) VALUES (%s,'agent',%s,%s)",
                (state["date"], current.get("question", ""), lead_id),
            )
            cur.execute(
                "INSERT INTO pratibha_conversations (conv_date, role, content, lead_id) VALUES (%s,'pratibha',%s,%s)",
                (state["date"], answer, lead_id),
            )
            conn.commit()
            cur.close()
        except Exception:
            pass
        finally:
            conn.close()

    if outcome == "ambiguous":
        # Re-ask — keep the consent question pending, do not advance.
        clarification = "I need a clear answer — yes (permanently junk) or no + your plan for them."
        reask = touch_4_prompt({"contact_name": name, "city": city, "first_seen_date": None})
        return {
            **state,
            "followup_pending": f"{clarification}\n\n{reask}",
        }

    old_summary = state.get("session_summary") or ""
    new_line = f"- {name}, {city} — touch-4 consent: {outcome}"
    new_summary = (
        (old_summary + "\n" + new_line).strip()
        if old_summary and old_summary != "No leads covered yet today."
        else new_line
    )

    if outcome == "consented":
        reply = f"Got it — {name} marked as permanent junk. They won't resurface again."
    else:
        reply = (
            f"OK — {name} moved to high-effort tracking. "
            f"Won't be requeued but will appear in the weekly summary."
        )

    return {
        **state,
        "session_summary": new_summary,
        "followup_pending": "",
        "followup_count": 0,
        "extracted_so_far": {},
        "asked_fields": [],
        "responses_saved": state.get("responses_saved", 0) + 1,
        "messages": state["messages"] + [AIMessage(content=reply)],
    }


def answer_received_node(state: PratibhaState) -> dict:
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None
    )
    answer = last_human.content if last_human else ""
    current = state.get("current_question", {})

    if not current:
        return state

    # Touch-4 consent replies go through a separate handler — not save_response.
    if current.get("trigger") == "touch_4_consent":
        return _handle_touch_4_answer(state, current, answer)

    followup_count = state.get("followup_count", 0)

    # Label follow-up saves so the transcript is clear
    question_to_save = current.get("question", "")
    if followup_count > 0:
        question_to_save = f"[Follow-up {followup_count}] {question_to_save}"

    # Pass the trigger AND the per-lead extraction accumulator so save_response
    # merges this-turn extraction with what we've already captured for this lead.
    # This is what stops the bot re-asking model/price/etc. once she's answered
    # them in a previous turn for the same lead.
    prev_extracted = state.get("extracted_so_far") or {}
    prev_asked = state.get("asked_fields") or []
    result = save_response.invoke({
        "lead_id": current["lead_id"],
        "question": question_to_save,
        "answer": answer,
        "date": state["date"],
        "trigger": current.get("trigger", ""),
        "is_followup": followup_count > 0,
        "prev_extracted": prev_extracted,
        "already_asked": prev_asked,
    })

    # Pull the merged extraction back so the NEXT turn (if it's a follow-up
    # on this same lead) starts with everything we've captured so far.
    merged_extracted = result.get("extracted_fields") or prev_extracted

    # Migration #004: trace this turn for eval + monitoring. Best-effort; never
    # raises. Auto-flags (repeat_question, extraction_missed, resurface_missing_date,
    # high_pov_flag_missed, terminal_ignored) are populated by deterministic
    # checks in traces.py — the same code the offline eval harness runs.
    try:
        # Collect prior agent messages on this lead for the repeat-question check.
        prior_on_lead = []
        for m in state["messages"]:
            if isinstance(m, AIMessage):
                prior_on_lead.append(getattr(m, "content", ""))
        trace_input = {
            "current_question": current,
            "extracted_so_far": prev_extracted,
            "touch_count": (state.get("current_question") or {}).get("touch_count"),
            "session_summary": state.get("session_summary"),
            "responses_saved": state.get("responses_saved", 0),
            "prior_agent_messages_on_lead": prior_on_lead[-6:],  # bounded
        }
        write_trace(
            session_date=state["date"],
            thread_id=None,   # available in main.py; will be threaded through later
            lead_id=current.get("lead_id"),
            mobile_number=current.get("mobile_number"),
            turn_index=state.get("responses_saved", 0),
            trigger_type=current.get("trigger", ""),
            touch_count=(state.get("current_question") or {}).get("touch_count"),
            input_state=trace_input,
            llm_output=current.get("question", ""),
            user_reply=answer,
        )
    except Exception:
        pass  # tracing must never break the session

    old_summary = state.get("session_summary") or ""
    new_line = result.get("summary_line", "")
    new_summary = (old_summary + "\n" + new_line).strip() if (old_summary and old_summary != "No leads covered yet today.") else new_line

    # Track consecutive vague answers
    prev_vague = state.get("consecutive_vague", 0)
    new_vague = (prev_vague + 1) if _is_vague(answer) else 0

    # Migration #003 (revised 26 Jun): ONLY deterministic templated follow-ups.
    #
    # Previously we fell back to evaluate_answer() (an LLM judge) when the
    # quality layer didn't return a follow-up. That produced the loose,
    # repetitive loops seen in 25 Jun sessions:
    #   Pratibha: "yes called, not garment industry, marking junk"
    #   Bot:     "What was the outcome of your call with X?"  ← LLM nonsense
    #   Pratibha: "told him we don't sell these"
    #   Bot:     "Can you elaborate on what you told X?"      ← LLM nonsense
    #
    # New rule: trust the templates in required_fields.FOLLOWUP_QUESTIONS.
    # If the quality layer says "no more fields needed", move on. If it didn't
    # run at all (exception / feature flag off), also move on — never invent
    # follow-ups via LLM.
    #
    # Plus: if Pratibha's answer is terminal ("junk", "we don't sell these",
    # "not garment industry"), advance immediately regardless of missing fields.
    # No point asking "How many call attempts?" on a lead she just closed.
    followup_question = None
    # Terminal answer detection: either the agent-side pattern list OR the
    # glossary signaled this is a closed lead (e.g. "junk call", "language
    # barrier"). Glossary-terminal also flips lifecycle_status to declined
    # inside save_response, so the lead won't resurface.
    terminal = is_terminal_answer(answer) or bool(result.get("terminal"))
    if not terminal and followup_count < MAX_FOLLOWUPS:
        # quality_followup is "" when all required fields are satisfied OR when
        # the quality layer didn't run. Either way: don't follow up.
        followup_question = result.get("quality_followup") or None

    if followup_question:
        # Stay on same lead — don't advance responses_saved.
        # Carry the merged extraction + asked_fields set forward so the NEXT
        # save_response sees everything we've captured AND skips already-asked
        # fields (loop guard, Migration #004 phase 2).
        newly_asked = list(prev_asked)
        asked_field = result.get("asked_field") or ""
        if asked_field and asked_field not in newly_asked:
            newly_asked.append(asked_field)
        return {
            **state,
            "session_summary": new_summary,
            "consecutive_vague": new_vague,
            "followup_pending": followup_question,
            "followup_count": followup_count + 1,
            "extracted_so_far": merged_extracted,
            "asked_fields": newly_asked,
        }
    else:
        # Satisfied / terminal / hit limit — advance to next lead and RESET
        # the accumulators so the next lead starts clean.
        return {
            **state,
            "session_summary": new_summary,
            "consecutive_vague": new_vague,
            "followup_pending": "",
            "followup_count": 0,
            "extracted_so_far": {},
            "asked_fields": [],
            "responses_saved": state.get("responses_saved", 0) + 1,
        }


def get_next_question_node(state: PratibhaState) -> dict:
    # If a follow-up is pending, stay on the same lead with the follow-up question
    followup = state.get("followup_pending", "")
    if followup:
        current = state.get("current_question", {})
        return {
            **state,
            "current_question": {**current, "question": followup},
            "_next_route": "has_question",
        }

    responses_saved = state.get("responses_saved", 0)
    queue = state.get("question_queue", [])

    if responses_saved >= len(queue):
        return {**state, "_next_route": "queue_empty"}

    next_q = queue[responses_saved]
    return {
        **state,
        "current_question": next_q,
        "_next_route": "has_question",
    }


def route_next_question(state: PratibhaState) -> str:
    return state.get("_next_route", "has_question")


def respond_node(state: PratibhaState) -> dict:
    current = state.get("current_question", {})
    question_text = current.get("question", "")
    vague_streak = state.get("consecutive_vague", 0)
    nudge_shown = state.get("nudge_shown", False)

    # Show the vague-answer nudge ONCE per session, the first time the streak
    # hits 3. Previously it fired at every multiple of 3, producing repeated
    # blocks of the same scolding text in the same chat.
    extra_state = {}
    if vague_streak >= 3 and not nudge_shown:
        nudge = (
            f"(Heads up — your last {vague_streak} answers have been short. "
            f"Try to include a call count or a clear next step where possible.)\n\n"
        )
        question_text = nudge + question_text
        extra_state["nudge_shown"] = True

    return {
        **state,
        **extra_state,
        "messages": state["messages"] + [AIMessage(content=question_text)],
    }


def generate_digest_node(state: PratibhaState) -> dict:
    from summary_writer import write_chat_transcript
    try:
        write_chat_transcript(state["date"])
    except Exception:
        pass  # never block the session over a file-write failure

    result = generate_digest.invoke({"date": state["date"]})

    # Session vs day reconciliation. The digest reports the DAY total (all
    # sessions). responses_saved on state is THIS session only. If they differ,
    # we surface the breakdown so the count can't look hallucinated.
    session_count = state.get("responses_saved", 0) or 0
    day_total = result.get("total_leads", 0) or 0
    earlier_today = max(0, day_total - session_count)

    models_str = ", ".join(result.get("models_sent", [])) or "none"
    if earlier_today > 0:
        leads_line = (
            f"- Leads reviewed today: {day_total} "
            f"({session_count} this session + {earlier_today} earlier today)"
        )
    else:
        leads_line = f"- Leads reviewed today: {day_total}"

    summary_text = (
        f"All done for today!\n\n"
        f"**Summary:**\n"
        f"{leads_line}\n"
        f"- Contacted: {result['contacted']}\n"
        f"- Details sent: {result['details_sent']} (models: {models_str})\n"
   f"- Marked junk: {result['junked']}\n\n"
        f"{result['raw_summary']}\n\n"
        f"The daily summary will be saved at 6:00 PM IST."
    )

    return {
        **state,
        "digest_generated": True,
        "current_question": {},
        "messages": state["messages"] + [AIMessage(content=summary_text)],
    }


def respond_directly_node(state: PratibhaState) -> dict:
    # If no session is loaded yet, don't call the LLM — it will hallucinate lead names.
    if not state.get("question_queue"):
        return {
            **state,
            "messages": state["messages"] + [AIMessage(
                content="Please upload today's Cratio exports first to start the session."
            )],
        }
    llm = get_llm()
    hook = pre_model_hook(state)
    response = llm.invoke(hook["llm_input_messages"])
    return {
        **state,
        "messages": state["messages"] + [AIMessage(content=response.content)],
    }


def done_node(state: PratibhaState) -> dict:
    return {
        **state,
        "messages": state["messages"] + [
            AIMessage(content="The session is already complete. Check the daily summary for today's report.")
        ],
    }


def build_graph(checkpointer):
    graph = StateGraph(PratibhaState)

    graph.add_node("classify_input", classify_input)
    graph.add_node("load_queue", load_queue_node)
    graph.add_node("resume", resume_node)
    graph.add_node("answer_received", answer_received_node)
    graph.add_node("get_next_question", get_next_question_node)
    graph.add_node("generate_digest", generate_digest_node)
    graph.add_node("respond", respond_node)
    graph.add_node("respond_directly", respond_directly_node)
    graph.add_node("done", done_node)

    graph.add_edge(START, "classify_input")
    graph.add_conditional_edges("classify_input", route_classify, {
        "start": "load_queue",
        "resume": "resume",
        "answer": "answer_received",
        "free_text": "respond_directly",
        "done": "done",
    })
    graph.add_edge("load_queue", END)
    graph.add_edge("resume", END)
    graph.add_edge("answer_received", "get_next_question")
    graph.add_conditional_edges("get_next_question", route_next_question, {
        "has_question": "respond",
        "queue_empty": "generate_digest",
    })
    graph.add_edge("respond", END)
    graph.add_edge("generate_digest", END)
    graph.add_edge("respond_directly", END)
    graph.add_edge("done", END)

    return graph.compile(checkpointer=checkpointer)
