"""Hard-junk classifier + touch-4 consent flow (Migration #004).

TWO SEPARATE CONCEPTS — do not confuse:

  1. HARD-JUNK — after ONE call, the lead is provably junk and won't resurface.
     Deterministic rules against a whitelist of criteria. Sets
     pratibha_customers.resurface_blocked=TRUE, hard_junk_reason='<tag>'.

  2. TOUCH-4 CONSENT — the lead has been queued 4 times without resolution.
     Agent asks Pratibha explicitly: "Junk this and discard? Confirm."
     If yes  → resurface_blocked=TRUE, hard_junk_reason='touch_4_consented'.
     If no + plan → moves to stuck_high_effort bucket (touch_count stays at 4,
                    lead is NOT requeued but IS listed in weekly summary).

FORCE-RESURFACE OVERRIDES apply to (1) only:
  Even if Pratibha marks a lead junk, if the ORIGINAL requirement crosses any
  of these thresholds, the lead resurfaces at least once more for director
  review — no exceptions.

    - POV explicitly > ₹50,000 in original_requirement
    - Bulk quantity > 5 pieces
    - Probable Order Value ≥ ₹1,00,000
    - Customer named a specific model number by exact code
"""
import re
from datetime import date, timedelta
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Configurable thresholds — bumped as we learn more from production
# ─────────────────────────────────────────────────────────────────────────────

POV_HARD_JUNK_CEILING = 50_000        # rupees. Below this, ordinary junk rules apply.
BULK_QTY_THRESHOLD    = 5             # pieces
POV_FORCE_RESURFACE   = 100_000       # ₹1L absolute floor for forced director review
INVALID_CONTACT_MIN_ATTEMPTS = 2

# Wrong-product blacklist — HCA does not sell any of these, ever.
# Any lead where the ORIGINAL requirement contains these terms is hard-junk on sight,
# regardless of Pratibha's follow-through.
WRONG_PRODUCT_BLACKLIST = (
    "banana leaf",
    "raw banana",
    "food processing",
    "milk processing",
    "medical",
    "diagnostic",
    "printer",
    "3d printer",
    "cnc",
    "milling",
    "lathe",
    "welding",
    "packaging carton",
)

# Language regions where Pratibha cannot serve directly — bulk of leads confirmed
# junk over the last 30 days. Extended when a new region shows a pattern.
LANGUAGE_BARRIER_REGIONS = (
    "bellary", "siruguppa", "hospet", "hubli", "mysuru", "mysore",  # Karnataka
    "coimbatore", "salem", "madurai",                               # Tamil Nadu
    "kochi", "trivandrum", "thiruvananthapuram",                    # Kerala
)

# Explicit non-buyer phrases in Pratibha's answer OR the CRM activity note.
NON_BUYER_PHRASES = (
    "just checking",
    "just researching",
    "not buying",
    "not required",
    "no requirement",
    "not a buyer",
    "not interested",
    "customer not need",
    "just checking price",
    "just enquiry",
)


# ─────────────────────────────────────────────────────────────────────────────
# Value extraction — cheap regex over the original_requirement string.
# Never invents numbers; returns None if it cannot parse confidently.
# ─────────────────────────────────────────────────────────────────────────────

def extract_pov_inr(requirement: str) -> Optional[int]:
    """Best-effort parse of 'Probable Order Value' field from Sourcewise text.
    Handles: 'Rs 63000-110000', 'Rs. 63,000 - 1,10,000', 'More than Rs 1 Crore',
    '17.6-18.5 lakh'. Returns the UPPER bound in rupees, or None if no value found."""
    if not requirement:
        return None
    s = requirement.lower().replace(",", "").replace("₹", "rs")
    # "more than rs 1 crore" / "rs 1 crore+"
    m = re.search(r"(\d+(?:\.\d+)?)\s*crore", s)
    if m:
        return int(float(m.group(1)) * 10_000_000)
    # "rs X-Y lakh" or "X lakh"
    m = re.search(r"(?:rs\s*)?(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(\d+(?:\.\d+)?)?\s*lakh", s)
    if m:
        upper = float(m.group(2) or m.group(1))
        return int(upper * 100_000)
    # "rs X-Y" (plain rupees)
    m = re.search(r"rs\s*(\d+)\s*[-to]+\s*(\d+)", s)
    if m:
        return int(m.group(2))
    # "rs X" (single value)
    m = re.search(r"rs\s*(\d+)", s)
    if m:
        return int(m.group(1))
    return None


def extract_quantity(requirement: str) -> Optional[int]:
    """Parse quantity from 'Quantity : 95 Piece' or '1165 pieces' patterns."""
    if not requirement:
        return None
    s = requirement.lower()
    m = re.search(r"quantity\s*:\s*(\d+)\s*(?:piece|pieces|pcs|units?)?", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{2,4})\s*(?:piece|pieces|pcs|units)", s)
    if m:
        return int(m.group(1))
    return None


MODEL_CODE_RE = re.compile(
    r"\b(?:DY|DLR|DFB|LS|LU|ES|ZOJE|KANSAI|B\d+|LK|MB|"
    r"F5|F7|EBT|JZQ|CM|LEW|HW)[-\s]?\d{2,5}[A-Z]{0,3}\b",
    re.IGNORECASE,
)


def contains_specific_model(requirement: str) -> bool:
    """True if the customer named a specific model number (e.g. DY-1201, ZOJE HS,
    B2000C-BELT, F5). Used to force-resurface high-intent leads even if junked."""
    return bool(MODEL_CODE_RE.search(requirement or ""))


# ─────────────────────────────────────────────────────────────────────────────
# Force-resurface gate
# ─────────────────────────────────────────────────────────────────────────────

def must_force_resurface(requirement: str) -> tuple[bool, str]:
    """Return (True, reason) if this lead must resurface at least once more
    for director review, regardless of Pratibha's junk mark. Reason is the
    audit tag written into the flag."""
    pov = extract_pov_inr(requirement)
    if pov is not None and pov >= POV_FORCE_RESURFACE:
        return True, f"pov_1L_plus:{pov}"
    qty = extract_quantity(requirement)
    if qty is not None and qty > BULK_QTY_THRESHOLD:
        return True, f"bulk_qty:{qty}"
    if contains_specific_model(requirement):
        return True, "specific_model_named"
    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# Hard-junk classifier — returns a tag or None
# ─────────────────────────────────────────────────────────────────────────────

def classify_hard_junk(
    *,
    original_requirement: str,
    activity_note: str,
    pratibha_answer: str,
    city: str,
    call_attempts: Optional[int],
    mobile_number: str,
    already_resolved_within_30d: bool,
) -> Optional[str]:
    """Return a hard-junk tag if this lead qualifies for permanent junk after
    one call, else None. Force-resurface overrides win — they are checked
    first and always suppress the tag."""
    forced, _reason = must_force_resurface(original_requirement)
    if forced:
        return None

    req_l = (original_requirement or "").lower()
    note_l = (activity_note or "").lower()
    answer_l = (pratibha_answer or "").lower()
    city_l = (city or "").lower()

    # 1. Wrong product — deterministic keyword hit.
    for term in WRONG_PRODUCT_BLACKLIST:
        if term in req_l:
            return "wrong_product"

    # 2. Language barrier — must be BOTH signalled (city in known region
    #    OR "language" mentioned) AND POV below ceiling.
    pov = extract_pov_inr(original_requirement)
    lang_hit = (
        any(r in city_l for r in LANGUAGE_BARRIER_REGIONS)
        or "language" in note_l or "language" in answer_l
    )
    if lang_hit and (pov is None or pov < POV_HARD_JUNK_CEILING):
        return "language_barrier"

    # 3. Explicit non-buyer + POV below ceiling.
    non_buyer_hit = any(p in answer_l for p in NON_BUYER_PHRASES) \
                 or any(p in note_l for p in NON_BUYER_PHRASES)
    if non_buyer_hit and (pov is None or pov < POV_HARD_JUNK_CEILING):
        return "explicit_non_buyer"

    # 4. Invalid contact — 2 attempts, both dead, no alternate.
    if (call_attempts or 0) >= INVALID_CONTACT_MIN_ATTEMPTS and any(
        p in answer_l or p in note_l
        for p in ("wrong number", "disconnected", "switched off", "not in service")
    ):
        return "invalid_contact"

    # 5. Duplicate — same mobile resolved within 30 days.
    if already_resolved_within_30d:
        return "duplicate"

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — set the block flags on pratibha_customers
# ─────────────────────────────────────────────────────────────────────────────

def apply_hard_junk(conn, mobile_number: str, reason: str) -> None:
    """Set resurface_blocked=TRUE on the customer and record the reason.
    Idempotent: overwriting with the same reason is a no-op."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE pratibha_customers
           SET resurface_blocked = TRUE,
               hard_junk_reason  = %s,
               hard_junked_at    = COALESCE(hard_junked_at, NOW()),
               lifecycle_status  = CASE
                   WHEN lifecycle_status = 'ordered' THEN 'ordered'    -- never override wins
                   ELSE 'auto_junked'
               END,
               last_resolution_at = COALESCE(last_resolution_at, NOW())
         WHERE mobile_number = %s
           AND (resurface_blocked = FALSE OR resurface_blocked IS NULL)
    """, (reason, mobile_number))
    conn.commit()
    cur.close()


def was_resolved_within(conn, mobile_number: str, days: int = 30) -> bool:
    """True if this mobile has a previous ordered/declined resolution in the
    last N days — used to detect duplicates."""
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM pratibha_customers
        WHERE mobile_number = %s
          AND lifecycle_status IN ('ordered', 'declined', 'auto_junked')
          AND last_resolution_at >= NOW() - (%s * INTERVAL '1 day')
        LIMIT 1
    """, (mobile_number, days))
    hit = cur.fetchone() is not None
    cur.close()
    return hit


# ─────────────────────────────────────────────────────────────────────────────
# Touch-4 consent flow — called by the agent when touch_count reaches 4
# ─────────────────────────────────────────────────────────────────────────────

TOUCH_4_MAX = 4


def touch_4_prompt(customer_row: dict) -> str:
    """The exact question the agent must ask when a lead reaches touch 4."""
    name = customer_row.get("contact_name") or "this customer"
    city = customer_row.get("city") or ""
    first_seen = customer_row.get("first_seen_date")
    date_str = first_seen.strftime("%d %b") if hasattr(first_seen, "strftime") else str(first_seen or "")
    return (
        f"This is the 4th session for {name} from {city} "
        f"(first seen Cratio {date_str}). No resolution yet. "
        f"Mark as junk and permanently discard? (yes / no + plan)"
    )


def handle_touch_4_reply(conn, mobile_number: str, reply: str) -> str:
    """Process Pratibha's reply to the touch-4 consent question.

    Returns one of:
      'consented'       → resurface_blocked=TRUE, hard_junk_reason='touch_4_consented'
      'plan_provided'   → moved to stuck_high_effort, no resurface but tracked
      'ambiguous'       → agent must re-ask
    """
    reply_l = (reply or "").strip().lower()
    if not reply_l:
        return "ambiguous"

    yes_words = ("yes", "haan", "ha", "y", "confirm", "junk it", "discard", "junk kar")
    no_words = ("no", "nahi", "nahin", "n")

    starts_with = reply_l.split()[0] if reply_l.split() else ""

    if starts_with in yes_words or any(w in reply_l for w in ("junk it", "discard", "permanently")):
        apply_hard_junk(conn, mobile_number, "touch_4_consented")
        return "consented"

    if starts_with in no_words or "plan" in reply_l or "will" in reply_l or "next" in reply_l:
        # Any plan text → park in stuck_high_effort, don't requeue.
        cur = conn.cursor()
        cur.execute("""
            UPDATE pratibha_customers
               SET lifecycle_status = 'stuck_high_effort',
                   next_touch_date  = NULL,
                   updated_at       = NOW()
             WHERE mobile_number = %s
        """, (mobile_number,))
        conn.commit()
        cur.close()
        return "plan_provided"

    return "ambiguous"
