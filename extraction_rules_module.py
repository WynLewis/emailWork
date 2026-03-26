"""
Rule-based CLO Email Extraction Module
=====================================
Self-contained module for extracting deal and transaction fields from email content using YAML rules and heuristics.
"""

import re
import yaml
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

# =============================================================================
# LOAD EXTRACTION RULES
# =============================================================================
def load_extraction_rules(rules_path: Path) -> dict:
    with open(rules_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

# =============================================================================
# TEXT EXTRACTION HELPERS
# =============================================================================
def extract_text(html_content: str) -> str:
    """Extract plain text from HTML (fallback: return as-is if not HTML)"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, 'html.parser')
        return soup.get_text(separator=' ', strip=True)
    except ImportError:
        return html_content


def find_all_matches(content: str, patterns: List[str]) -> List[str]:
    """Find all unique matches from a list of patterns"""
    matches = []
    for pattern in patterns:
        try:
            for m in re.finditer(pattern, content, re.IGNORECASE):
                val = m.group(1) if m.groups() else m.group(0)
                val = val.strip()
                val = re.sub(r'&[a-z]+;', ' ', val)
                val = ' '.join(val.split())
                if val and val not in matches and len(val) < 100:
                    matches.append(val)
        except re.error:
            continue
    return matches[:6]  # Max 6 options

# =============================================================================
# MAIN EXTRACTION FUNCTION
# =============================================================================
def extract_from_email(
    email_content: str,
    rules: dict,
    deal_fields: Optional[List[str]] = None,
    txn_fields: Optional[List[str]] = None,
    filename: Optional[str] = None
) -> Dict[str, Dict[str, str]]:
    """
    Extract deal and transaction fields from email content using rules and heuristics.
    Returns: {'deal': {...}, 'transaction': {...}}
    """
    text = extract_text(email_content)
    combined = text + " " + email_content
    deal_data = {}
    txn_data = {}

    # --- Helper for YAML rule extraction ---
    def extract_by_rules(field_type: str, section: str) -> List[str]:
        suggestions = []
        rules_section = rules.get(section, {})
        ruleset = rules_section.get(field_type, {})
        allowed = ruleset.get('allowed_values', []) if isinstance(ruleset, dict) else []
        # Extraction patterns — match against content first
        if 'extraction_patterns' in ruleset:
            for pat in ruleset['extraction_patterns']:
                if isinstance(pat, dict):
                    pattern = pat.get('pattern')
                    group = pat.get('group', 0)
                    value = pat.get('value')
                    value_if_match = pat.get('value_if_match')
                    value_if_no_match = pat.get('value_if_no_match')
                    if pattern:
                        m = re.search(pattern, combined, re.I)
                        if m:
                            if value:
                                suggestions.append(value)
                            elif value_if_match:
                                suggestions.append(value_if_match)
                            elif group:
                                suggestions.append(m.group(int(group)))
                            else:
                                suggestions.append(m.group(0))
                        elif value_if_no_match:
                            suggestions.append(value_if_no_match)
                elif isinstance(pat, str):
                    m = re.search(pat, combined, re.I)
                    if m:
                        suggestions.append(m.group(1) if m.groups() else m.group(0))
        # Validate extracted values against allowed_values if defined
        if allowed and suggestions:
            validated = [s for s in suggestions if s in allowed]
            if validated:
                return validated
        return suggestions

    # --- Deal fields ---
    deal_fields = deal_fields or list(rules.get('deals', {}).keys())
    for field in deal_fields:
        vals = extract_by_rules(field, 'deals')
        deal_data[field] = vals[0] if vals else ''

    # --- Transaction fields ---
    txn_fields = txn_fields or list(rules.get('transactions', {}).keys())
    for field in txn_fields:
        vals = extract_by_rules(field, 'transactions')
        txn_data[field] = vals[0] if vals else ''

    return {'deal': deal_data, 'transaction': txn_data}

# =============================================================================
# MODULE USAGE EXAMPLE
# =============================================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python extraction_rules_module.py <email_file.txt> <extraction_rules.yaml>")
        sys.exit(1)
    email_file = Path(sys.argv[1])
    rules_file = Path(sys.argv[2])
    with open(email_file, 'r', encoding='utf-8') as f:
        email_content = f.read()
    rules = load_extraction_rules(rules_file)
    result = extract_from_email(email_content, rules)
    print("Deal Fields:", result['deal'])
    print("Transaction Fields:", result['transaction'])
