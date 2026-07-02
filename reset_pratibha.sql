-- Pratibha Chatbot — full reset.
-- Wipes every Pratibha table + LangGraph checkpoints. Schema untouched.
-- Run with:
--   docker compose exec postgres psql -U hca -d hca_agent -f /reset_pratibha.sql
-- (Container must have this file mounted, OR copy it in first — see commands below.)
--
-- After this runs:
--   - 0 leads, 0 customers, 0 responses, 0 conversations, 0 digests
--   - All LangGraph checkpoint state cleared (no resume from old sessions)
--   - Re-upload today's CSVs and you start from scratch.

BEGIN;

-- Pratibha tables, in FK-safe order
TRUNCATE TABLE
  pratibha_touches,
  pratibha_customer_inquiries,
  pratibha_responses,
  pratibha_conversations,
  pratibha_digest,
  pratibha_customers,
  pratibha_leads
RESTART IDENTITY CASCADE;

-- LangGraph checkpoint tables. Table names vary slightly by version;
-- DROP IF EXISTS + truncate any that are present.
DO $$
DECLARE
  t TEXT;
BEGIN
  FOR t IN
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public'
      AND (table_name LIKE 'checkpoint%' OR table_name = 'writes' OR table_name = 'blobs')
  LOOP
    EXECUTE format('TRUNCATE TABLE %I RESTART IDENTITY CASCADE', t);
  END LOOP;
END $$;

COMMIT;

-- Sanity
SELECT 'pratibha_leads' AS table, COUNT(*) FROM pratibha_leads
UNION ALL SELECT 'pratibha_customers', COUNT(*) FROM pratibha_customers
UNION ALL SELECT 'pratibha_customer_inquiries', COUNT(*) FROM pratibha_customer_inquiries
UNION ALL SELECT 'pratibha_responses', COUNT(*) FROM pratibha_responses
UNION ALL SELECT 'pratibha_touches', COUNT(*) FROM pratibha_touches
UNION ALL SELECT 'pratibha_digest', COUNT(*) FROM pratibha_digest
UNION ALL SELECT 'pratibha_conversations', COUNT(*) FROM pratibha_conversations;
