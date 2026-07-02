"""Diagnostic — run inside the pratibha-agent container.
Usage from host (PowerShell, in 'Pratibha Chatbot' folder):
    docker compose exec pratibha-agent python /app/diagnose.py 2026-06-25

The script answers four questions used to root-cause today's chatbot issues:
1. Does pratibha_responses have the Migration #003 quality columns?
2. For the given date: distinct lead count + completeness_score + trigger_type distribution.
3. auto_junked count from pratibha_customers for that date.
4. pratibha_leads count for the date (what the queue actually saw).

Outputs plain text — paste it back to the chat.
"""
import os
import sys
import psycopg2

date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-06-25"

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

print(f"=== Diagnostic for {date_str} ===\n")

# 1. Schema check
cur.execute("""
    SELECT column_name FROM information_schema.columns
    WHERE table_name='pratibha_responses'
    ORDER BY ordinal_position
""")
cols = [r[0] for r in cur.fetchall()]
print("Q1 — pratibha_responses columns:")
print("  ", ", ".join(cols))
expected = ["price_quoted_inr", "customer_response_status", "trigger_type", "completeness_score"]
missing = [c for c in expected if c not in cols]
print(f"  Migration #003 columns missing: {missing or 'NONE — schema is up to date'}\n")

# 2. Today's responses
cur.execute("""
    SELECT COUNT(*) FROM pratibha_responses WHERE export_date = %s
""", (date_str,))
total_rows = cur.fetchone()[0]

cur.execute("""
    SELECT COUNT(DISTINCT lead_id) FROM pratibha_responses WHERE export_date = %s
""", (date_str,))
distinct_leads = cur.fetchone()[0]

print(f"Q2 — pratibha_responses for {date_str}:")
print(f"  total rows (including follow-ups): {total_rows}")
print(f"  distinct lead_id count:            {distinct_leads}")

if "completeness_score" in cols:
    cur.execute("""
        SELECT completeness_score, COUNT(*)
        FROM pratibha_responses WHERE export_date = %s
        GROUP BY completeness_score ORDER BY completeness_score NULLS FIRST
    """, (date_str,))
    rows = cur.fetchall()
    print("  completeness_score distribution:")
    for score, cnt in rows:
        print(f"    score={score}: {cnt} rows")

if "trigger_type" in cols:
    cur.execute("""
        SELECT trigger_type, COUNT(*)
        FROM pratibha_responses WHERE export_date = %s
        GROUP BY trigger_type ORDER BY COUNT(*) DESC
    """, (date_str,))
    rows = cur.fetchall()
    print("  trigger_type distribution:")
    for trig, cnt in rows:
        print(f"    {trig or '(NULL)'}: {cnt}")

cur.execute("""
    SELECT pl.contact_name, pl.city, pl.activity_note,
           COUNT(pr.id) AS response_count
    FROM pratibha_responses pr
    JOIN pratibha_leads pl ON pl.id = pr.lead_id
    WHERE pr.export_date = %s
    GROUP BY pl.id, pl.contact_name, pl.city, pl.activity_note
    ORDER BY response_count DESC
""", (date_str,))
print("\n  per-lead response count (high = many follow-ups):")
for name, city, note, n in cur.fetchall():
    note_short = (note or "")[:40]
    print(f"    {n}x — {name}, {city} | note: {note_short!r}")
print()

# 3. Auto-junked
try:
    cur.execute("""
        SELECT contact_name, mobile_number, touch_count, last_resolution_at, last_product
        FROM pratibha_customers
        WHERE lifecycle_status='auto_junked' AND last_resolution_at::date = %s
        ORDER BY last_resolution_at
    """, (date_str,))
    aj = cur.fetchall()
    print(f"Q3 — auto-junked on {date_str}: {len(aj)}")
    for name, mob, tc, ts, prod in aj:
        print(f"    {name} ({mob}) — touch_count={tc} — product={prod}")
    print()
except Exception as e:
    print(f"Q3 — auto_junked query failed: {e}\n")

# 4. Queue source
cur.execute("""
    SELECT COUNT(*) FROM pratibha_leads WHERE export_date = %s
""", (date_str,))
leads_n = cur.fetchone()[0]
print(f"Q4 — pratibha_leads for {date_str}: {leads_n} rows")

# Active customers due that date (the actual queue source under memory-fix path)
cur.execute("""
    SELECT COUNT(*) FROM pratibha_customers
    WHERE lifecycle_status='active'
      AND next_touch_date IS NOT NULL
      AND next_touch_date <= %s
""", (date_str,))
queue_n = cur.fetchone()[0]
print(f"  active customers due on or before {date_str}: {queue_n}")
print("  (this is what build_question_queue actually loaded as the queue)")

cur.close()
conn.close()
