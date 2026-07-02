"""Trigger -> required-fields contract for the Data-Quality layer (Migration #003).

The agent uses this to decide whether Pratibha's answer captured enough structured
data for the owner's daily report + brain's downstream queries. If a required field
is missing after LLM extraction, the agent asks one targeted follow-up question
per missing field, up to MAX_FOLLOWUPS (=3).

Adding or changing a trigger? Update REQUIRED_FIELDS + add the matching prompt to
FOLLOWUP_QUESTIONS. Don't add a trigger without both — the agent will silently let
vague answers through."""

# Maps trigger name (set in csv_parser_legacy.classify_legacy_lead_with_priority
# and csv_parser_queue._build_question_for_customer) -> ordered list of fields
# that MUST be populated on pratibha_responses before the lead is "done".
REQUIRED_FIELDS = {
    # Memory-Fix triggers (csv_parser_queue)
    "returning_customer":      ["machine_sent", "price_quoted_inr", "next_action"],
    "multi_inquiry":           ["machine_sent", "customer_response_status"],
    "followup_touch":          ["customer_response_status", "next_action"],

    # First-touch triggers (csv_parser_legacy)
    "sent_details":            ["machine_sent", "price_quoted_inr", "customer_response_status"],
    "sent_details_visit_planned": ["machine_sent", "visit_date", "customer_response_status"],
    "not_responding":          ["call_attempts", "next_action", "next_action_date"],
    "disconnected":            ["call_attempts", "next_action"],
    "not_required":            ["why_not_required", "future_potential"],
    "high_value_junk_flag":    ["actual_customer_response", "junk_reason"],
    "customer_described_need": ["machine_sent", "price_quoted_inr"],
    "forwarded_to_person":     ["forwarded_to_name", "handoff_status"],
    "callback_pending":        ["callback_outcome", "next_action"],
    "blank_note":              ["call_attempts", "next_action"],
    "person_mentioned":        ["forwarded_to_name", "handoff_status"],
    "junk_no_reason":          ["junk_reason"],
    "followup_stale":          ["customer_response_status", "next_action"],
    "stale_lead":              ["call_attempts", "next_action"],   # legacy queue
}

# One short, deterministic follow-up question per data field. Plain language.
# Owner sees the same question for the same gap every time — consistent across reps.
FOLLOWUP_QUESTIONS = {
    "machine_sent":              "Which exact model — DY-1201, ZOJE HS, something else? I need the model number.",
    "price_quoted_inr":          "What price did you quote — exact figure in rupees?",
    "customer_response_status":  "Has the customer replied — yes positively, no response, revision requested, or declined?",
    "visit_date":                "When is the visit planned — exact date please.",
    "call_attempts":             "How many times did you try calling — exact number?",
    "next_action":               "What is the next action — call back, send revision, schedule visit, or junk?",
    "next_action_date":          "When will you do that next action — date please.",
    "why_not_required":          "Why didn't they need it — what did they actually want?",
    "future_potential":          "Is there future potential here or permanently junk?",
    "actual_customer_response":  "What did the customer actually say when you spoke to them?",
    "junk_reason":               "Why was this junked — bad contact, language issue, or no real need?",
    "forwarded_to_name":         "Who exactly did you forward it to — name?",
    "handoff_status":            "What happened after you forwarded — did they take it forward?",
    "callback_outcome":          "What was the outcome when you called back?",
}

# Migration #004 phase 2 (01 Jul 2026): cap at 2 total follow-ups. Beyond 2,
# the loop bug from 26-Jun kicks in — Pratibha ends up typing "just told you"
# after the same field is re-asked because extraction failed on a short reply.
MAX_FOLLOWUPS = 2


# Fields where the value 0 is a legitimate "this is the answer" rather than
# "missing". For these, 0 satisfies the field.
#   - call_attempts = 0 means "I haven't tried yet" (still a clear signal)
#   - price_quoted_inr = 0 means "catalog only, no price quoted" (valid state
#     for sent-catalog scenarios where Pratibha hasn't given a number yet)
ZERO_IS_VALID = {"call_attempts", "price_quoted_inr"}


def missing_fields(trigger: str, response_row: dict) -> list:
    """Return ordered list of required-field names that are missing/empty
    in response_row. Returns [] if all fields satisfied (or if trigger unknown)."""
    required = REQUIRED_FIELDS.get(trigger, [])
    missing = []
    for f in required:
        v = response_row.get(f)
        if v is None or v == "":
            missing.append(f)
            continue
        # 0 is missing for most fields, but valid for the ZERO_IS_VALID set.
        if v == 0 and f not in ZERO_IS_VALID:
            missing.append(f)
    return missing


def compute_completeness_score(trigger: str, response_row: dict) -> int:
    """0..10. Fraction of required fields actually captured."""
    required = REQUIRED_FIELDS.get(trigger, [])
    if not required:
        return 10
    filled = 0
    for f in required:
        v = response_row.get(f)
        if v is None or v == "":
            continue
        filled += 1
    return round(10 * filled / len(required))


def next_followup_question(trigger: str, response_row: dict,
                           already_asked: list = None) -> tuple[str, str]:
    """Pick the first missing field for this trigger that HAS NOT already been
    asked this session, and return (field_name, templated_question).

    Migration #004 phase 2: skipping already-asked fields is the loop guard.
    Previously the same missing field would be picked repeatedly if extraction
    failed on a short reply — that's the 26-Jun bug. Now if a field was asked
    once and STILL couldn't be parsed, we move on rather than nag.

    Returns ("", "") if no unanswered-and-unasked field remains."""
    already_asked = set(already_asked or [])
    missing = missing_fields(trigger, response_row)
    for f in missing:
        if f in already_asked:
            continue
        q = FOLLOWUP_QUESTIONS.get(f)
        if q:
            return f, q
    return "", ""
