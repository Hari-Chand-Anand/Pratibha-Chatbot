"""Customer-lifecycle + multi-inquiry ingest logic. Called from csv_parser.parse_and_load_exports
after pratibha_leads is populated. Split out to keep csv_parser.py under the per-file size limit."""

from datetime import datetime


def apply_customer_and_inquiry_ingest(conn, df, df_src, lead_id_by_mobile, export_date):
    """Performs:
       - Loop A: upsert pratibha_customers from today's Activities-side merge.
       - Loop B: insert one pratibha_customer_inquiries row per Sourcewise inquiry
                 (preserves same-day duplicates).
       - FR-7 reopen pass for terminal customers whose mobile appears again
                 with a new open inquiry.
       - last_product recompute for all customers touched in this upload.
    Returns the set of mobile_numbers touched today."""
    cur = conn.cursor()

    new_mobiles_today = set()
    for _, row in df.iterrows():
        mobile = row.get("Mobile Number", "").strip()
        if not mobile:
            continue
        new_mobiles_today.add(mobile)
        cur.execute("""
            INSERT INTO pratibha_customers
                (mobile_number, contact_name, city, first_seen_date,
                 lifecycle_status, touch_count, next_touch_date)
            VALUES (%s,%s,%s,%s,'active',0,%s)
            ON CONFLICT (mobile_number) DO UPDATE SET
                contact_name = CASE WHEN pratibha_customers.contact_name = '' OR pratibha_customers.contact_name IS NULL
                    THEN EXCLUDED.contact_name ELSE pratibha_customers.contact_name END,
                city = CASE WHEN pratibha_customers.city = '' OR pratibha_customers.city IS NULL
                    THEN EXCLUDED.city ELSE pratibha_customers.city END,
                updated_at = NOW()
        """, (mobile, row.get("Contact Name",""), row.get("City",""), export_date, export_date))

    for _, srow in df_src.iterrows():
        mobile = (srow.get("Mobile Number") or "").strip()
        inquiry_text = (srow.get("original_requirement") or "").strip()
        if not mobile or not inquiry_text:
            continue
        raw_date = (srow.get("Lead Date") or "").strip()
        inquired_on = export_date
        for fmt in ("%d-%b-%y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                inquired_on = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue
        cur.execute("""
            INSERT INTO pratibha_customers
                (mobile_number, contact_name, city, first_seen_date,
                 lifecycle_status, touch_count, next_touch_date)
            VALUES (%s,%s,%s,%s,'active',0,%s)
            ON CONFLICT (mobile_number) DO NOTHING
        """, (mobile, srow.get("Contact Name",""), srow.get("City",""), inquired_on, inquired_on))
        # Migration #004: first_seen_time captures the earliest Cratio touch
        # timestamp we can find. Prefer the merged Activities-side
        # last_activity_time, else the parsed Lead Date at midnight IST.
        first_seen_time = None
        act_row = df[df["Mobile Number"].astype(str).str.strip() == mobile]
        if not act_row.empty:
            raw_ts = str(act_row.iloc[0].get("Last Activity Date/ Time", "") or "").strip()
            for fmt in ("%d-%b-%y %I:%M %p", "%d-%b-%y %I:%M:%S %p", "%d-%b-%Y %I:%M %p"):
                try:
                    first_seen_time = datetime.strptime(raw_ts, fmt)
                    break
                except ValueError:
                    continue
        if first_seen_time is None and inquired_on:
            first_seen_time = datetime.combine(inquired_on, datetime.min.time())

        cur.execute("""
            INSERT INTO pratibha_customer_inquiries
                (mobile_number, inquiry_text, inquired_on, source_lead_id, status,
                 first_seen_time)
            VALUES (%s,%s,%s,%s,'open',%s)
            ON CONFLICT (mobile_number, inquiry_text, inquired_on) DO UPDATE
              SET first_seen_time = COALESCE(pratibha_customer_inquiries.first_seen_time,
                                             EXCLUDED.first_seen_time)
        """, (mobile, inquiry_text, inquired_on, lead_id_by_mobile.get(mobile),
              first_seen_time))

    conn.commit()

    # FR-7 reopen pass
    cur.execute("""
        WITH terminal_with_new_open AS (
          SELECT pc.mobile_number FROM pratibha_customers pc
          WHERE pc.mobile_number = ANY(%s)
            AND pc.lifecycle_status IN ('ordered','declined','auto_junked')
            AND EXISTS (
              SELECT 1 FROM pratibha_customer_inquiries pci
              WHERE pci.mobile_number = pc.mobile_number AND pci.status = 'open'
                AND pci.inquired_on >= COALESCE(pc.last_resolution_at::date, %s)))
        UPDATE pratibha_customers pc
        SET lifecycle_status='active', touch_count=0, next_touch_date=%s,
            reopened_at=NOW(), updated_at=NOW()
        WHERE pc.mobile_number IN (SELECT mobile_number FROM terminal_with_new_open)
    """, (list(new_mobiles_today), export_date, export_date))

    # Recompute last_product from latest open inquiry
    cur.execute("""
        UPDATE pratibha_customers pc
        SET last_product = sub.inquiry_text, updated_at = NOW()
        FROM (SELECT DISTINCT ON (mobile_number) mobile_number, inquiry_text
              FROM pratibha_customer_inquiries WHERE status = 'open'
              ORDER BY mobile_number, inquired_on DESC, created_at DESC) sub
        WHERE pc.mobile_number = sub.mobile_number AND pc.mobile_number = ANY(%s)
    """, (list(new_mobiles_today),))

    conn.commit()
    cur.close()
    return new_mobiles_today
