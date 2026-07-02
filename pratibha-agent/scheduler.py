"""Two-window scheduler with idempotency (Migration #004).

Windows:
  6:00 PM IST — primary. Generates summary for TODAY.
  10:00 AM IST — backup. Scans last 3 days; regenerates any digest whose
                 status is NOT 'complete' (partial | failed | pending | missing).

Coordination via pratibha_digest.status:
  pending   → summary never attempted
  partial   → session still in progress at 6 PM (last message < 15 min old)
  failed    → Claude call raised; template fallback was written
  complete  → success, backup job skips

Row-level lock via SELECT ... FOR UPDATE SKIP LOCKED ensures 6 PM and 10 AM
can never collide. attempt_count caps at 3 — after that we stop retrying
and expect manual intervention (also logged loudly).
"""
import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from csv_parser import get_db_conn
from summary_writer import generate_daily_summary
from monitor_writer import generate_monitor_report

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MAX_ATTEMPTS = 3
PARTIAL_WINDOW_MIN = 15   # if last chat message younger than this at 6 PM → partial

# 02 Jul 2026: auto-promote today's flagged traces into the regression suite
# and re-check them, right after the summary/monitor are written. Replaces
# the manual "review traces every Monday" step that never actually ran.
# Feature-flagged so it can be turned off without touching this file again.
EVAL_AUTO_PROMOTE_ENABLED = os.environ.get("EVAL_AUTO_PROMOTE_ENABLED", "true").lower() == "true"


# ─────────────────────────────────────────────────────────────────────────────
# Idempotent digest lifecycle helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_digest_row(conn, target_date: str) -> None:
    """Guarantee a pratibha_digest row exists for target_date so we can lock it."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pratibha_digest (digest_date, status, attempt_count)
        VALUES (%s, 'pending', 0)
        ON CONFLICT (digest_date) DO NOTHING
    """, (target_date,))
    conn.commit()
    cur.close()


def _acquire_digest_lock(conn, target_date: str) -> dict | None:
    """Row-lock the digest for target_date. Returns current state, or None if
    another worker holds it (SKIP LOCKED)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT status, attempt_count
        FROM pratibha_digest
        WHERE digest_date = %s
        FOR UPDATE SKIP LOCKED
    """, (target_date,))
    row = cur.fetchone()
    if not row:
        cur.close()
        return None
    return {"status": row[0], "attempt_count": row[1] or 0, "_cur": cur}


def _release(conn, lock_ctx: dict) -> None:
    if lock_ctx and lock_ctx.get("_cur"):
        lock_ctx["_cur"].close()
    conn.commit()


def _mark_status(conn, target_date: str, status: str,
                 failure_reason: str | None = None,
                 generated_by: str | None = None) -> None:
    cur = conn.cursor()
    cur.execute("""
        UPDATE pratibha_digest
           SET status          = %s,
               last_attempt_at = NOW(),
               attempt_count   = attempt_count + 1,
               failure_reason  = COALESCE(%s, failure_reason),
               generated_by    = COALESCE(%s, generated_by)
         WHERE digest_date = %s
    """, (status, failure_reason, generated_by, target_date))
    conn.commit()
    cur.close()


def _session_still_active(conn, target_date: str) -> bool:
    """True if a pratibha_conversations message was written within
    PARTIAL_WINDOW_MIN minutes — session probably not finished yet."""
    cur = conn.cursor()
    cur.execute("""
        SELECT MAX(created_at) FROM pratibha_conversations WHERE conv_date = %s
    """, (target_date,))
    row = cur.fetchone()
    cur.close()
    if not row or not row[0]:
        return False
    last_at = row[0]
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - last_at
    return delta < timedelta(minutes=PARTIAL_WINDOW_MIN)


# ─────────────────────────────────────────────────────────────────────────────
# Generation logic — shared by both windows
# ─────────────────────────────────────────────────────────────────────────────

def _attempt_summary(target_date: str, window: str) -> str:
    """Attempt to generate the summary for target_date. Returns final status."""
    conn = get_db_conn()
    _ensure_digest_row(conn, target_date)
    lock = _acquire_digest_lock(conn, target_date)
    if lock is None:
        logger.info("[%s] Digest %s locked by another worker — skipping", window, target_date)
        conn.close()
        return "locked"

    try:
        if lock["status"] == "complete":
            logger.info("[%s] Digest %s already complete — no action", window, target_date)
            return "complete"

        if lock["attempt_count"] >= MAX_ATTEMPTS:
            logger.error(
                "[%s] Digest %s hit MAX_ATTEMPTS (%s) — manual review required",
                window, target_date, MAX_ATTEMPTS,
            )
            return "cap_reached"

        # Session still live at 6 PM → partial only, wait for 10 AM to finalise.
        if window == "6pm" and _session_still_active(conn, target_date):
            logger.info("[%s] Session still active for %s — writing partial", window, target_date)
            _mark_status(conn, target_date, "partial")
            return "partial"

        # Generate. On failure, mark failed and let 10 AM retry.
        try:
            path = generate_daily_summary(target_date)
            _mark_status(conn, target_date, "complete", generated_by=os.environ.get("SUMMARY_LLM", "claude"))
            logger.info("[%s] Summary %s written to %s", window, target_date, path)
            # Migration #004: also produce the monitor report from today's traces.
            # Failure here does NOT flip digest status — the summary is what
            # matters for the digest lifecycle.
            try:
                mpath = generate_monitor_report(target_date)
                if mpath:
                    logger.info("[%s] Monitor written to %s", window, mpath)
            except Exception as me:
                logger.warning("[%s] Monitor report failed: %s", window, me)

            # 02 Jul 2026: auto-promote today's flagged traces into the
            # regression suite, then re-run the free (Layer 1, no LLM cost)
            # eval so the daily log shows whether today's real failures are
            # still failing under current code. Best-effort — never flips
            # digest status, same as the monitor report above.
            if EVAL_AUTO_PROMOTE_ENABLED:
                try:
                    from eval.promote_from_traces import promote
                    n = promote(target_date, verbose=False)
                    logger.info("[%s] Eval: %s new case(s) promoted from %s traces",
                                window, n, target_date)
                    if n > 0:
                        from eval.run_eval import run as run_eval
                        exit_code = run_eval(dataset="regressions", layer=1, verbose=False)
                        logger.info("[%s] Eval: regression re-run exit_code=%s "
                                    "(0 = all Blockers pass)", window, exit_code)
                except Exception as ee:
                    logger.warning("[%s] Eval auto-promotion failed: %s", window, ee)

            return "complete"
        except Exception as e:
            logger.exception("[%s] Summary generation failed for %s", window, target_date)
            _mark_status(conn, target_date, "failed", failure_reason=str(e)[:500])
            return "failed"
    finally:
        _release(conn, lock)
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler entry points
# ─────────────────────────────────────────────────────────────────────────────

async def trigger_6pm_primary():
    """Primary window — generate today's summary at 6 PM IST."""
    today = datetime.now(IST).date().isoformat()
    logger.info("[6pm] Primary run for %s", today)
    _attempt_summary(today, window="6pm")


async def trigger_10am_backup():
    """Backup window — scan last 3 days, regenerate anything not complete.
    Catches: sessions that were still live at 6 PM (partial), Claude API failures,
    late CSV uploads (June-27 uploaded on June-30 case), missed cron runs."""
    now_ist = datetime.now(IST).date()
    logger.info("[10am] Backup scan from %s", now_ist)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT digest_date, status, attempt_count
        FROM pratibha_digest
        WHERE digest_date >= %s
          AND digest_date <= %s
          AND (status IS NULL OR status <> 'complete')
        ORDER BY digest_date ASC
    """, (now_ist - timedelta(days=3), now_ist - timedelta(days=1)))
    stale = cur.fetchall()
    cur.close()
    conn.close()

    if not stale:
        logger.info("[10am] Nothing to reprocess")
        return

    for digest_date, status, attempts in stale:
        if attempts and attempts >= MAX_ATTEMPTS:
            logger.error(
                "[10am] Skipping %s — attempt_count %s >= %s (manual review needed)",
                digest_date, attempts, MAX_ATTEMPTS,
            )
            continue
        logger.info("[10am] Reprocessing %s (was: %s, attempts: %s)",
                    digest_date, status, attempts)
        _attempt_summary(digest_date.isoformat(), window="10am")


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        func=trigger_6pm_primary,
        trigger=CronTrigger(hour=18, minute=0, timezone=IST),
        id="daily_summary_6pm",
        replace_existing=True,
        misfire_grace_time=3600,   # if we boot late, still run within 1h
    )
    scheduler.add_job(
        func=trigger_10am_backup,
        trigger=CronTrigger(hour=10, minute=0, timezone=IST),
        id="daily_summary_10am_backup",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("[Scheduler] 6 PM primary + 10 AM backup active (IST)")
    return scheduler
