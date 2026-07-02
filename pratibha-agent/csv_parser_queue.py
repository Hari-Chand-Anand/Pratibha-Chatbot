"""Question-queue builder. Reads from pratibha_customers (the lifecycle entity).
Split out of csv_parser.py to keep each file under the file-size limit."""

import os
import logging
from datetime import date

from csv_parser_legacy import (
    legacy_build_question_queue,
    classify_legacy_lead_with_priority,
)
from hard_junk import must_force_resurface, extract_pov_inr, extract_quantity

logger = logging.getLogger(__name__)

# Fresh-start cutoff (02 Jul 2026). Customers first seen before this date are
# never queued, regardless of touch/resurface state — they're not deleted,
# just permanently out of scope for the queue builder. Old rows in Postgres
# are untouched. Set DAY_ZERO_DATE="" in .env to disable this filter entirely
# and go back to surfacing the full backlog.
_day_zero_str = os.environ.get("DAY_ZERO_DATE", "").strip()
try:
    DAY_ZERO = date.fromisoformat(_day_zero_str) if _day_zero_str else None
except ValueError:
    logger.warning("DAY_ZERO_DATE=%r is not a valid YYYY-MM-DD date — filter disabled", _day_zero_str)
    DAY_ZERO = None


def build_question_queue(export_date: date, conn) -> list[dict]:
    """Per-customer priority: FR-7 returning > multi-inquiry > follow-up touch > first-touch.
    MEMORY_FIX_ENABLED=false reverts to the legacy per-day stale-lead path."""
    if os.environ.get("MEMORY_FIX_ENABLED", "true").lower() == "false":
        return legacy_build_question_queue(export_date, conn)

    cur = conn.cursor()
    # Migration #004: skip resurface_blocked customers (hard-junked or touch-4 consented).
    # Fresh-start filter: exclude anyone first seen before DAY_ZERO.
    day_zero_clause = ""
    main_params = [export_date]
    if DAY_ZERO is not None:
        day_zero_clause = "AND first_seen_date >= %s"
        main_params.append(DAY_ZERO)
    cur.execute(f"""
        SELECT mobile_number, contact_name, city, first_seen_date,
               lifecycle_status, touch_count, last_touch_date, next_touch_date,
               last_product, last_resolution_at, reopened_at
        FROM pratibha_customers
        WHERE lifecycle_status = 'active'
          AND COALESCE(resurface_blocked, FALSE) = FALSE
          AND next_touch_date IS NOT NULL
          AND next_touch_date <= %s
          {day_zero_clause}
        ORDER BY first_seen_date ASC, mobile_number ASC
    """, tuple(main_params))
    customer_rows = cur.fetchall()
    ccols = ["mobile_number", "contact_name", "city", "first_seen_date",
             "lifecycle_status", "touch_count", "last_touch_date", "next_touch_date",
             "last_product", "last_resolution_at", "reopened_at"]
    customers = [dict(zip(ccols, r)) for r in customer_rows]

    queue = []
    for c in customers:
        mobile = c["mobile_number"]

        cur.execute("""
            SELECT id, contact_name, city, lead_stage, activity_note,
                   original_requirement, last_activity_time, export_date
            FROM pratibha_leads WHERE mobile_number = %s
            ORDER BY export_date DESC, id DESC LIMIT 1
        """, (mobile,))
        lrow = cur.fetchone()
        latest_lead = {
            "id": lrow[0] if lrow else None,
            "contact_name": (lrow[1] if lrow else c["contact_name"]) or c["contact_name"],
            "city": (lrow[2] if lrow else c["city"]) or c["city"],
            "lead_stage": lrow[3] if lrow else "",
            "activity_note": lrow[4] if lrow else "",
            "original_requirement": lrow[5] if lrow else "",
            "last_activity_time": lrow[6] if lrow else None,
            "mobile_number": mobile,
        }

        cur.execute("""
            SELECT id, inquiry_text, inquired_on, first_seen_time
            FROM pratibha_customer_inquiries
            WHERE mobile_number = %s AND status = 'open'
            ORDER BY inquired_on ASC, id ASC
        """, (mobile,))
        inquiries = [{"id": r[0], "inquiry_text": r[1], "inquired_on": r[2],
                      "first_seen_time": r[3]}
                     for r in cur.fetchall()]

        cur.execute("""
            SELECT question, answer, machine_sent FROM pratibha_responses
            WHERE mobile_number = %s ORDER BY created_at DESC LIMIT 1
        """, (mobile,))
        lr = cur.fetchone()
        last_response = {
            "question": lr[0] if lr else "",
            "answer": lr[1] if lr else "",
            "machine_sent": lr[2] if lr else "",
        }

        item = _build_question_for_customer(c, latest_lead, inquiries, last_response, export_date)
        if item and item.get("question"):
            queue.append(item)

    # ── Force-resurface pass ────────────────────────────────────────────────
    # Fetch recently-resolved (junked/declined) customers whose ORIGINAL
    # requirement crosses a force-resurface threshold (₹1L+ POV, bulk qty,
    # or specific model). These leads were excluded by the active-only query
    # above, but must resurface once more for director review regardless of
    # Pratibha's junk decision. Prepended at the front of the queue.
    try:
        fr_clause = ""
        fr_params = [export_date]
        if DAY_ZERO is not None:
            fr_clause = "AND first_seen_date >= %s"
            fr_params.append(DAY_ZERO)
        cur.execute(f"""
            SELECT mobile_number, contact_name, city, first_seen_date,
                   lifecycle_status, touch_count, last_touch_date, next_touch_date,
                   last_product, last_resolution_at, reopened_at
            FROM pratibha_customers
            WHERE lifecycle_status IN ('auto_junked', 'declined')
              AND COALESCE(resurface_blocked, FALSE) = FALSE
              AND last_resolution_at >= %s::date - INTERVAL '7 days'
              {fr_clause}
            ORDER BY last_resolution_at DESC
        """, tuple(fr_params))
        resolved_rows = cur.fetchall()
        already_queued = {q.get("mobile_number") for q in queue}
        force_items = []

        for r in resolved_rows:
            c = dict(zip(ccols, r))
            mobile = c["mobile_number"]
            if mobile in already_queued:
                continue

            cur.execute("""
                SELECT id, contact_name, city, lead_stage, activity_note,
                       original_requirement, last_activity_time, export_date
                FROM pratibha_leads WHERE mobile_number = %s
                ORDER BY export_date DESC, id DESC LIMIT 1
            """, (mobile,))
            lrow = cur.fetchone()
            if not lrow:
                continue

            requirement = lrow[5] or c.get("last_product") or ""
            forced, reason = must_force_resurface(requirement)
            if not forced:
                continue

            pov = extract_pov_inr(requirement)
            qty = extract_quantity(requirement)
            parts = []
            if pov is not None and pov >= 100_000:
                parts.append(f"₹{pov/10_000_000:.1f} Crore" if pov >= 10_000_000 else f"₹{pov/100_000:.1f} lakh POV")
            if qty is not None and qty > 5:
                parts.append(f"{qty} pieces")
            if "specific_model_named" in reason and not parts:
                parts.append("specific model requested")
            value_str = " / ".join(parts) if parts else "high-value inquiry"

            name = lrow[1] or c["contact_name"]
            city_name = lrow[2] or c["city"]
            req_short = requirement[:100]
            force_items.append({
                "lead_id": lrow[0],
                "mobile_number": mobile,
                "contact_name": name,
                "city": city_name,
                "original_requirement": requirement[:120],
                "trigger": "high_value_junk_flag",
                "question": (
                    f"⚠ DIRECTOR FLAG — {name} from {city_name} was marked junk "
                    f"but their original inquiry had {value_str}. "
                    f"Requirement: \"{req_short}\". "
                    f"Are you sure this is junk, or should we re-engage?"
                ),
            })
            already_queued.add(mobile)
            logger.info("force-resurface: %s (%s) reason=%s", name, mobile, reason)

        # High-value junked leads go at the FRONT so they can't be missed.
        queue = force_items + queue
    except Exception as e:
        logger.warning("force-resurface pass failed (queue unaffected): %s", e)
    # ────────────────────────────────────────────────────────────────────────

    cur.close()
    return queue


def _fmt_cratio_ts(ts):
    """Format Cratio original timestamp as '24 Jun 03:47 PM' for the opener."""
    if not ts:
        return ""
    if hasattr(ts, "strftime"):
        return ts.strftime("%d %b %I:%M %p")
    return str(ts)


def _fmt_cratio_date(d):
    if not d:
        return ""
    if hasattr(d, "strftime"):
        return d.strftime("%d %b")
    return str(d)


def _build_question_for_customer(customer, latest_lead, inquiries, last_response, export_date):
    name = customer["contact_name"] or "this customer"
    city = customer["city"] or ""
    touch_n = (customer["touch_count"] or 0) + 1
    days = (export_date - customer["first_seen_date"]).days if customer["first_seen_date"] else 0

    # Migration #004: the resurface prefix must include the Cratio original
    # date/time so Pratibha can identify exactly which lead is being talked
    # about. Prefer first_seen_time from the earliest open inquiry (that's
    # the actual Cratio "Lead Date" field). Fallback to first_seen_date only.
    cratio_ts = None
    if inquiries:
        cratio_ts = min(
            (i["first_seen_time"] for i in inquiries if i.get("first_seen_time")),
            default=None,
        )
    cratio_stamp = _fmt_cratio_ts(cratio_ts) or _fmt_cratio_date(customer["first_seen_date"])

    if customer["touch_count"] and customer["touch_count"] >= 1:
        prefix = (
            f"(Touch {touch_n}/4 — Cratio {cratio_stamp} · Day {days} since first seen) "
        )
    else:
        prefix = ""

    # Migration #004 phase 2 — high-POV / bulk / specific-model banner. If the
    # ORIGINAL requirement crosses a force-resurface threshold, prepend a ⚠
    # value banner so Pratibha (and the director on the summary) can't miss
    # that this lead is worth real attention.
    requirement_text = (latest_lead.get("original_requirement") or ""
                        or customer.get("last_product", "")
                        or (inquiries[0]["inquiry_text"] if inquiries else ""))
    forced, reason = must_force_resurface(requirement_text)
    if forced:
        pov = extract_pov_inr(requirement_text)
        qty = extract_quantity(requirement_text)
        parts = []
        if pov is not None and pov >= 100_000:
            if pov >= 10_000_000:
                parts.append(f"₹{pov/10_000_000:.1f} Crore")
            else:
                parts.append(f"₹{pov/100_000:.1f} lakh")
        if qty is not None and qty > 5:
            parts.append(f"{qty} pieces")
        if "specific_model_named" in reason and not parts:
            parts.append("specific model asked")
        banner = "⚠ " + " · ".join(parts) + " — flagged for review. " if parts else ""
        prefix = banner + prefix

    base = {
        "lead_id": latest_lead["id"],
        "mobile_number": customer["mobile_number"],
        "contact_name": name,
        "city": city,
        "original_requirement": (customer["last_product"] or "")[:120],
    }

    # Touch-4 consent: this is the 4th (or beyond) session on the same lead
    # with no resolution. Replace the normal follow-up question with an explicit
    # "junk permanently or give me a plan" prompt so Pratibha has to make a
    # decision rather than letting the lead keep cycling forever.
    if (customer["touch_count"] or 0) >= 3:
        from hard_junk import touch_4_prompt
        q = touch_4_prompt(customer)
        return {**base, "trigger": "touch_4_consent", "question": prefix + q}

    if (customer["reopened_at"] and customer["last_resolution_at"]
            and customer["reopened_at"] > customer["last_resolution_at"]):
        prior_product = (f' Last note: "{last_response["answer"][:80]}".'
                         if last_response.get("answer") else "")
        new_inqs = ", ".join(i["inquiry_text"][:80] for i in inquiries) or customer["last_product"] or ""
        q = (f"Returning customer alert — {name} from {city}. "
             f"They were previously closed.{prior_product} "
             f"New inquiry today: {new_inqs}. What's the plan this time?")
        return {**base, "trigger": "returning_customer", "question": q}

    if len(inquiries) >= 2:
        lines = []
        for idx, inq in enumerate(inquiries, 1):
            d = inq["inquired_on"].strftime("%d %b") if inq["inquired_on"] else ""
            lines.append(f"  ({idx}) {inq['inquiry_text'][:100]} ({d})")
        last_note = (f' You said last time: "{last_response["answer"][:80]}".'
                     if last_response.get("answer") else "")
        q = (f"{prefix}{name} from {city} — this customer has {len(inquiries)} open inquiries:\n"
             + "\n".join(lines)
             + f"\nWhich one are you updating me on, or both?{last_note}")
        return {**base, "trigger": "multi_inquiry", "question": q}

    if customer["touch_count"] and customer["touch_count"] >= 1 and last_response.get("answer"):
        machine_sent = (last_response.get("machine_sent") or "").strip()
        if machine_sent:
            # Frame around what was actually sent, not what the customer originally asked for.
            # Customer may have asked for machine X but we sent Y because Y does the same job.
            q = (f"{prefix}{name} from {city}. "
                 f"You sent the {machine_sent} catalog last time — customer hadn't responded. "
                 f"Any update from them?")
        else:
            product = (customer["last_product"]
                       or (inquiries[0]["inquiry_text"] if inquiries else "")
                       or latest_lead.get("original_requirement", ""))
            product_part = (f" on {product[:80]}"
                            if product and len(product.strip()) > 5
                            and not product.lower().startswith("requirement for")
                            else "")
            q = (f"{prefix}{name} from {city}. "
                 f'Last time you said: "{last_response["answer"][:120]}". '
                 f"Any update{product_part}?")
        return {**base, "trigger": "followup_touch", "question": q}

    priority, item = classify_legacy_lead_with_priority(latest_lead)
    if priority >= 99:
        return None
    item.update(base)
    item["question"] = prefix + item["question"]
    return item
