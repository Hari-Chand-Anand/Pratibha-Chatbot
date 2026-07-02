"""Domain vocabulary for Pratibha's chats — phrases that have specific business
meaning in HCA's Cratio workflow that the generic LLM extractor doesn't catch.

The LLM is good at parsing free-form text but doesn't know Cratio's conventions:
  - "yet to talk" in Cratio actually means "first contact attempted, didn't connect"
    (i.e. call_attempts >= 1), not "haven't tried"
  - "junk call" / "not a buyer" close out the lead — no further follow-up needed
  - "sent catalog" implies no specific quote was given (price_quoted_inr = 0)

Edit this file whenever you find the bot misreading a phrase. The rules apply
BEFORE the LLM extraction runs, so glossary matches take priority over the
LLM's guess. Predictable, auditable, no retraining needed.

Each entry:
  - "phrase": case-insensitive substring match against Pratibha's answer
  - "fields": dict of field overrides to set on the response
  - "terminal": if True, the agent stops asking follow-ups AND the lead's
                lifecycle_status is set to 'declined' (won't re-surface)
"""

# Order matters slightly — more specific phrases first so they shadow shorter ones.
PHRASE_RULES = [
    # ---- Terminal closures (lead is done, don't resurface) ----
    {"phrase": "not related to our garment",
     "fields": {"junk_reason": "wrong industry", "next_action": "junk"},
     "terminal": True},
    {"phrase": "not related to garment",
     "fields": {"junk_reason": "wrong industry", "next_action": "junk"},
     "terminal": True},
    {"phrase": "not garment industry",
     "fields": {"junk_reason": "wrong industry", "next_action": "junk"},
     "terminal": True},
    {"phrase": "we do not sell",
     "fields": {"junk_reason": "not our product", "next_action": "junk"},
     "terminal": True},
    {"phrase": "we don't sell",
     "fields": {"junk_reason": "not our product", "next_action": "junk"},
     "terminal": True},
    {"phrase": "we dont sell",
     "fields": {"junk_reason": "not our product", "next_action": "junk"},
     "terminal": True},
    {"phrase": "language barrier",
     "fields": {"junk_reason": "language", "next_action": "junk"},
     "terminal": True},
    {"phrase": "language issue",
     "fields": {"junk_reason": "language", "next_action": "junk"},
     "terminal": True},
    {"phrase": "not a buyer",
     "fields": {"junk_reason": "not a buyer", "next_action": "junk"},
     "terminal": True},
    {"phrase": "no requirement of machine",
     "fields": {"junk_reason": "no need", "next_action": "junk"},
     "terminal": True},
    {"phrase": "no requirement",
     "fields": {"junk_reason": "no need", "next_action": "junk"},
     "terminal": True},
    {"phrase": "not need any machine",
     "fields": {"junk_reason": "no need", "next_action": "junk"},
     "terminal": True},
    {"phrase": "customer not need",
     "fields": {"junk_reason": "no need", "next_action": "junk"},
     "terminal": True},
    {"phrase": "just checking",
     "fields": {"junk_reason": "tyre kicker", "next_action": "junk"},
     "terminal": True},
    {"phrase": "junk call",
     "fields": {"next_action": "junk", "junk_reason": "junk call"},
     "terminal": True},
    {"phrase": "marking junk",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "marking as junk",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "marked junk",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "marked as junk",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "this is junk",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "it is junk",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "junk for us",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "no follow up",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "no followup",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "no follow-up",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "wont be taken",
     "fields": {"next_action": "junk"},
     "terminal": True},
    {"phrase": "will not be taken",
     "fields": {"next_action": "junk"},
     "terminal": True},

    # ---- Cratio convention: "yet to talk" ----
    # In Cratio's stage labels, "Yet To Talk" means "first contact attempt made,
    # didn't connect — will try again". So when Pratibha types this:
    #   - call_attempts = 1 (already tried once)
    #   - next_action = "call" (will retry)
    #   - customer_response_status = "no_answer"
    # The next_touch +1 day cadence is handled separately in tools.NEXT_DAY_PATTERNS
    # so the lead resurfaces tomorrow morning, not in 2 days.
    {"phrase": "yet to talk",
     "fields": {"call_attempts": 1, "customer_response_status": "no_answer",
                "next_action": "call"},
     "terminal": False},

    # ---- "Didn't pick up" / similar → call attempted but no answer ----
    # We intentionally do NOT set call_attempts here because the count varies;
    # we only mark the response status.
    {"phrase": "didnt picked up",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "didn't picked up",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "didnt pickup",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "didnt picked",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "didn't pick",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "not picked up",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "not picking",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "not picking up",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "not attend",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "no response after",
     "fields": {"customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "didnt responded",
     "fields": {"customer_response_status": "awaiting"},
     "terminal": False},
    {"phrase": "didn't responded",
     "fields": {"customer_response_status": "awaiting"},
     "terminal": False},
    {"phrase": "did not responded",
     "fields": {"customer_response_status": "awaiting"},
     "terminal": False},
    {"phrase": "did not respond",
     "fields": {"customer_response_status": "awaiting"},
     "terminal": False},

    # ---- Catalog-only sent (no price quoted yet) ----
    {"phrase": "sent catalog",
     "fields": {"price_quoted_inr": 0},
     "terminal": False},
    {"phrase": "send catalog",
     "fields": {"price_quoted_inr": 0},
     "terminal": False},
    {"phrase": "sent the catalog",
     "fields": {"price_quoted_inr": 0},
     "terminal": False},
    {"phrase": "sent catalog of",
     "fields": {"price_quoted_inr": 0},
     "terminal": False},
    {"phrase": "catalog only",
     "fields": {"price_quoted_inr": 0},
     "terminal": False},
    {"phrase": "only catalog",
     "fields": {"price_quoted_inr": 0},
     "terminal": False},

    # ---- "Not called" / "no" variants — explicitly call_attempts=0 ----
    {"phrase": "not called",
     "fields": {"call_attempts": 0},
     "terminal": False},
    {"phrase": "didnt called",
     "fields": {"call_attempts": 0},
     "terminal": False},
    {"phrase": "didn't called",
     "fields": {"call_attempts": 0},
     "terminal": False},
    {"phrase": "didnt call",
     "fields": {"call_attempts": 0},
     "terminal": False},
    {"phrase": "didn't call",
     "fields": {"call_attempts": 0},
     "terminal": False},
    {"phrase": "didnot called",
     "fields": {"call_attempts": 0},
     "terminal": False},
    {"phrase": "have to call",
     "fields": {"call_attempts": 0, "next_action": "call"},
     "terminal": False},

    # ---- Whatsapp / message-sent variants → next_action = "message" ----
    {"phrase": "sent message on whatsapp",
     "fields": {"next_action": "whatsapp", "customer_response_status": "awaiting"},
     "terminal": False},
    {"phrase": "send message on whatsapp",
     "fields": {"next_action": "whatsapp"},
     "terminal": False},
    {"phrase": "messaged on whatsapp",
     "fields": {"next_action": "whatsapp", "customer_response_status": "awaiting"},
     "terminal": False},
    {"phrase": "whatsapp kar diya",
     "fields": {"next_action": "whatsapp", "customer_response_status": "awaiting"},
     "terminal": False},

    # ---- Multiple call attempts language ----
    {"phrase": "called multiple times",
     "fields": {"call_attempts": 3, "customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "tried multiple times",
     "fields": {"call_attempts": 3, "customer_response_status": "no_answer"},
     "terminal": False},
    {"phrase": "called many times",
     "fields": {"call_attempts": 3, "customer_response_status": "no_answer"},
     "terminal": False},
]


def apply_glossary(answer: str) -> tuple[dict, bool]:
    """Scan `answer` for any glossary phrases and return:
      - merged fields dict (later phrases override earlier on same field)
      - terminal flag (True if any matching phrase is marked terminal)

    Returns ({}, False) when no phrases match.
    """
    if not answer:
        return {}, False
    a = answer.lower()
    merged_fields: dict = {}
    terminal = False
    for rule in PHRASE_RULES:
        if rule["phrase"] in a:
            for k, v in rule["fields"].items():
                # Don't let a later non-terminal rule overwrite an already-set
                # field with a different value (rule order is authority).
                if k not in merged_fields:
                    merged_fields[k] = v
            if rule.get("terminal"):
                terminal = True
    return merged_fields, terminal


def is_terminal(answer: str) -> bool:
    """Convenience: True if any glossary phrase in `answer` is terminal."""
    _, t = apply_glossary(answer)
    return t
