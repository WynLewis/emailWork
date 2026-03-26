"""
Custom rule-based extraction module for CLO deal emails.
=========================================================

HOW TO USE:
    1. Copy this file:  cp custom_rules_template.py my_rules.py
    2. Edit my_rules.py with your extraction logic
    3. Set the env var:  CLO_RULES_MODULE=my_rules.py
       (or add to .env:  CLO_RULES_MODULE=my_rules.py)
    4. Set the extractor: CLO_EXTRACTOR=rules
       (or add to .env:   CLO_EXTRACTOR=rules)

HOW IT WORKS:
    The RuleBasedExtractor runs in this order:
        1. Built-in HTML table parser (handles structured HTML tables)
        2. Built-in tranche pricing parser (formats IPT/guidance/pricing)
        3. Heuristic email type detection (announced/updated/priced)
        4. YOUR extract_from_email() function (overrides any of the above)

    Your function only needs to return fields you want to set or override.
    Fields you don't return will keep the built-in parser's values.

AVAILABLE FIELDS (return any subset of these):
    email_type                          "announced" | "updated" | "priced"
    deal_name                           Full deal legal name
    collateral_manager_legal_entity     Legal entity of manager
    collateral_manager_short            Short name (e.g. "PGIM", "Ares")
    arranger                            Investment bank arranging the deal
    deal_type                           "BSL" | "MM" | "Euro BSL" | "Euro MM" | "Other"
    target_par_mm                       Target par in millions (number)
    reinvestment_period                 e.g. "5 years"
    non_call_period                     e.g. "2 years"
    stated_maturity                     e.g. "April 2037"
    warehouse_provider                  Bank providing warehouse
    trustee                             e.g. "U.S. Bank"
    announced_date                      YYYY-MM-DD
    priced_date                         YYYY-MM-DD
    ipt                                 Initial price talk (tranche-level)
    updated_guidance                    Revised spread guidance
    final_pricing                       Final tranche-level pricing
    transaction_type                    "New Issue" | "Reset" | "Refi"

TRAINING DATA:
    Everything you accept through the GUI (notebook or web) is still saved
    to data/training_examples.jsonl. When you later enable the HuggingFace
    model, you can fine-tune it on all the corrections you made while using
    rule-based mode. This means your work computer time isn't wasted — it's
    building training data for the AI.
"""

import re


def extract_from_email(email_text: str) -> dict:
    """
    Extract CLO deal fields from email text using custom rules.

    Parameters:
        email_text: Raw email content (HTML or plain text).

    Returns:
        Dict with field names as keys. Only include fields your rules
        can extract — missing fields will use the built-in parser's values.
    """
    result = {}

    # -----------------------------------------------------------------------
    # ADD YOUR CUSTOM RULES BELOW
    # -----------------------------------------------------------------------
    # Each rule extracts a specific field using regex, keyword matching,
    # or any Python logic you want. Return only the fields you can extract.
    #
    # The built-in HTML parser already handles most structured emails well.
    # Focus your custom rules on patterns specific to YOUR emails that the
    # built-in parser misses.
    # -----------------------------------------------------------------------

    # --- Example: Extract deal name from subject line ---
    # m = re.search(r'Subject:\s*(?:RE:\s*)?(?:FW:\s*)?(.+?)(?:\s*-\s*(?:New Issue|Updated|Priced))', email_text, re.IGNORECASE)
    # if m:
    #     result["deal_name"] = m.group(1).strip()

    # --- Example: Extract manager from a specific format ---
    # m = re.search(r'(?:Managed|Sponsored)\s+by[:\s]+(.+?)(?:\n|<)', email_text, re.IGNORECASE)
    # if m:
    #     result["collateral_manager_legal_entity"] = m.group(1).strip()

    # --- Example: Extract arranger from footer ---
    # m = re.search(r'(?:Arranged|Structured)\s+by[:\s]+(.+?)(?:\n|<)', email_text, re.IGNORECASE)
    # if m:
    #     result["arranger"] = m.group(1).strip()

    # --- Example: Override deal type based on keywords ---
    # lower = email_text.lower()
    # if "middle market" in lower:
    #     result["deal_type"] = "MM"
    # elif "broadly syndicated" in lower:
    #     result["deal_type"] = "BSL"

    return result
