"""Pratibha-Agent smoke test — happy-path scenario.

Run AFTER every `docker compose up -d --build python-agent`. Takes ~5 seconds.

What it checks (in one shot):
  1. All modules import cleanly (catches the Aug-2025-style 'forgot to update an import' bug).
  2. ensure_tables runs without error against a real Postgres connection (DATABASE_URL env var).
  3. The Memory-Fix schema is present (pratibha_customers, pratibha_customer_inquiries, pratibha_touches).
  4. The pratibha_responses table has the new mobile_number column.
  5. build_question_queue can be called and returns a list.

Usage:
    docker compose exec python-agent python smoke_test.py

If any check fails, do NOT trust the deploy. Either fix forward or flip
MEMORY_FIX_ENABLED=false in backend/.env and rebuild.
"""
import os
import sys
import traceback
from datetime import date


def step(name):
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                fn(*a, **kw)
                print(f"  ✓ {name}")
                return True
            except Exception as e:
                print(f"  ✗ {name}")
                traceback.print_exc()
                return False
        return wrapper
    return deco


@step("import csv_parser")
def t_import_csv_parser():
    import csv_parser  # noqa: F401
    from csv_parser import (
        get_db_conn, ensure_tables, extract_date_from_filename,
        clean_html, parse_and_load_exports, build_question_queue,
    )
    _ = (get_db_conn, ensure_tables, extract_date_from_filename,
         clean_html, parse_and_load_exports, build_question_queue)


@step("import tools")
def t_import_tools():
    import tools  # noqa: F401
    from tools import (
        save_response, get_question_queue, get_next_question,
        generate_digest, call_groq_mini,
    )
    _ = (save_response, get_question_queue, get_next_question,
         generate_digest, call_groq_mini)


@step("import agent")
def t_import_agent():
    import agent  # noqa: F401
    from agent import (
        classify_input, resume_node, load_queue_node, build_graph,
    )
    _ = (classify_input, resume_node, load_queue_node, build_graph)


@step("ensure_tables runs cleanly")
def t_ensure_tables():
    from csv_parser import get_db_conn, ensure_tables
    conn = get_db_conn()
    ensure_tables(conn)
    conn.close()


@step("Memory-Fix schema is present")
def t_memory_fix_schema():
    from csv_parser import get_db_conn
    conn = get_db_conn()
    cur = conn.cursor()
    required = ["pratibha_customers", "pratibha_customer_inquiries", "pratibha_touches"]
    for t in required:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = %s", (t,))
        assert cur.fetchone(), f"missing table: {t}"
    cur.close()
    conn.close()


@step("pratibha_responses.mobile_number column exists")
def t_response_mobile_column():
    from csv_parser import get_db_conn
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name='pratibha_responses' AND column_name='mobile_number'
    """)
    assert cur.fetchone(), "pratibha_responses.mobile_number column missing"
    cur.close()
    conn.close()


@step("build_question_queue returns a list (today)")
def t_queue_builds():
    from csv_parser import get_db_conn, build_question_queue
    conn = get_db_conn()
    q = build_question_queue(date.today(), conn)
    conn.close()
    assert isinstance(q, list), f"expected list, got {type(q)}"


@step("feature flag flips to legacy path without error")
def t_legacy_path():
    os.environ["MEMORY_FIX_ENABLED"] = "false"
    try:
        from csv_parser import get_db_conn, build_question_queue
        conn = get_db_conn()
        q = build_question_queue(date.today(), conn)
        conn.close()
        assert isinstance(q, list)
    finally:
        os.environ["MEMORY_FIX_ENABLED"] = "true"


def main():
    print("Pratibha-Agent smoke test")
    print("=" * 60)
    checks = [
        t_import_csv_parser, t_import_tools, t_import_agent,
        t_ensure_tables, t_memory_fix_schema, t_response_mobile_column,
        t_queue_builds, t_legacy_path,
    ]
    results = [c() for c in checks]
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
