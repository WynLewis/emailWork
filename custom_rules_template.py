"""
Custom rule-based extraction module for CLO deal emails — TEMPLATE.
====================================================================

NOTE: my_rules.py is the working implementation with full extraction
logic for BNP, Citi, Barclays, GS, SMBC email formats. This file is
a blank template kept for reference. If you need to customize, edit
my_rules.py directly or copy it and modify.

HOW TO USE:
    1. Set the env var:  CLO_RULES_MODULE=my_rules.py
       (or add to .env:  CLO_RULES_MODULE=my_rules.py)
    2. Set the extractor: CLO_EXTRACTOR=rules
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
    deal_type                           "BSL" | "MM" | "PC" | "Infra" | "EM"
    target_par_mm                       Target par in millions (number)
    term                                e.g. "5nc2", "3nc1"
    announced_date                      YYYY-MM-DD
    priced_date                         YYYY-MM-DD
    ipt                                 Initial price talk (tranche-level)
    updated_guidance                    Revised spread guidance
    final_pricing                       Final tranche-level pricing
    transaction_type                    "New Issue" | "Reset" | "Refinancing" | "Re-Issue"

    CSV column names are also accepted (see my_rules.py for the full mapping):
    Title, Collateral Manager, Placement Agent, Transaction Type, Status, Term, etc.
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

    # Add your custom rules here.
    # See my_rules.py for a working implementation.

    return result
