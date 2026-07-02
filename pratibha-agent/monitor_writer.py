"""Daily monitor report (Migration #004).

Aggregates today's rows from pratibha_agent_traces and computes pass-rate per
metric. Runs alongside the 6 PM summary AND the 10 AM backup (both windows
call generate_monitor_report(date)).

Output: /app/summaries/monitor_YYYY-MM-DD.md
Alerts: any Blocker metric with pass-rate < target logs an ERROR and also
        writes a top-of-file alert banner in the markdown.

The metric definitions match eval/checks/deterministic.py — same code path
runs offline (on the seed dataset) and online (on real traces).
"""
import logging
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from csv_parser import get_db_conn

logger = logging.getLogger(__name__)

MONITOR_ENABLED = os.environ.get("MONITOR_ENABLED", "true").lower() == "true"

BLOCKER_TARGETS = {
    "no_repeat_question":         (1.00, "A1", "No repeat questions"),
    "first_turn_acceptance":      (0.90, "A2", "First-turn acceptance"),
    "extraction_success":         (0.90, "A3", "Field extraction"),
    "resurface_has_date":         (1.00, "A5", "Resurface has CRM date"),
    "high_pov_flag_fired":        (1.00, "A7", "High-POV flagged"),
}
HIGH_TARGETS = {
    "session_completion":         (0.80, "A6", "Session completion"),
}


def generate_monitor_report(date_str: str) -> str:
    """Read all traces for the given date, compute per-metric pass rates, write
    monitor_YYYY-MM-DD.md. Returns the file path."""
    if not MONITOR_ENABLED:
        logger.info("[Monitor] disabled")
        return ""

    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) FROM pratibha_agent_traces WHERE session_date = %s
    """, (date_str,))
    total_turns = cur.fetchone()[0] or 0

    if total_turns == 0:
        cur.close()
        conn.close()
        return _write_empty_monitor(date_str)

    # Per-flag counts (each flag = one metric failure)
    cur.execute("""
        SELECT unnest(auto_flags), COUNT(*)
        FROM pratibha_agent_traces
        WHERE session_date = %s
        GROUP BY 1
    """, (date_str,))
    flag_counts = dict(cur.fetchall())

    # Session completion — how many queued vs how many covered
    cur.execute("""
        SELECT COUNT(DISTINCT lead_id)
        FROM pratibha_agent_traces
        WHERE session_date = %s AND lead_id IS NOT NULL
    """, (date_str,))
    leads_covered = cur.fetchone()[0] or 0

    cur.execute("""
        SELECT COUNT(*) FROM pratibha_customers
        WHERE next_touch_date = %s
          AND COALESCE(resurface_blocked, FALSE) = FALSE
    """, (date_str,))
    leads_queued = cur.fetchone()[0] or 0

    # Top failing traces for triage
    cur.execute("""
        SELECT id, lead_id, mobile_number, trigger_type, touch_count,
               llm_output, user_reply, auto_flags
        FROM pratibha_agent_traces
        WHERE session_date = %s
          AND array_length(auto_flags, 1) > 0
        ORDER BY id DESC
        LIMIT 20
    """, (date_str,))
    failing = cur.fetchall()

    cur.close()
    conn.close()

    # Compute pass rates
    def _rate(flag_name: str, total: int) -> float:
        fails = flag_counts.get(flag_name, 0)
        return 1.0 - (fails / total) if total else 1.0

    metrics = {
        "no_repeat_question":    _rate("repeat_question", total_turns),
        "first_turn_acceptance": _rate("terminal_ignored", total_turns),
        "extraction_success":    _rate("extraction_missed", total_turns),
        "resurface_has_date":    _rate("resurface_missing_date", total_turns),
        "high_pov_flag_fired":   _rate("high_pov_flag_missed", total_turns),
        "session_completion":    leads_covered / leads_queued if leads_queued else 1.0,
    }

    # Build report
    path = _write_report(date_str, total_turns, leads_covered, leads_queued,
                        metrics, flag_counts, failing)

    # Alerts
    for m, rate in metrics.items():
        target = BLOCKER_TARGETS.get(m, (None,))[0] or HIGH_TARGETS.get(m, (None,))[0]
        if target and rate < target and m in BLOCKER_TARGETS:
            logger.error(
                "[Monitor ALERT] %s %.0f%% < target %.0f%% on %s",
                m, rate * 100, target * 100, date_str,
            )
    return path


def _write_empty_monitor(date_str: str) -> str:
    path = Path(f"/app/summaries/monitor_{date_str}.md")
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        f"# Monitor — {date_str}\n\nNo traces recorded for this date.\n",
        encoding="utf-8",
    )
    return str(path)


def _write_report(date_str, total_turns, leads_covered, leads_queued,
                  metrics, flag_counts, failing_traces) -> str:
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Monitor — {date_str}",
        f"_Generated: {ts}_",
        "",
        f"**Turns traced today:** {total_turns}",
        f"**Leads covered:** {leads_covered} / {leads_queued} queued",
        "",
    ]

    # Alert banner
    alerts = []
    for m, (tgt, mid, label) in BLOCKER_TARGETS.items():
        rate = metrics.get(m, 1.0)
        if rate < tgt:
            alerts.append(f"[{mid}] {label}: {rate:.0%} < {tgt:.0%}")
    if alerts:
        lines.append("## BLOCKER ALERTS")
        for a in alerts:
            lines.append(f"- {a}")
        lines.append("")

    # Scorecard
    lines.append("## Scorecard")
    lines.append("")
    lines.append("| ID | Metric | Rate | Target | Status |")
    lines.append("|---|---|---|---|---|")
    for m, (tgt, mid, label) in {**BLOCKER_TARGETS, **HIGH_TARGETS}.items():
        rate = metrics.get(m, 1.0)
        status = "PASS" if rate >= tgt else "FAIL"
        lines.append(f"| {mid} | {label} | {rate:.0%} | {tgt:.0%} | {status} |")
    lines.append("")

    # Failing traces
    if failing_traces:
        lines.append("## Failing traces (top 20)")
        lines.append("")
        for tid, lid, mobile, trigger, touch, out, reply, flags in failing_traces:
            lines.append(
                f"- **trace #{tid}** (lead {lid}, mobile {mobile}, trigger `{trigger}`, "
                f"touch {touch}) — flags: {list(flags)}"
            )
            if out:
                lines.append(f"  - agent: `{out[:120]}`")
            if reply:
                lines.append(f"  - pratibha: `{reply[:120]}`")
        lines.append("")

    # Flag breakdown
    if flag_counts:
        lines.append("## Flag counts")
        lines.append("")
        for flag, count in sorted(flag_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- `{flag}`: {count}")
        lines.append("")

    path = Path(f"/app/summaries/monitor_{date_str}.md")
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
