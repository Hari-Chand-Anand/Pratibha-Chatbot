"""CSV parser + schema migration. The question-queue builder lives in csv_parser_queue.py
   and the legacy classifier lives in csv_parser_legacy.py — split to stay under the
   per-file size limit. Public surface (unchanged from before Memory Fix):
       get_db_conn, ensure_tables, extract_date_from_filename, clean_html,
       parse_and_load_exports, build_question_queue
   Migration #002 (24 Jun 2026) added customer-lifecycle + multi-inquiry tables."""
import re
import pandas as pd
import psycopg2
from datetime import date, datetime
from calendar import month_abbr
import os

from csv_parser_queue import build_question_queue  # noqa: F401

MONTH_MAP = {m.lower(): i for i, m in enumerate(month_abbr) if m}


def extract_date_from_filename(filename: str) -> date:
    pattern = r'(\d{1,2})(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(\d{2})?'
    m = re.search(pattern, filename.lower())
    if not m:
        return date.today()
    day = int(m.group(1))
    month = MONTH_MAP[m.group(2)]
    year = int("20" + m.group(3)) if m.group(3) else date.today().year
    try:
        return date(year, month, day)
    except ValueError:
        return date.today()


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<br\s*/?>', ' ', text, flags=re.IGNORECASE)
    text = text.replace('&amp;', '&').replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>')
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def get_db_conn():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def ensure_tables(conn):
    """Idempotent schema setup. Pure additive — no DROP, no destructive ALTER."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pratibha_leads (
            id SERIAL PRIMARY KEY,
            export_date DATE NOT NULL,
            contact_name TEXT, company_name TEXT,
            mobile_number TEXT, city TEXT,
            lead_stage TEXT, lead_source TEXT,
            original_requirement TEXT,
            last_activity_time TIMESTAMPTZ,
            activity_note TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(mobile_number, export_date)
        );
        CREATE TABLE IF NOT EXISTS pratibha_responses (
            id SERIAL PRIMARY KEY, export_date DATE NOT NULL,
            lead_id INTEGER REFERENCES pratibha_leads(id),
            contact_name TEXT, question TEXT, answer TEXT,
            machine_sent TEXT, call_attempts INTEGER, follow_up_plan TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS pratibha_digest (
            id SERIAL PRIMARY KEY, digest_date DATE NOT NULL UNIQUE,
            total_leads INTEGER, contacted INTEGER, details_sent INTEGER,
            details_sent_models TEXT[], junked INTEGER, junk_reasons TEXT[],
            pending INTEGER, pending_reasons TEXT[],
            raw_summary TEXT, partial BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS pratibha_conversations (
            id SERIAL PRIMARY KEY, conv_date DATE NOT NULL,
            role TEXT NOT NULL, content TEXT NOT NULL,
            lead_id INTEGER REFERENCES pratibha_leads(id),
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS pratibha_conv_date_idx ON pratibha_conversations(conv_date);

        CREATE TABLE IF NOT EXISTS pratibha_customers (
            mobile_number TEXT PRIMARY KEY,
            contact_name TEXT, city TEXT,
            first_seen_date DATE NOT NULL,
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            touch_count INTEGER NOT NULL DEFAULT 0,
            last_touch_date DATE, next_touch_date DATE,
            last_product TEXT,
            last_resolution_at TIMESTAMPTZ,
            reopened_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS pc_due_idx ON pratibha_customers(next_touch_date)
            WHERE lifecycle_status = 'active';

        CREATE TABLE IF NOT EXISTS pratibha_touches (
            id SERIAL PRIMARY KEY,
            mobile_number TEXT REFERENCES pratibha_customers(mobile_number),
            touch_number INTEGER NOT NULL, surfaced_on DATE NOT NULL,
            outcome TEXT, response_id INTEGER REFERENCES pratibha_responses(id),
            created_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS pratibha_customer_inquiries (
            id SERIAL PRIMARY KEY,
            mobile_number TEXT REFERENCES pratibha_customers(mobile_number),
            inquiry_text TEXT NOT NULL, inquired_on DATE NOT NULL,
            source_lead_id INTEGER REFERENCES pratibha_leads(id),
            status TEXT NOT NULL DEFAULT 'open',
            addressed_at TIMESTAMPTZ,
            addressed_response_id INTEGER REFERENCES pratibha_responses(id),
            addressed_by_model TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(mobile_number, inquiry_text, inquired_on)
        );
        CREATE INDEX IF NOT EXISTS pci_open_idx ON pratibha_customer_inquiries(mobile_number)
            WHERE status = 'open';

        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS mobile_number TEXT;

        -- Migration #003 (24 Jun 2026): data-quality fields for owner report + brain.
        -- All nullable, all additive. Rollback = ignore the columns; old code keeps working.
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS price_quoted_inr         NUMERIC(12,2);
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS customer_response_status TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS visit_date               DATE;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS next_action              TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS next_action_date         DATE;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS why_not_required         TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS future_potential         TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS actual_customer_response TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS junk_reason              TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS forwarded_to_name        TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS handoff_status           TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS callback_outcome         TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS trigger_type             TEXT;
        ALTER TABLE pratibha_responses ADD COLUMN IF NOT EXISTS completeness_score       INTEGER;

        -- ═══════════════════════════════════════════════════════════════════
        -- Migration #004 (01 Jul 2026) — Evaluation harness + hard-junk +
        -- summary status tracking + Cratio-timestamped resurface.
        -- All additive, all idempotent. See MIGRATIONS.md #004 for rationale.
        -- ═══════════════════════════════════════════════════════════════════

        -- 4a) Trace every LLM turn so eval can replay production failures.
        CREATE TABLE IF NOT EXISTS pratibha_agent_traces (
            id            BIGSERIAL PRIMARY KEY,
            session_date  DATE NOT NULL,
            thread_id     TEXT,
            lead_id       INTEGER,
            mobile_number TEXT,
            turn_index    INTEGER,
            trigger_type  TEXT,
            touch_count   INTEGER,
            input_state   JSONB NOT NULL,
            llm_output    TEXT,
            user_reply    TEXT,
            auto_flags    TEXT[] NOT NULL DEFAULT '{}',
            latency_ms    INTEGER,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS pat_session_date_idx ON pratibha_agent_traces(session_date);
        CREATE INDEX IF NOT EXISTS pat_flags_gin_idx    ON pratibha_agent_traces USING GIN (auto_flags);
        CREATE INDEX IF NOT EXISTS pat_mobile_idx       ON pratibha_agent_traces(mobile_number);

        -- 4b) Summary lifecycle tracking so the 6 PM + 10 AM windows can
        --     coordinate idempotently.  status ∈ (pending|partial|complete|failed)
        ALTER TABLE pratibha_digest ADD COLUMN IF NOT EXISTS status          TEXT DEFAULT 'pending';
        ALTER TABLE pratibha_digest ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
        ALTER TABLE pratibha_digest ADD COLUMN IF NOT EXISTS attempt_count   INTEGER DEFAULT 0;
        ALTER TABLE pratibha_digest ADD COLUMN IF NOT EXISTS failure_reason  TEXT;
        ALTER TABLE pratibha_digest ADD COLUMN IF NOT EXISTS generated_by    TEXT;    -- 'claude' | 'template' | 'groq'

        -- 4c) Hard-junk enforcement + touch-4 consent flag on the lifecycle entity.
        --     resurface_blocked=true → the queue builder skips this customer forever.
        ALTER TABLE pratibha_customers ADD COLUMN IF NOT EXISTS resurface_blocked BOOLEAN DEFAULT FALSE;
        ALTER TABLE pratibha_customers ADD COLUMN IF NOT EXISTS hard_junk_reason  TEXT;
        ALTER TABLE pratibha_customers ADD COLUMN IF NOT EXISTS hard_junked_at    TIMESTAMPTZ;

        -- 4d) Cratio original timestamp — needed for resurface openers so Pratibha
        --     recognises which lead is being talked about ("Cratio 24 Jun 03:47 PM").
        ALTER TABLE pratibha_customer_inquiries ADD COLUMN IF NOT EXISTS first_seen_time TIMESTAMPTZ;

        DROP VIEW IF EXISTS pratibha_daily_board;
        CREATE VIEW pratibha_daily_board AS
        SELECT
          pr.export_date AS report_date,
          COUNT(DISTINCT pr.lead_id) AS contacted,
          COUNT(DISTINCT pr.lead_id) FILTER (WHERE pr.machine_sent IS NOT NULL) AS details_sent,
          COALESCE(SUM(pr.price_quoted_inr)
                   FILTER (WHERE pr.price_quoted_inr IS NOT NULL), 0) AS quote_value_inr,
          COUNT(DISTINCT pc.mobile_number)
            FILTER (WHERE pc.lifecycle_status='ordered'
                    AND pc.last_resolution_at::date = pr.export_date) AS orders_today,
          COUNT(DISTINCT pc.mobile_number)
            FILTER (WHERE pc.lifecycle_status='declined'
                    AND pc.last_resolution_at::date = pr.export_date) AS declined_today,
          COUNT(DISTINCT pc.mobile_number)
            FILTER (WHERE pc.lifecycle_status='auto_junked'
                    AND pc.last_resolution_at::date = pr.export_date) AS auto_junked_today,
          ROUND(AVG(pr.completeness_score)::numeric, 1) AS avg_completeness
        FROM pratibha_responses pr
        LEFT JOIN pratibha_customers pc ON pc.mobile_number = pr.mobile_number
        GROUP BY pr.export_date;
    """)
    conn.commit()

    cur.execute("""
        UPDATE pratibha_responses pr SET mobile_number = pl.mobile_number
        FROM pratibha_leads pl
        WHERE pr.lead_id = pl.id AND pr.mobile_number IS NULL
    """)
    conn.commit()

    cur.execute("""
        INSERT INTO pratibha_customers
          (mobile_number, contact_name, city, first_seen_date, lifecycle_status,
           touch_count, last_touch_date, next_touch_date, last_product)
        SELECT pl.mobile_number, MAX(pl.contact_name), MAX(pl.city),
               MIN(pl.export_date), 'active', 0, NULL, NULL,
               MAX(pl.original_requirement)
        FROM pratibha_leads pl
        WHERE pl.mobile_number IS NOT NULL AND pl.mobile_number <> ''
        GROUP BY pl.mobile_number
        ON CONFLICT (mobile_number) DO NOTHING
    """)
    conn.commit()
    cur.close()


def parse_and_load_exports(activities_path: str, sourcewise_path: str, active_path: str, export_date: date) -> int:
    """Reads 3 CSVs, populates pratibha_leads, pratibha_customers,
       pratibha_customer_inquiries; runs FR-7 reopen + last_product recompute.
       Idempotent: re-uploading the same CSVs is a no-op."""
    conn = get_db_conn()
    ensure_tables(conn)

    df_act = pd.read_csv(activities_path, dtype=str, index_col=False).fillna("")
    df_act.columns = [c.strip() for c in df_act.columns]
    for col in df_act.select_dtypes(include="object").columns:
        df_act[col] = df_act[col].str.strip()

    df_src = pd.read_csv(sourcewise_path, dtype=str, index_col=False).fillna("")
    df_src.columns = [c.strip() for c in df_src.columns]
    df_src["original_requirement"] = df_src["Description"].apply(clean_html)
    df_src["Mobile Number"] = df_src["Mobile Number"].str.strip()

    df = df_act.merge(df_src[["Mobile Number", "original_requirement"]],
                      on="Mobile Number", how="left")
    df["original_requirement"] = df["original_requirement"].fillna("")

    cur = conn.cursor()
    count = 0
    lead_id_by_mobile = {}

    for _, row in df.iterrows():
        mobile = row.get("Mobile Number", "").strip()
        raw_ts = row.get("Last Activity Date/ Time", "").strip()
        activity_time = None
        for fmt in ("%d-%b-%y %I:%M %p", "%d-%b-%y %I:%M:%S %p", "%d-%b-%Y %I:%M %p"):
            try:
                activity_time = datetime.strptime(raw_ts, fmt); break
            except ValueError:
                continue
        cur.execute("""
            INSERT INTO pratibha_leads
              (export_date, contact_name, company_name, mobile_number, city,
               lead_stage, lead_source, original_requirement, last_activity_time, activity_note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (mobile_number, export_date) DO UPDATE SET
              lead_stage = EXCLUDED.lead_stage,
              activity_note = EXCLUDED.activity_note,
              last_activity_time = EXCLUDED.last_activity_time,
              original_requirement = CASE WHEN EXCLUDED.original_requirement != ''
                THEN EXCLUDED.original_requirement
                ELSE pratibha_leads.original_requirement END
            RETURNING id
        """, (export_date, row.get("Contact Name",""), row.get("Company Name",""),
              mobile, row.get("City",""), row.get("Lead Stage",""),
              row.get("Lead Source","Indiamart"), row.get("original_requirement",""),
              activity_time, row.get("Last Activity Notes","")))
        r = cur.fetchone()
        if r and mobile:
            lead_id_by_mobile[mobile] = r[0]
        count += 1

    # Customer-lifecycle + multi-inquiry ingest (Loops A/B, FR-7 reopen, last_product recompute)
    # lives in csv_parser_ingest to keep this file under the per-file size limit.
    from csv_parser_ingest import apply_customer_and_inquiry_ingest
    apply_customer_and_inquiry_ingest(conn, df, df_src, lead_id_by_mobile, export_date)

    cur.close()
    conn.close()
    return count
