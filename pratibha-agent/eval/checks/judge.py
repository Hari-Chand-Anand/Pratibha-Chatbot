"""Layer 2 — Claude LLM-judge.

Only used for the two metrics that resist deterministic checking:
    A4 — Note-aware question rate
    B3 — Summary narrative quality

Judgements are cached in a JSONL sidecar (`judge_cache.jsonl`) keyed by
(case_id, agent_output_hash), so re-runs against unchanged outputs are free.

Rubric-based scoring 1..10, plus a one-line reason. The rubric is written into
the prompt — never softened, never hidden.
"""
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE = Path(__file__).parent / "judge_cache.jsonl"
_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-sonnet-5")

try:
    from anthropic import Anthropic
    _key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    _client = Anthropic(api_key=_key) if _key else None
except Exception:
    _client = None


@dataclass
class JudgeVerdict:
    score: int          # 1..10
    passed: bool        # score >= threshold
    reason: str
    metric: str


def _cache_key(case_id: str, output: str, metric: str) -> str:
    h = hashlib.sha256((case_id + metric + output).encode()).hexdigest()[:16]
    return h


def _read_cache(key: str) -> dict | None:
    if not _CACHE.exists():
        return None
    for line in _CACHE.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if row.get("key") == key:
                return row
        except Exception:
            continue
    return None


def _write_cache(key: str, verdict: JudgeVerdict) -> None:
    row = {
        "key": key, "metric": verdict.metric, "score": verdict.score,
        "reason": verdict.reason,
    }
    with _CACHE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _call_judge(prompt: str) -> tuple[int, str]:
    if _client is None:
        return 0, "judge unavailable — no ANTHROPIC_API_KEY"
    try:
        resp = _client.messages.create(
            model=_MODEL,
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
    except Exception as e:
        return 0, f"judge exception: {e}"

    # Expected format: "SCORE: N\nREASON: ..."
    score = 0
    reason = text
    for line in text.splitlines():
        if line.lower().startswith("score:"):
            try:
                score = int(line.split(":", 1)[1].strip().split()[0])
            except Exception:
                pass
        elif line.lower().startswith("reason:"):
            reason = line.split(":", 1)[1].strip()
    return score, reason


# ─────────────────────────────────────────────────────────────────────────────
# A4 — Note-aware question rate
# ─────────────────────────────────────────────────────────────────────────────

A4_RUBRIC = """You are evaluating a sales-accountability bot's next question to Pratibha.

INPUT:
  Activity note: {note}
  Original requirement: {req}
  Touch count: {touch}
  Pratibha's last reply: {reply}

BOT ASKED:
  {question}

SCORING RUBRIC (1..10):
  1-3  Ignores the note entirely. Generic template.
  4-6  References the lead superficially but misses the point of the note.
  7-8  Reads the note AND the requirement, asks a specific relevant question.
  9-10 Bot asks the exact question a good manager would ask given this note.

PASS THRESHOLD: 7.

Output format (exactly two lines):
SCORE: <number 1-10>
REASON: <one sentence>"""


def judge_a4_note_aware(*, note: str, req: str, touch: int, reply: str,
                        question: str, case_id: str) -> JudgeVerdict:
    key = _cache_key(case_id, question, "A4")
    cached = _read_cache(key)
    if cached:
        return JudgeVerdict(cached["score"], cached["score"] >= 7,
                            cached["reason"], "A4")
    prompt = A4_RUBRIC.format(
        note=note or "(blank)", req=req or "(none)", touch=touch or 0,
        reply=reply or "(N/A)", question=question or "(none)",
    )
    score, reason = _call_judge(prompt)
    v = JudgeVerdict(score, score >= 7, reason, "A4")
    if score > 0:
        _write_cache(key, v)
    return v


# ─────────────────────────────────────────────────────────────────────────────
# B3 — Summary narrative quality
# ─────────────────────────────────────────────────────────────────────────────

B3_RUBRIC = """You are evaluating a daily sales summary written by an AI for a
director. The reference template is money-first → breakdown by outcome →
gut-check ratios → two things worth raising with the team.

REFERENCE EXAMPLE (this is what "good" looks like):
{reference}

GENERATED SUMMARY:
{generated}

SCORING RUBRIC (1..10):
  1-3  Missing sections, wrong framing, or made-up numbers.
  4-6  All sections present but generic; no actionable observations.
  7-8  Named customers/machines/₹, specific gut-check numbers, two useful
       observations.
  9-10 Reads like a competent team-lead wrote it — pattern-spotting is sharp,
       recommendations are concrete.

PASS THRESHOLD: 8.

Output format (exactly two lines):
SCORE: <number 1-10>
REASON: <one sentence>"""


def judge_b3_summary(*, reference: str, generated: str, case_id: str) -> JudgeVerdict:
    key = _cache_key(case_id, generated, "B3")
    cached = _read_cache(key)
    if cached:
        return JudgeVerdict(cached["score"], cached["score"] >= 8,
                            cached["reason"], "B3")
    prompt = B3_RUBRIC.format(reference=reference or "(no reference)", generated=generated or "")
    score, reason = _call_judge(prompt)
    v = JudgeVerdict(score, score >= 8, reason, "B3")
    if score > 0:
        _write_cache(key, v)
    return v
