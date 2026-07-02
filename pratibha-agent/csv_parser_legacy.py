"""Legacy first-touch trigger classifier and the pre-Memory-Fix queue builder.
Kept separate so the main csv_parser.py stays under the file-size limit and to make
the rollback path (MEMORY_FIX_ENABLED=false) obvious."""

import re
from datetime import date, datetime


def legacy_build_question_queue(export_date: date, conn) -> list[dict]:
    """Rollback path. Original per-day stale-lead behaviour from before Migration #002."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id, contact_name, company_name, city, lead_stage,
               activity_note, original_requirement, last_activity_time, mobile_number
        FROM pratibha_leads
        WHERE export_date = %s
        ORDER BY id ASC
    """, (export_date,))
    today_leads = cur.fetchall()
    cols = ["id", "contact_name", "company_name", "city", "lead_stage",
            "activity_note", "original_requirement", "last_activity_time", "mobile_number"]
    leads = [dict(zip(cols, row)) for row in today_leads]

    cur.execute("""
        SELECT id, contact_name, city, lead_stage, export_date, original_requirement
        FROM pratibha_leads
        WHERE export_date >= %s - INTERVAL '2 days'
          AND export_date < %s
          AND lead_stage IN ('Yet To Talk', 'Followup')
        ORDER BY export_date ASC
    """, (export_date, export_date))
    stale_rows = cur.fetchall()
    stale_leads = [
        {"id": r[0], "contact_name": r[1], "city": r[2], "lead_stage": r[3],
         "export_date": r[4], "original_requirement": r[5]}
        for r in stale_rows
        if r[1] not in [l["contact_name"] for l in leads]
    ]
    cur.close()

    queue = []
    for lead in stale_leads:
        days_old = (export_date - lead["export_date"]).days
        req = (lead["original_requirement"] or "")[:80]
        queue.append({
            "lead_id": lead["id"],
            "contact_name": lead["contact_name"],
            "city": lead["city"],
            "trigger": "stale_lead",
            "question": f"{lead['contact_name']} from {lead['city']} — still '{lead['lead_stage']}' since {lead['export_date'].strftime('%b %d')} ({days_old} day(s) ago). What's blocking contact?",
            "original_requirement": req,
        })

    classified = [classify_legacy_lead_with_priority(l) for l in leads]
    classified.sort(key=lambda x: x[0])
    for priority, item in classified:
        if priority < 99 and item["question"]:
            queue.append(item)
    return queue


def classify_legacy_lead_with_priority(lead) -> tuple[int, dict]:
    """First-touch trigger classifier. Returns (priority, question_dict).
    Lower priority number = higher importance in the queue. Used by both the new
    and the legacy queue builder."""
    name = lead["contact_name"] or "this customer"
    city = lead["city"] or ""
    note = (lead["activity_note"] or "").strip()
    note_lower = note.lower()
    stage = (lead["lead_stage"] or "").strip()
    req = (lead["original_requirement"] or "").strip()
    req_short = req[:80]

    base = {
        "lead_id": lead.get("id"),
        "contact_name": name,
        "city": city,
        "original_requirement": req_short,
    }

    if stage.lower() == "junk" and req:
        bulk = re.search(
            r'\d+\s*(piece|pcs|unit|nos|thaan)|probable order value|bulk',
            req.lower()
        )
        if bulk:
            return (0, {**base,
                "trigger": "high_value_junk_flag",
                "question": (
                    f"IMPORTANT: {name} from {city} was marked junk as '{note}' but their "
                    f"IndiaMart inquiry was for: '{req_short}'. "
                    f"Are you sure this is junk? What did they actually say when you spoke to them?"
                ),
            })

    if not note_lower:
        q = f"No activity logged for {name} from {city}"
        if req_short:
            q += f" who asked about {req_short}"
        q += ". Did you call them? What happened?"
        return (1, {**base, "trigger": "blank_note", "question": q})

    if "language issue" in note_lower:
        return (99, {**base, "trigger": "skip", "question": ""})

    if ("sent detail" in note_lower or "sent details" in note_lower) and "visit" in note_lower:
        return (2, {**base,
            "trigger": "sent_details_visit_planned",
            "question": f"For {name} — details sent and a visit is planned. Which model did you send details for? Is the visit confirmed?",
        })

    if "sent detail" in note_lower or "sent details" in note_lower:
        if req_short:
            q = f"For {name} — you sent details. Which machine/model did you send? The customer asked about {req_short}. Did you send that specifically? Have they responded?"
        else:
            q = f"For {name} — you sent details. Which machine/model did you send? What was the price? Have they responded?"
        return (3, {**base, "trigger": "sent_details", "question": q})

    if "not responding" in note_lower or "not respond" in note_lower or "not attend" in note_lower:
        return (4, {**base,
            "trigger": "not_responding",
            "question": f"For {name} — how many times have you tried calling? Will you try again or should we mark as junk?",
        })

    if "disconnected" in note_lower or "disconnect" in note_lower:
        q = f"For {name}"
        if req_short:
            q += f" who enquired about {req_short}"
        q += " — they disconnected. Will you follow up?"
        return (5, {**base, "trigger": "disconnected", "question": q})

    if "not required" in note_lower:
        return (6, {**base,
            "trigger": "not_required",
            "question": f"For {name} — what did they actually need? Any future potential or permanently junk?",
        })

    if "send to" in note_lower or "sent to" in note_lower:
        person_match = re.search(r'(?:send to|sent to)\s+(.+)', note_lower)
        person = person_match.group(1).strip().title() if person_match else "someone"
        return (7, {**base,
            "trigger": "forwarded_to_person",
            "question": f"For {name} — you forwarded this to {person}. Who is that? What happened with it? Are you still tracking this lead?",
        })

    if "call after" in note_lower:
        time_ref = re.sub(r'call after', '', note_lower).strip()
        return (7, {**base,
            "trigger": "callback_pending",
            "question": f"For {name} — you noted to call after {time_ref}. Did you call back? What was the outcome?",
        })

    if re.search(r'customer need|customer needs|customer want', note_lower):
        return (7, {**base,
            "trigger": "customer_described_need",
            "question": f"For {name} — customer described their need as '{note}'. Did you identify the right machine for this? Did you send them details?",
        })

    if re.search(r'\b\w+\s+sir\b', note_lower):
        return (8, {**base,
            "trigger": "person_mentioned",
            "question": f"You mentioned connecting with someone at {name}'s end. Who are they, what was discussed, and what is the next step?",
        })

    if stage.lower() == "junk":
        return (8, {**base,
            "trigger": "junk_no_reason",
            "question": f"Why was {name} marked junk? Bad contact info or genuinely not a buyer?",
        })

    if stage.lower() == "followup" and lead.get("last_activity_time"):
        from datetime import timezone
        last = lead["last_activity_time"]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if (now - last).days >= 1:
            return (9, {**base,
                "trigger": "followup_stale",
                "question": f"{name} (Followup) was last updated {(now - last).days} day(s) ago. What's the current status?",
            })

    return (99, {**base, "trigger": "no_trigger", "question": ""})
