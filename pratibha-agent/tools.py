"""LangGraph @tool functions: queue lookup, save_response, generate_digest.
Heavy data-quality logic is in tools_quality.py (extraction prompt, persistence,
completeness scoring). Memory-Fix lifecycle logic stays inline."""
import os
import json
import re
import logging
from datetime import datetime
from groq import Groq
from langchain_core.tools import tool
from csv_parser import get_db_conn, build_question_queue
from tools_quality import (
    EXTRACTION_PROMPT, build_response_row, persist_quality_fields,
    evaluate_quality, is_enabled as quality_enabled,
    extract_from_context,
)
from domain_glossary import apply_glossary

logger = logging.getLogger(__name__)
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

DROPOUT_ORDERED = ("ordered","placed order","bought","purchase done","payment received",
                   "order placed","order confirm","po received","advance received")
DROPOUT_DECLINED = ("not interested","declined","won't buy","wont buy","mana kar diya",
                    "nahi chahiye","no interest","refused","rejected")

# "I'll act on this lead tomorrow" signals. When detected, next_touch_date is
# set to today+1 instead of the default today+2 so the lead surfaces on the
# day Pratibha said she'd actually do something about it.
#
# Conservative list — only explicit "tomorrow" / "kal" phrasings. We do NOT
# include "later" or "baad mein" (too vague) — those keep the default +2 day
# cadence so the lead resurfaces once she's had a real chance to act.
NEXT_DAY_PATTERNS = (
    "tomorrow",
    "tom morning",
    "tomorrow morning",
    "tomorrow evening",
    "next day",
    "agle din",
    "kal call",
    "kal karunga",
    "kal karenge",
    "kal karta",
    "kal karenge",
    "kal phone",
    "kal baat",
    "call kal",
    " kal ",        # word-boundary "kal" — leading space prevents matching "skal" etc.
    # Cratio "yet to talk" = called once, retry tomorrow. Surface the lead
    # tomorrow's queue, not in 2 days.
    "yet to talk",
)


def _wants_next_day_touch(answer: str) -> bool:
    """True if Pratibha's answer explicitly says she'll act on this lead
    tomorrow. Used to set next_touch_date = +1 day instead of +2."""
    if not answer:
        return False
    # Pad with spaces so the " kal " boundary check works for end-of-string too
    a = " " + answer.lower() + " "
    return any(p in a for p in NEXT_DAY_PATTERNS)


def call_groq_mini(prompt: str):
    resp = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        temperature=0, max_tokens=512,
    )
    text = resp.choices[0].message.content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    logger.info("GROQ RAW: %s", text[:300])
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    logger.warning("GROQ JSON EXTRACT FAILED: %s", text[:200])
    return text


@tool
def get_question_queue(date: str) -> list[dict]:
    """Returns ordered question list for the given export date."""
    conn = get_db_conn()
    ed = datetime.strptime(date, "%Y-%m-%d").date()
    q = build_question_queue(ed, conn)
    conn.close()
    return [{"lead_id": x["lead_id"], "contact_name": x["contact_name"],
             "city": x["city"], "question": x["question"],
             "trigger": x.get("trigger","")} for x in q]


@tool
def get_next_question(date: str, responses_saved: int):
    """Returns next unanswered question, or None."""
    conn = get_db_conn()
    ed = datetime.strptime(date, "%Y-%m-%d").date()
    q = build_question_queue(ed, conn)
    conn.close()
    if responses_saved >= len(q):
        return None
    x = q[responses_saved]
    return {"lead_id": x["lead_id"], "contact_name": x["contact_name"],
            "city": x["city"], "question": x["question"],
            "trigger": x.get("trigger","")}


@tool
def save_response(lead_id: int, question: str, answer: str, date: str,
                  trigger: str = "", is_followup: bool = False,
                  prev_extracted: dict = None,
                  already_asked: list = None) -> dict:
    """Logs Pratibha's answer (C-2 sacrosanct), runs LLM extraction, persists
    Memory-Fix lifecycle updates AND Migration #003 quality fields. Returns
    summary line + missing-field follow-up question + the merged extracted
    dict so the agent can carry it forward across follow-ups for this lead.

    prev_extracted: fields already captured for this lead in earlier turns
                    (the per-lead accumulator). Glossary + this-turn LLM
                    extraction are merged ON TOP of these, so a field that
                    was answered in turn 1 is still considered satisfied
                    in turn 2's missing-field check.
    """
    # 1. Glossary pre-pass (deterministic phrase rules — see domain_glossary.py)
    glossary_fields, glossary_terminal = apply_glossary(answer)

    # 1b. Migration #004 phase 2 — context-aware extraction. Deterministic
    # field parsing based on the question that was asked. Runs FIRST because
    # it handles the bare "2", "36000 + gst", "dy 6800-ds" cases the LLM was
    # missing. Wins over the LLM for these clean matches.
    context_fields = extract_from_context(question, answer)

    # 2. LLM free-form extraction
    extraction_prompt = EXTRACTION_PROMPT.format(question=question, answer=answer)
    extracted = call_groq_mini(extraction_prompt)
    if isinstance(extracted, str):
        extracted = {}

    # 3. Priority: context-aware (deterministic) > glossary > LLM.
    #    Glossary already deterministic; both override the LLM.
    for k, v in glossary_fields.items():
        extracted[k] = v
    for k, v in context_fields.items():
        if v is not None:
            extracted[k] = v

    al = answer.lower()
    dropout = extracted.get("dropout_status")
    if dropout not in ("ordered", "declined"):
        if any(t in al for t in DROPOUT_ORDERED):
            dropout = "ordered"
        elif any(t in al for t in DROPOUT_DECLINED):
            dropout = "declined"
        else:
            dropout = None

    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("SELECT contact_name, city, mobile_number FROM pratibha_leads WHERE id = %s", (lead_id,))
    row = cur.fetchone()
    contact_name = row[0] if row else "unknown"
    city = row[1] if row else ""
    mobile = row[2] if row else None

    call_attempts = extracted.get("call_attempts")
    if call_attempts is not None:
        try:
            call_attempts = int(call_attempts)
        except (ValueError, TypeError):
            call_attempts = None

    cur.execute("""
        INSERT INTO pratibha_responses
            (export_date, lead_id, contact_name, mobile_number, question, answer,
             machine_sent, call_attempts, follow_up_plan)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (date, lead_id, contact_name, mobile, question, answer,
          extracted.get("machine_sent"), call_attempts, extracted.get("follow_up_plan")))
    response_id = cur.fetchone()[0]

    cur.execute("INSERT INTO pratibha_conversations (conv_date, role, content, lead_id) VALUES (%s,'agent',%s,%s)",
                (date, question, lead_id))
    cur.execute("INSERT INTO pratibha_conversations (conv_date, role, content, lead_id) VALUES (%s,'pratibha',%s,%s)",
                (date, answer, lead_id))
    conn.commit()

    # Migration #003 — data-quality fields + completeness + follow-up signal.
    # Per-lead accumulator: merge prev_extracted (fields captured in earlier
    # turns for this same lead) so missing-field check doesn't keep re-asking
    # things Pratibha already answered. This-turn extraction wins where it has
    # a non-null value; otherwise prev_extracted's value carries forward.
    merged_qrow = {}
    quality_score, quality_missing, quality_followup = None, [], ""
    asked_field = ""
    if quality_enabled():
        try:
            qrow = build_response_row(extracted)
            if prev_extracted:
                for k, prev_v in prev_extracted.items():
                    cur_v = qrow.get(k)
                    if (cur_v is None or cur_v == "") and prev_v is not None and prev_v != "":
                        qrow[k] = prev_v
            merged_qrow = dict(qrow)
            quality_score, quality_missing, quality_followup, asked_field = evaluate_quality(
                trigger or "", qrow, already_asked=already_asked or []
            )
            persist_quality_fields(cur, response_id, qrow, trigger or "", quality_score)
            conn.commit()
        except Exception as e:
            logger.exception("data-quality persist failed (response already logged): %s", e)
            conn.rollback()

    # Memory-Fix layer: touch counter, inquiry match, dropout/auto-junk.
    # touch_count increments ONCE per lead question, NOT on follow-up saves.
    # Follow-ups are extra field captures on the same touch — incrementing for
    # each one was what caused all 4 Jun-25 customers to hit auto-junk in one session.
    #
    # Cadence override: if Pratibha explicitly said "kal call karunga / will call
    # tomorrow", set next_touch_date = today + 1 so the lead resurfaces on the
    # day she said she'd actually act. Default stays +2 days when there's no
    # explicit signal.
    next_day_signal = (not is_followup) and _wants_next_day_touch(answer)
    next_touch_interval = "INTERVAL '1 day'" if next_day_signal else "INTERVAL '2 days'"
    if mobile:
        try:
            if not is_followup:
                cur.execute(f"""
                    UPDATE pratibha_customers
                    SET touch_count = touch_count + 1,
                        last_touch_date = %s,
                        next_touch_date = %s::date + {next_touch_interval},
                        updated_at = NOW()
                    WHERE mobile_number = %s
                    RETURNING touch_count, lifecycle_status
                """, (date, date, mobile))
            else:
                cur.execute("""
                    SELECT touch_count, lifecycle_status FROM pratibha_customers
                    WHERE mobile_number = %s
                """, (mobile,))
            r = cur.fetchone()
            new_touch_count = r[0] if r else 0
            cur_status = r[1] if r else 'active'

            if not is_followup:
                cur.execute("""
                    INSERT INTO pratibha_touches (mobile_number, touch_number, surfaced_on, outcome, response_id)
                    VALUES (%s, %s, %s, 'answered', %s)
                """, (mobile, new_touch_count, date, response_id))

            machine_sent = (extracted.get("machine_sent") or "").strip()
            inquiry_addressed = False
            if machine_sent:
                tokens = [t for t in re.split(r'[\s/,\-]+', machine_sent.upper()) if len(t) >= 2]
                cur.execute("SELECT id, inquiry_text FROM pratibha_customer_inquiries WHERE mobile_number=%s AND status='open'", (mobile,))
                for inq_id, inq_text in cur.fetchall():
                    upper = (inq_text or "").upper()
                    if any(tok in upper for tok in tokens):
                        cur.execute("""UPDATE pratibha_customer_inquiries
                                       SET status='addressed', addressed_at=NOW(),
                                           addressed_response_id=%s, addressed_by_model=%s
                                       WHERE id=%s""", (response_id, machine_sent, inq_id))
                        inquiry_addressed = True

            new_status = cur_status
            resolution_now = False
            if dropout == "ordered":
                new_status = "ordered"; resolution_now = True
            elif dropout == "declined":
                new_status = "declined"; resolution_now = True
            elif glossary_terminal:
                # Glossary-detected terminal answers ("junk call", "not a buyer",
                # "language barrier", "we don't sell these", "no requirement", ...)
                # close the lead exactly like a "declined" dropout. The lead is
                # removed from the active queue and will not resurface unless
                # FR-7 fires because of a fresh IndiaMART inquiry later.
                new_status = "declined"; resolution_now = True
            elif new_touch_count >= 4 and not inquiry_addressed and cur_status == 'active':
                new_status = "auto_junked"; resolution_now = True

            if resolution_now:
                cur.execute("UPDATE pratibha_customers SET lifecycle_status=%s, last_resolution_at=NOW(), updated_at=NOW() WHERE mobile_number=%s",
                            (new_status, mobile))
                cur.execute("UPDATE pratibha_customer_inquiries SET status='auto_closed' WHERE mobile_number=%s AND status='open'", (mobile,))

            cur.execute("""UPDATE pratibha_customers pc
                           SET last_product = sub.inquiry_text, updated_at = NOW()
                           FROM (SELECT inquiry_text FROM pratibha_customer_inquiries
                                 WHERE mobile_number = %s AND status = 'open'
                                 ORDER BY inquired_on DESC, created_at DESC LIMIT 1) sub
                           WHERE pc.mobile_number = %s""", (mobile, mobile))
            cur.execute("""UPDATE pratibha_customers SET last_product = NULL
                           WHERE mobile_number = %s
                             AND NOT EXISTS (SELECT 1 FROM pratibha_customer_inquiries
                                             WHERE mobile_number = %s AND status = 'open')""",
                        (mobile, mobile))
            conn.commit()
        except Exception as e:
            logger.exception("save_response memory-fix layer failed (response logged): %s", e)
            conn.rollback()

    cur.close()
    conn.close()

    summary_line = extracted.get("summary_line") or answer[:60]
    return {
        "ok": True,
        "summary_line": f"- {contact_name}, {city} — {summary_line}",
        "completeness_score": quality_score,
        "missing_fields": quality_missing,
        "quality_followup": quality_followup,
        # Migration #004 phase 2 — the specific field the follow-up will ask
        # about. Agent adds this to state.asked_fields so it's not re-asked.
        "asked_field": asked_field,
        # For the agent's per-lead accumulator. Pass this back on the next turn
        # as prev_extracted so already-captured fields aren't re-asked.
        "extracted_fields": merged_qrow,
        # Glossary-driven terminal flag; the agent uses this to advance the
        # lead immediately even if other required fields look "missing".
        "terminal": glossary_terminal,
    }


@tool
def generate_digest(date: str) -> dict:
    """Aggregates today's responses + auto-junked count. Writes pratibha_digest.
    Migration #003: also pulls numeric daily board (pratibha_daily_board view)."""
    conn = get_db_conn()
    cur = conn.cursor()
    # Per-LEAD aggregation (NOT per-response — follow-ups inflated the count).
    # Group rows by lead_id so 4 leads × 4 follow-ups = 4, not 16.
    cur.execute("""
        SELECT
          pr.lead_id,
          pl.lead_stage,
          BOOL_OR(COALESCE(NULLIF(pr.answer, ''), '') <> '')  AS was_contacted,
          ARRAY_REMOVE(ARRAY_AGG(DISTINCT pr.machine_sent), NULL) AS models,
          ARRAY_REMOVE(ARRAY_AGG(DISTINCT pr.follow_up_plan), NULL) AS plans
        FROM pratibha_responses pr
        JOIN pratibha_leads pl ON pl.id = pr.lead_id
        WHERE pr.export_date = %s
        GROUP BY pr.lead_id, pl.lead_stage
    """, (date,))
    lead_rows = cur.fetchall()

    total = len(lead_rows)
    contacted = sum(1 for r in lead_rows if r[2])
    junked = sum(1 for r in lead_rows if (r[1] or "").lower() == "junk")
    # Flatten and dedupe across leads. Filter to plausible model names
    # (drop garbage like "all the details", " catalog", etc.)
    _seen = set()
    details_sent_models = []
    for r in lead_rows:
        for m in (r[3] or []):
            if not m or not isinstance(m, str):
                continue
            clean = m.strip()
            if len(clean) < 2 or len(clean) > 40 or clean.lower() in _seen:
                continue
            _seen.add(clean.lower())
            details_sent_models.append(clean)
    pending_reasons = []
    for r in lead_rows:
        for p in (r[4] or []):
            if p and isinstance(p, str) and p.strip():
                pending_reasons.append(p.strip())

    cur.execute("""
        SELECT contact_name, last_product FROM pratibha_customers
        WHERE lifecycle_status='auto_junked' AND last_resolution_at::date = %s
    """, (date,))
    auto_junked = cur.fetchall()
    auto_junked_count = len(auto_junked)
    auto_junked_names = [f"{r[0]} ({r[1] or 'no product'})" for r in auto_junked]

    # Deterministic prose summary. The LLM previously hallucinated numbers
    # ("8 leads auto-junked" on Day 1 — impossible). Template instead.
    parts = [f"Pratibha reviewed {total} lead{'s' if total != 1 else ''} today."]
    if contacted:
        parts.append(f"Contacted {contacted}.")
    if details_sent_models:
        parts.append(f"Details sent on {len(details_sent_models)} model(s): {', '.join(details_sent_models)}.")
    if junked:
        parts.append(f"{junked} manually marked junk.")
    if auto_junked_count:
        parts.append(f"{auto_junked_count} auto-junked after 4 silent touches.")
    raw_summary = " ".join(parts)

    cur.execute("""
        INSERT INTO pratibha_digest
          (digest_date, total_leads, contacted, details_sent, details_sent_models,
           junked, pending, pending_reasons, raw_summary)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (digest_date) DO UPDATE SET
          total_leads = EXCLUDED.total_leads, contacted = EXCLUDED.contacted,
          details_sent = EXCLUDED.details_sent, details_sent_models = EXCLUDED.details_sent_models,
          junked = EXCLUDED.junked, pending = EXCLUDED.pending,
          pending_reasons = EXCLUDED.pending_reasons, raw_summary = EXCLUDED.raw_summary
    """, (date, total, contacted, len(details_sent_models), details_sent_models,
          junked + auto_junked_count, len(pending_reasons), pending_reasons[:10], raw_summary))
    conn.commit()

    # Migration #003 — read the numeric daily board for the owner report.
    board = {}
    try:
        cur.execute("""
            SELECT contacted, details_sent, quote_value_inr,
                   orders_today, declined_today, auto_junked_today, avg_completeness
            FROM pratibha_daily_board WHERE report_date = %s
        """, (date,))
        brow = cur.fetchone()
        if brow:
            board = {
                "contacted": brow[0], "details_sent": brow[1],
                "quote_value_inr": float(brow[2] or 0),
                "orders_today": brow[3], "declined_today": brow[4],
                "auto_junked_today": brow[5],
                "avg_completeness": float(brow[6] or 0),
            }
    except Exception as e:
        logger.warning("daily_board read failed: %s", e)

    cur.close()
    conn.close()
    return {
        "total_leads": total, "contacted": contacted,
        "details_sent": len(details_sent_models), "models_sent": details_sent_models,
        "junked": junked, "auto_junked": auto_junked_count,
        "auto_junked_names": auto_junked_names, "raw_summary": raw_summary,
        "board": board,
    }
