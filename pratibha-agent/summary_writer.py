"""Owner-facing daily report (Migration #003 numbers + Migration #004 Claude narrative).
Written to /app/summaries/summary_YYYY-MM-DD.md at 6:00 PM IST (with 10 AM backup).
Also writes summary_YYYY-MM-DD.html — narrative director view.

ARCHITECTURE SPLIT (Migration #004):
  - Deterministic counts (from Postgres) — quotes, orders, active pipeline, junk
    counts, week stats — computed by SQL. NEVER asked of any LLM.
  - Narrative synthesis (two-things-to-raise, gut-check phrasing) — Claude via
    Anthropic API. Falls back to Groq/Qwen if ANTHROPIC_API_KEY absent or call
    fails, then to a deterministic template if both LLMs are unavailable.

Reads pratibha_daily_board view + pratibha_customers + pratibha_responses."""
import os
import json
import logging
from datetime import datetime, timedelta, date as date_cls
from groq import Groq
from csv_parser import get_db_conn

logger = logging.getLogger(__name__)
_groq = Groq(api_key=os.environ["GROQ_API_KEY"])

# Claude client is optional — if the key is not set we fall back to Groq then template.
try:
    from anthropic import Anthropic
    _anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    _anthropic = Anthropic(api_key=_anthropic_key) if _anthropic_key else None
except Exception:  # library missing or init error — never crash boot
    _anthropic = None

SUMMARY_LLM_PREF = os.environ.get("SUMMARY_LLM", "claude").lower()   # 'claude' | 'groq' | 'template'
CLAUDE_MODEL = os.environ.get("CLAUDE_SUMMARY_MODEL", "claude-sonnet-5")


def _fmt_inr(n):
    """Indian number formatting in lakhs."""
    n = float(n or 0)
    if n >= 100000:
        return f"₹ {n/100000:.2f} L"
    if n >= 1000:
        return f"₹ {n/1000:.1f} k"
    return f"₹ {n:.0f}"


def _delta(today, yesterday):
    if today is None or yesterday is None:
        return "→"
    d = today - yesterday
    if d > 0:
        return f"▲ +{d}"
    if d < 0:
        return f"▼ {d}"
    return "→"


def generate_daily_summary(date: str) -> str:
    conn = get_db_conn()
    cur = conn.cursor()

    # Today's board
    cur.execute("""
        SELECT contacted, details_sent, quote_value_inr,
               orders_today, declined_today, auto_junked_today, avg_completeness
        FROM pratibha_daily_board WHERE report_date = %s
    """, (date,))
    row = cur.fetchone()
    today_b = {
        "contacted": row[0] if row else 0,
        "details_sent": row[1] if row else 0,
        "quote_value_inr": float(row[2] or 0) if row else 0.0,
        "orders_today": row[3] if row else 0,
        "declined_today": row[4] if row else 0,
        "auto_junked_today": row[5] if row else 0,
        "avg_completeness": float(row[6] or 0) if row else 0.0,
    }

    # Yesterday's board for deltas
    y_date = (datetime.strptime(date, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    cur.execute("""
        SELECT contacted, details_sent, orders_today, declined_today, auto_junked_today
        FROM pratibha_daily_board WHERE report_date = %s
    """, (y_date,))
    yrow = cur.fetchone()
    y_b = {
        "contacted": yrow[0] if yrow else None,
        "details_sent": yrow[1] if yrow else None,
        "orders_today": yrow[2] if yrow else None,
        "declined_today": yrow[3] if yrow else None,
        "auto_junked_today": yrow[4] if yrow else None,
    }

    # New leads today (count of new pratibha_customers with first_seen_date=today)
    cur.execute("SELECT COUNT(*) FROM pratibha_customers WHERE first_seen_date = %s", (date,))
    new_leads = cur.fetchone()[0]

    # Active pipeline — sum of latest quote per active customer
    cur.execute("""
        SELECT COUNT(*) AS active_count,
               COALESCE(SUM(latest_quote), 0) AS total_value
        FROM (
            SELECT pc.mobile_number,
                   (SELECT pr.price_quoted_inr
                    FROM pratibha_responses pr
                    WHERE pr.mobile_number = pc.mobile_number
                      AND pr.price_quoted_inr IS NOT NULL
                    ORDER BY pr.created_at DESC LIMIT 1) AS latest_quote
            FROM pratibha_customers pc
            WHERE pc.lifecycle_status = 'active'
        ) s
        WHERE latest_quote IS NOT NULL
    """)
    _active_row = cur.fetchone()
    active_count, active_value = (_active_row[0], _active_row[1]) if _active_row else (0, 0)

    # Today's conversions (orders)
    cur.execute("""
        SELECT pc.contact_name, pc.city, pc.last_product,
               (SELECT pr.price_quoted_inr FROM pratibha_responses pr
                WHERE pr.mobile_number = pc.mobile_number
                ORDER BY pr.created_at DESC LIMIT 1)
        FROM pratibha_customers pc
        WHERE pc.lifecycle_status = 'ordered'
          AND pc.last_resolution_at::date = %s
    """, (date,))
    conversions = cur.fetchall()
    order_value = sum(float(r[3] or 0) for r in conversions)

    # Today's losses
    cur.execute("""
        SELECT pc.contact_name, pc.city, pc.last_product,
               (SELECT pr.price_quoted_inr FROM pratibha_responses pr
                WHERE pr.mobile_number = pc.mobile_number
                ORDER BY pr.created_at DESC LIMIT 1)
        FROM pratibha_customers pc
        WHERE pc.lifecycle_status = 'declined'
          AND pc.last_resolution_at::date = %s
    """, (date,))
    losses = cur.fetchall()
    lost_value = sum(float(r[3] or 0) for r in losses)

    # Red flag — high-value junked (>5 pcs or >1L)
    cur.execute("""
        SELECT pc.contact_name, pc.city, pl.original_requirement, pl.activity_note
        FROM pratibha_customers pc
        JOIN pratibha_leads pl ON pl.mobile_number = pc.mobile_number
        WHERE pc.lifecycle_status IN ('auto_junked','declined')
          AND pc.last_resolution_at::date = %s
          AND (pl.original_requirement ILIKE '%%piece%%' OR pl.original_requirement ILIKE '%%pcs%%'
               OR pl.original_requirement ILIKE '%%bulk%%' OR pl.original_requirement ILIKE '%%lakh%%')
    """, (date,))
    high_value_junks = cur.fetchall()

    # Red flag — touch 3/4 with high-value quote (one more touch and auto-junks)
    cur.execute("""
        SELECT pc.contact_name, pc.city, pc.last_product,
               (SELECT pr.price_quoted_inr FROM pratibha_responses pr
                WHERE pr.mobile_number = pc.mobile_number
                ORDER BY pr.created_at DESC LIMIT 1)
        FROM pratibha_customers pc
        WHERE pc.lifecycle_status = 'active'
          AND pc.touch_count = 3
          AND pc.next_touch_date <= %s::date + INTERVAL '1 day'
    """, (date,))
    near_junk = cur.fetchall()

    # Active pipeline breakdown by customer_response_status
    cur.execute("""
        SELECT pr.customer_response_status,
               COALESCE(SUM(pr.price_quoted_inr), 0)
        FROM pratibha_responses pr
        JOIN pratibha_customers pc ON pc.mobile_number = pr.mobile_number
        WHERE pc.lifecycle_status = 'active' AND pr.price_quoted_inr IS NOT NULL
        GROUP BY pr.customer_response_status
    """)
    breakdown = dict(cur.fetchall())

    # Week-so-far stats
    monday = (datetime.strptime(date, "%Y-%m-%d").date() - timedelta(days=datetime.strptime(date,"%Y-%m-%d").weekday())).isoformat()
    cur.execute("""
        SELECT
          COUNT(DISTINCT pc.mobile_number) FILTER (WHERE pc.lifecycle_status='ordered'
             AND pc.last_resolution_at::date BETWEEN %s AND %s) AS orders_week,
          COALESCE(SUM(pr.price_quoted_inr) FILTER
            (WHERE pc.lifecycle_status='ordered'), 0) AS orders_week_value,
          AVG(pr.completeness_score) AS avg_completeness_week
        FROM pratibha_customers pc
        LEFT JOIN pratibha_responses pr ON pr.mobile_number = pc.mobile_number
        WHERE pc.first_seen_date >= %s
    """, (monday, date, monday))
    wrow = cur.fetchone()
    week = {
        "orders": wrow[0] if wrow else 0,
        "orders_value": float(wrow[1] or 0) if wrow else 0.0,
        "avg_completeness": float(wrow[2] or 0) if wrow else 0.0,
    }

    # Pratibha's per-day stats
    cur.execute("""
        SELECT COUNT(*), AVG(completeness_score)
        FROM pratibha_responses WHERE export_date = %s
    """, (date,))
    prow = cur.fetchone()
    p_total, p_avg_quality = (prow[0] or 0), float(prow[1] or 0)

    # Raw chat transcript for the day — full back-and-forth between agent and
    # Pratibha, grouped by lead. Appended at the end of the .md so the owner
    # can scroll past the summary numbers and see the actual conversation.
    cur.execute("""
        SELECT pc.role, pc.content, pc.created_at, pc.lead_id,
               pl.contact_name, pl.city
        FROM pratibha_conversations pc
        LEFT JOIN pratibha_leads pl ON pl.id = pc.lead_id
        WHERE pc.conv_date = %s
        ORDER BY pc.created_at ASC, pc.id ASC
    """, (date,))
    conversation_rows = cur.fetchall()

    # ── Narrative data: quotes with price, catalogue-only, junk, unreached, cold ──
    cur2 = get_db_conn().cursor()

    # Leads touched today (all pratibha_leads rows for this date)
    cur2.execute("""
        SELECT pl.id, pl.contact_name, pl.city, pl.lead_stage, pl.activity_note,
               pl.original_requirement,
               pr.machine_sent, pr.price_quoted_inr, pr.follow_up_plan, pr.answer
        FROM pratibha_leads pl
        LEFT JOIN pratibha_responses pr ON pr.lead_id = pl.id AND pr.export_date = pl.export_date
        WHERE pl.export_date = %s
        ORDER BY pl.id
    """, (date,))
    all_leads = cur2.fetchall()
    cur2.connection.close()

    # Classify each lead for narrative
    quoted_with_price = []   # (name, city, machine, price)
    catalogue_only = []      # (name, machine)
    junked = []              # (name, city, reason)
    unreached = []           # (name, attempts)
    cold = []                # (name, city) — no contact at all

    for _id, name, city, stage, note, req, machine, price, plan, answer in all_leads:
        note_l = (note or "").lower()
        answer_l = (answer or "").lower()
        if price and float(price) > 0:
            quoted_with_price.append((name, city, machine or req or "—", float(price)))
        elif machine and any(w in note_l + answer_l for w in ("sent", "share", "detail", "catalogue", "catalog")):
            catalogue_only.append((name, machine))
        elif stage and stage.lower() in ("junk", "junk lead"):
            reason = note or answer or "no reason logged"
            junked.append((name, city or "—", reason))
        elif note_l in ("", "yet to talk") or "not attend" in note_l or "not respond" in note_l \
                or "switched off" in note_l or "disconnected" in note_l or "no answer" in note_l:
            attempts = 1
            for w in ("2 attempt", "twice", "2 time", "3 attempt", "multiple"):
                if w in note_l or w in answer_l:
                    attempts = int(w[0]) if w[0].isdigit() else 2
            if note_l == "" or "have to call" in answer_l or "abhi call" in answer_l:
                cold.append((name, city or "—"))
            else:
                unreached.append((name, attempts))
        # else: other / already handled above

    total_leads = len(all_leads)
    total_quoted = len(quoted_with_price) + len(catalogue_only)
    total_money = sum(p for _, _, _, p in quoted_with_price)

    conn.close()

    # ── Markdown report (existing) ──
    content = _build_report(date, today_b, y_b, new_leads, active_count, active_value,
                            conversions, order_value, losses, lost_value,
                            high_value_junks, near_junk, breakdown, week,
                            p_total, p_avg_quality, conversation_rows)
    os.makedirs("/app/summaries", exist_ok=True)
    path = f"/app/summaries/summary_{date}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    # ── Narrative HTML report (director view) ──
    html_path = f"/app/summaries/summary_{date}.html"
    html = _build_narrative_html(
        date, total_leads, quoted_with_price, catalogue_only,
        junked, unreached, cold, total_money, all_leads
    )
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # ── Chat transcript (auto) — written every time a summary is generated
    # (6 PM primary, 10 AM backup, or a manual /save-summary call), same
    # moment as summary_{date}.md/.html above. Previously this file only
    # existed when someone manually reconstructed it by hand from Postgres —
    # this makes it automatic, using the same conversation_rows already
    # queried above, so it can never drift from the numbers report.
    chat_path = f"/app/summaries/chat_{date}.md"
    chat_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    chat_lines = [f"# Pratibha Chat Log — {date}", f"_Generated: {chat_ts}_", ""]
    chat_lines.extend(_format_conversation_log(conversation_rows))
    with open(chat_path, "w", encoding="utf-8") as f:
        f.write("\n".join(chat_lines))

    # ── Update index.json for dashboard ──
    _update_index(date, total_leads, total_money, len(quoted_with_price),
                  len(catalogue_only), len(junked), len(unreached), len(cold))

    return path


def write_chat_transcript(date: str) -> str:
    """Write chat_{date}.md immediately — called at end of conversation,
    not just at 6 PM. Returns the file path written."""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT pc.role, pc.content, pc.created_at, pc.lead_id,
               pl.contact_name, pl.city
        FROM pratibha_conversations pc
        LEFT JOIN pratibha_leads pl ON pl.id = pc.lead_id
        WHERE pc.conv_date = %s
        ORDER BY pc.created_at ASC, pc.id ASC
    """, (date,))
    rows = cur.fetchall()
    conn.close()

    os.makedirs("/app/summaries", exist_ok=True)
    chat_path = f"/app/summaries/chat_{date}.md"
    chat_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Pratibha Chat Log — {date}", f"_Generated: {chat_ts}_", ""]
    lines.extend(_format_conversation_log(rows))
    with open(chat_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info("Chat transcript written: %s", chat_path)
    return chat_path


def _format_conversation_log(rows):
    """Group rows by lead, render as a readable back-and-forth."""
    if not rows:
        return ["_No conversation logged for this date._", ""]
    L = []
    current_lead = object()  # sentinel
    for role, content, created_at, lead_id, contact_name, city in rows:
        # New lead header
        if lead_id != current_lead:
            current_lead = lead_id
            L.append("")
            if lead_id and contact_name:
                header = f"### {contact_name}" + (f", {city}" if city else "")
            elif lead_id:
                header = f"### Lead #{lead_id}"
            else:
                header = "### (general)"
            L.append(header)
        speaker = "**Agent**" if role == "agent" else "**Pratibha**"
        ts = created_at.strftime("%H:%M") if created_at else ""
        prefix = f"{speaker} ({ts}):" if ts else f"{speaker}:"
        # Indent multi-line content under the speaker line
        body = (content or "").strip().replace("\n", " ")
        L.append(f"- {prefix} {body}")
    L.append("")
    return L


def _build_report(date, t, y, new_leads, active_count, active_value, conversions,
                  order_value, losses, lost_value, hv_junks, near_junk, breakdown,
                  week, p_total, p_avg_quality, conversation_rows):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M IST")
    L = []
    L.append(f"# Pratibha — {date}")
    L.append(f"_Generated: {ts}_")
    L.append("")
    L.append("## PIPELINE TODAY")
    L.append("")
    L.append("| Metric | Today | vs yesterday |")
    L.append("|---|---|---|")
    L.append(f"| New leads in | {new_leads} | — |")
    L.append(f"| Contacted by Pratibha | {t['contacted']} | {_delta(t['contacted'], y.get('contacted'))} |")
    L.append(f"| Details sent | {t['details_sent']} | {_delta(t['details_sent'], y.get('details_sent'))} |")
    L.append(f"| Orders confirmed | {t['orders_today']} | {_delta(t['orders_today'], y.get('orders_today'))} |")
    L.append(f"| Declined | {t['declined_today']} | {_delta(t['declined_today'], y.get('declined_today'))} |")
    L.append(f"| Auto-junked (touch 4) | {t['auto_junked_today']} | {_delta(t['auto_junked_today'], y.get('auto_junked_today'))} |")
    L.append("")
    L.append("## MONEY MOVED TODAY")
    L.append("")
    L.append(f"- **Quotes with price:** {_fmt_inr(t['quote_value_inr'])} ({t['details_sent']} details sent)")
    L.append(f"- **Orders confirmed:** {_fmt_inr(order_value)} ({t['orders_today']} orders)")
    L.append(f"- **Lost (declined):** {_fmt_inr(lost_value)} ({t['declined_today']} customers)")
    L.append("")
    L.append("## ACTIVE PIPELINE")
    L.append("")
    L.append(f"- **Open quote value:** {_fmt_inr(active_value)} across {active_count} active customers")
    if breakdown:
        L.append("")
        for status, value in breakdown.items():
            label = (status or "no_response").replace("_", " ")
            L.append(f"  - {label}: {_fmt_inr(value)}")
    L.append("")
    if hv_junks or near_junk:
        L.append("## ⚠ RED FLAGS")
        L.append("")
        for name, city, req, note in hv_junks:
            L.append(f"- **{name}**, {city} — junked but inquiry was: \"{(req or '')[:80]}\". "
                     f"Pratibha said: \"{note or ''}\". Audit recommended.")
        for name, city, product, price in near_junk:
            L.append(f"- **{name}**, {city} — Touch 3/4, quote {_fmt_inr(float(price or 0))} "
                     f"({product or 'no product'}). One more silent touch and auto-junks.")
        L.append("")
    if conversions:
        L.append("## TODAY'S CONVERSIONS")
        L.append("")
        for name, city, product, price in conversions:
            L.append(f"- ✓ **{name}**, {city} → {product or 'unknown'}, {_fmt_inr(float(price or 0))}")
        L.append("")
    L.append("## WEEK SO FAR")
    L.append("")
    L.append(f"- Orders: **{week['orders']}** ({_fmt_inr(week['orders_value'])})")
    if week['avg_completeness']:
        L.append(f"- Data completeness (week avg): **{week['avg_completeness']:.1f} / 10**")
    L.append("")
    L.append("## PRATIBHA TODAY")
    L.append("")
    L.append(f"- Leads reviewed: {p_total}")
    L.append(f"- Data quality (avg completeness): **{p_avg_quality:.1f} / 10**")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## CONVERSATION LOG")
    L.append("")
    L.append("_Full back-and-forth between the agent and Pratibha, grouped by lead._")
    L.extend(_format_conversation_log(conversation_rows))
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# NARRATIVE HTML REPORT — director view
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_price(p):
    """₹56,000 style — comma-separated, no decimals."""
    return f"₹{int(p):,}"


def _build_pattern_prompt(date, all_leads, junked, unreached, cold,
                          quoted_with_price, catalogue_only) -> str:
    """Structured, high-context prompt used by both Claude and Groq paths.
    All facts (counts, names, prices) are pre-computed in Python — the LLM's
    only job is to spot the pattern and write ONE concrete recommendation."""
    lines = []
    for _id, name, city, stage, note, req, machine, price, plan, answer in all_leads[:40]:
        lines.append(
            f"- {name or '?'} ({city or '?'}): stage={stage}, "
            f"note='{(note or '')[:80]}', requirement='{(req or '')[:60]}', "
            f"machine_sent={machine}, price={price}, "
            f"pratibha_answer='{(answer or '')[:80]}'"
        )
    leads_text = "\n".join(lines) or "(no leads today)"
    quoted_text = ", ".join(f"{n} — {m} at ₹{int(p):,}"
                            for n, _, m, p in quoted_with_price[:6]) or "none"
    junk_text = ", ".join(f"{n} ({c}): {r[:40]}"
                          for n, c, r in junked[:8]) or "none"

    return f"""You are the HCA Company Brain reviewing Pratibha's sales activity for {date}.

TODAY'S LEADS:
{leads_text}

QUOTED WITH PRICE: {quoted_text}
JUNK REASONS: {junk_text}
UNREACHED (retry tomorrow): {len(unreached)}
COLD (never contacted): {len(cold)}
CATALOGUE-ONLY (no price yet): {len(catalogue_only)}

TASK — write EXACTLY 2 bullet points for the director. Format is strict:
• [Pattern observed]. [Why it matters, with a specific name/city/machine from above].
• [One concrete action or question to raise with Pratibha this week].

Rules:
- Reference real names, cities, machines, or numbers from the data above. No generic advice.
- Prefer patterns across multiple leads over one-off observations.
- If a high-POV lead was junked or ignored, flag it.
- If the same "will send" pattern repeats without model/price, flag it.
- Two bullets total. No preamble. No section headers. No filler."""


def _llm_patterns(date, all_leads, junked, unreached,
                  cold=None, quoted_with_price=None, catalogue_only=None):
    """Two-things-to-raise narrative.

    Order of attempts:
      1. Claude (Anthropic) if SUMMARY_LLM_PREF='claude' and client is initialised
      2. Groq/Qwen fallback
      3. Deterministic template fallback (never crashes the summary)
    """
    cold = cold or []
    quoted_with_price = quoted_with_price or []
    catalogue_only = catalogue_only or []

    prompt = _build_pattern_prompt(
        date, all_leads, junked, unreached, cold, quoted_with_price, catalogue_only
    )

    if SUMMARY_LLM_PREF == "claude" and _anthropic is not None:
        try:
            resp = _anthropic.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=350,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            ).strip()
            if text:
                logger.info("Narrative generated by Claude (%s)", CLAUDE_MODEL)
                return text
        except Exception as e:
            logger.warning("Claude narrative failed, falling back to Groq: %s", e)

    # Groq fallback
    try:
        resp = _groq.chat.completions.create(
            model="qwen/qwen3-32b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=300,
        )
        text = resp.choices[0].message.content.strip()
        logger.info("Narrative generated by Groq (fallback)")
        return text
    except Exception as e:
        logger.warning("Groq narrative also failed, using template: %s", e)

    # Deterministic template fallback — always writes something so the summary
    # file is never blank on a Claude+Groq outage.
    bits = []
    if junked and len(junked) >= 3:
        cities = ", ".join(sorted({c for _, c, _ in junked if c})[:3]) or "multiple cities"
        bits.append(
            f"• {len(junked)} leads closed as junk today ({cities}). "
            "Review whether all were genuinely non-buyers or whether early-stage "
            "leads are being disqualified too fast."
        )
    else:
        bits.append("• Junk volume was normal today — no pattern to flag.")
    if catalogue_only and not quoted_with_price:
        bits.append(
            f"• {len(catalogue_only)} catalogues went out with no firm price. "
            "Ask Pratibha which of these can be quoted this week."
        )
    else:
        bits.append(
            f"• {len(unreached) + len(cold)} leads roll into tomorrow's queue. "
            "Confirm retry cadence."
        )
    return "\n".join(bits)


def _build_narrative_html(date, total_leads, quoted_with_price, catalogue_only,
                           junked, unreached, cold, total_money, all_leads):
    """Self-contained HTML in the exact narrative style the director wants."""

    # Human-readable date
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        date_label = dt.strftime("%-d %B").lstrip("0")  # "26 June"
    except Exception:
        date_label = date

    # Money line
    if total_money >= 100000:
        money_label = f"~₹{total_money/100000:.1f} lakh + GST"
    elif total_money > 0:
        money_label = f"~₹{total_money:,.0f} + GST"
    else:
        money_label = "no firm quotes"

    # Gut-check numbers
    hit_rate = round(len(quoted_with_price) / total_leads * 100) if total_leads else 0
    junk_rate = round(len(junked) / total_leads * 100) if total_leads else 0
    unreached_rate = round(len(unreached) / total_leads * 100) if total_leads else 0
    tomorrow_count = len(unreached) + len(cold)

    # LLM patterns — Claude first, Groq fallback, template last-resort
    patterns_text = _llm_patterns(
        date, all_leads, junked, unreached,
        cold=cold, quoted_with_price=quoted_with_price, catalogue_only=catalogue_only,
    )
    # Convert plain bullets to <li> items
    pattern_items = ""
    for line in patterns_text.split("\n"):
        line = line.strip().lstrip("•").lstrip("-").lstrip("1234567890.").strip()
        if line:
            pattern_items += f"<li>{line}</li>\n"

    # Quoted with price table rows
    price_rows = ""
    for name, city, machine, price in quoted_with_price:
        price_rows += f"""
            <tr>
              <td>{name or '—'}</td>
              <td>{machine}</td>
              <td>{_fmt_price(price)} (+GST)</td>
            </tr>"""

    # Catalogue only list
    cat_items = ""
    for name, machine in catalogue_only:
        cat_items += f"<li><strong>{name}</strong> — {machine} catalogue</li>\n"

    # Unreached list
    unreached_items = ""
    for name, attempts in unreached:
        unreached_items += f"<li>{name} ({attempts} attempt{'s' if attempts > 1 else ''})</li>\n"

    # Junk list
    junk_items = ""
    for name, city, reason in junked:
        short_reason = (reason or "no reason")[:80]
        junk_items += f"<li><strong>{name}</strong> ({city}) — {short_reason}</li>\n"

    # Cold list
    cold_items = ""
    for name, city in cold:
        cold_items += f"<li>{name} ({city}) — first contact still owed</li>\n"

    # Pending chase section
    pending_block = ""
    if quoted_with_price or catalogue_only:
        all_pending = [n for n, _, _, _ in quoted_with_price] + [n for n, _ in catalogue_only]
        pending_note = f"All {len(all_pending)} customer{'s' if len(all_pending) > 1 else ''} haven't replied yet. These are the leads worth chasing." if len(all_pending) > 1 else "This customer hasn't replied yet."
        pending_block = f'<p class="pending-note">{pending_note}</p>'

    # Pre-compute inline sections — Python 3.11 forbids same-quote nesting in f-strings
    _j = len(junked)
    junk_section = (
        f'<p style="font-size:13px;font-weight:600;color:#555;margin-bottom:8px;">'
        f'{_j} lead{"s" if _j > 1 else ""} closed as junk</p>'
        f'<ul>{junk_items}</ul><br>'
    ) if junked else ""

    _u = len(unreached)
    unreached_section = (
        f'<p style="font-size:13px;font-weight:600;color:#555;margin-bottom:8px;">'
        f'{_u} lead{"s" if _u > 1 else ""} called but customer didn\'t connect — to retry tomorrow</p>'
        f'<ul>{unreached_items}</ul><br>'
    ) if unreached else ""

    _c = len(cold)
    cold_section = (
        f'<p style="font-size:13px;font-weight:600;color:#555;margin-bottom:8px;">'
        f'{_c} lead{"s" if _c > 1 else ""} genuinely not contacted yet — first contact pending</p>'
        f'<ul>{cold_items}</ul>'
    ) if cold else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pratibha's day — {date_label}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f5f4f0;
    color: #1a1a1a;
    padding: 40px 24px;
    max-width: 780px;
    margin: 0 auto;
    line-height: 1.65;
  }}
  .day-header {{
    margin-bottom: 28px;
  }}
  .day-header h1 {{
    font-size: 22px;
    font-weight: 700;
    color: #111;
  }}
  .day-header .subtitle {{
    font-size: 14px;
    color: #666;
    margin-top: 4px;
  }}
  .money-banner {{
    background: #1a1a1a;
    color: #fff;
    border-radius: 10px;
    padding: 18px 22px;
    margin-bottom: 28px;
    font-size: 16px;
  }}
  .money-banner strong {{
    font-size: 18px;
    color: #f0e06a;
  }}
  .section {{
    background: #fff;
    border-radius: 10px;
    padding: 20px 22px;
    margin-bottom: 18px;
    border: 1px solid #e8e6e0;
  }}
  .section h2 {{
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: #888;
    margin-bottom: 14px;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }}
  th {{
    text-align: left;
    font-weight: 600;
    color: #555;
    border-bottom: 1px solid #e0deda;
    padding-bottom: 8px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  td {{
    padding: 10px 0;
    border-bottom: 1px solid #f0ede8;
    vertical-align: top;
  }}
  td:last-child {{
    text-align: right;
    font-weight: 600;
    color: #1a7a3c;
  }}
  tr:last-child td {{ border-bottom: none; }}
  ul {{
    list-style: none;
    padding: 0;
    font-size: 14px;
  }}
  ul li {{
    padding: 5px 0;
    border-bottom: 1px solid #f5f3ef;
    color: #333;
  }}
  ul li:last-child {{ border-bottom: none; }}
  .pending-note {{
    font-size: 13px;
    color: #b05a00;
    background: #fff8ee;
    border-left: 3px solid #f5a623;
    padding: 8px 12px;
    border-radius: 0 6px 6px 0;
    margin-top: 14px;
  }}
  .gut-check {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 12px;
    margin-top: 4px;
  }}
  .stat-box {{
    background: #f8f7f3;
    border-radius: 8px;
    padding: 14px 12px;
    text-align: center;
    border: 1px solid #e8e5df;
  }}
  .stat-box .num {{
    font-size: 28px;
    font-weight: 700;
    color: #111;
    display: block;
  }}
  .stat-box .label {{
    font-size: 11px;
    color: #888;
    margin-top: 2px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .stat-box .sub {{
    font-size: 12px;
    color: #555;
    margin-top: 4px;
  }}
  .patterns-list {{
    list-style: disc;
    padding-left: 18px;
    font-size: 14px;
  }}
  .patterns-list li {{
    padding: 6px 0;
    color: #333;
    border-bottom: none;
  }}
  .tomorrow-note {{
    background: #eef4ff;
    border-left: 3px solid #4a7fcb;
    padding: 10px 14px;
    border-radius: 0 6px 6px 0;
    font-size: 13px;
    color: #234;
    margin-top: 4px;
  }}
  .empty-state {{
    color: #aaa;
    font-size: 13px;
    font-style: italic;
  }}
  .footer {{
    text-align: center;
    font-size: 11px;
    color: #bbb;
    margin-top: 32px;
  }}
</style>
</head>
<body>

<div class="day-header">
  <h1>Pratibha's day, {date_label} ({total_leads} leads reviewed)</h1>
  <div class="subtitle">Generated automatically when conversation ended</div>
</div>

<div class="money-banner">
  Money committed today: <strong>{money_label}</strong> in quotes sent.
</div>

{"" if not quoted_with_price else f'''
<div class="section">
  <h2>Where the money came from</h2>
  <table>
    <thead>
      <tr><th>Customer</th><th>What was quoted</th><th>Price</th></tr>
    </thead>
    <tbody>{price_rows}</tbody>
  </table>
  {pending_block if not catalogue_only else ""}
</div>
'''}

{"" if not catalogue_only else f'''
<div class="section">
  <h2>Catalogue sent — no firm price yet</h2>
  <ul>{cat_items}</ul>
  {pending_block}
</div>
'''}

<div class="section">
  <h2>How the rest of the day broke down</h2>

  {junk_section}

  {unreached_section}

  {cold_section}

  {"<p class='empty-state'>All leads accounted for above.</p>" if not junked and not unreached and not cold else ""}
</div>

<div class="section">
  <h2>Quick gut-check</h2>
  <div class="gut-check">
    <div class="stat-box">
      <span class="num">{hit_rate}%</span>
      <span class="label">Hit rate</span>
      <div class="sub">{len(quoted_with_price)} of {total_leads} reached "details sent"</div>
    </div>
    <div class="stat-box">
      <span class="num">{junk_rate}%</span>
      <span class="label">Closed as junk</span>
      <div class="sub">{len(junked)} of {total_leads} leads</div>
    </div>
    <div class="stat-box">
      <span class="num">{unreached_rate}%</span>
      <span class="label">Attempted, unreached</span>
      <div class="sub">{len(unreached)} retry tomorrow</div>
    </div>
  </div>
  <div class="tomorrow-note">
    Tomorrow's follow-up list: <strong>{tomorrow_count} leads</strong>
    ({len(unreached)} retries + {len(cold)} first contact{"s" if len(cold) > 1 else ""})
  </div>
</div>

<div class="section">
  <h2>Two things worth raising with the team</h2>
  <ul class="patterns-list">
    {pattern_items}
  </ul>
</div>

<div class="footer">HCA Company Brain · Pratibha Daily Report · {date}</div>

</body>
</html>"""


def _update_index(date, total_leads, total_money, quoted_count, cat_count, junk_count,
                  unreached_count, cold_count):
    """Append today's entry to summaries/index.json for the dashboard."""
    index_path = "/app/summaries/index.json"
    try:
        with open(index_path) as f:
            index = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        index = []

    # Remove existing entry for this date if re-running
    index = [e for e in index if e.get("date") != date]
    index.append({
        "date": date,
        "total_leads": total_leads,
        "total_money": total_money,
        "quoted_count": quoted_count,
        "catalogue_count": cat_count,
        "junk_count": junk_count,
        "unreached_count": unreached_count,
        "cold_count": cold_count,
        "html_file": f"summary_{date}.html",
    })
    index.sort(key=lambda e: e["date"], reverse=True)

    with open(index_path, "w") as f:
        json.dump(index, f, indent=2)

