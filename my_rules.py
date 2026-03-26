"""
Custom rule-based extraction module for CLO deal emails.
=========================================================

Extracts deal fields from email HTML using regex patterns matched against
real email formats from BNP Paribas, Citigroup, Barclays, Goldman Sachs,
SMBC, and other CLO arrangers.

Loads extraction_rules.yaml for YAML-driven pattern matching, then applies
additional heuristics for fields the YAML can't handle (term derivation,
sender-domain fallback for placement agent, etc.).

HOW TO USE:
    Set env vars:
        CLO_RULES_MODULE=my_rules.py
        CLO_EXTRACTOR=rules
"""

import os
import re
import csv
from typing import Optional, Dict, Any, List

try:
    import yaml
except ImportError:
    yaml = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_text(html: str) -> str:
    """Strip HTML tags and entities to get visible text."""
    if BeautifulSoup:
        try:
            return BeautifulSoup(html, 'html.parser').get_text(separator=' ', strip=True)
        except Exception:
            pass
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&#\d+;', ' ', text)
    return ' '.join(text.split())


def _load_yaml() -> dict:
    path = os.getenv('CLO_YAML_RULES',
                      os.path.join(os.path.dirname(__file__), 'extraction_rules.yaml'))
    if yaml is None:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _first_match(text: str, patterns: list, flags=re.IGNORECASE | re.DOTALL) -> Optional[str]:
    """Try patterns in order, return first capture group 1 (or group 0)."""
    for pat in patterns:
        try:
            m = re.search(pat, text, flags)
            if m:
                return (m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)).strip()
        except re.error:
            continue
    return None


def _clean_html_value(val: str) -> str:
    """Remove &nbsp; and excess whitespace from an extracted value."""
    val = re.sub(r'&nbsp;', ' ', val)
    val = re.sub(r'<[^>]+>', '', val)
    return ' '.join(val.split()).strip()


def _read_managers_csv() -> List[Dict[str, str]]:
    path = os.getenv('CLO_CSV_MANAGERS',
                      os.path.join(os.path.dirname(__file__), 'data', 'csv', 'clo-managers.csv'))
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# Sender-domain → Placement Agent lookup
# ---------------------------------------------------------------------------

_DOMAIN_TO_AGENT = {
    "bnpparibas.com": "BNP Paribas",
    "us.bnpparibas.com": "BNP Paribas",
    "jpmorgan.com": "JPMorgan",
    "jpmchase.com": "JPMorgan",
    "jefferies.com": "Jefferies",
    "bofa.com": "Bank of America",
    "baml.com": "Bank of America",
    "citi.com": "Citigroup",
    "morganstanley.com": "Morgan Stanley",
    "gs.com": "Goldman Sachs",
    "barclays.com": "Barclays",
    "db.com": "Deutsche Bank",
    "cibc.com": "CIBC",
    "scotiabank.com": "Scotia",
    "natixis.com": "Natixis",
    "rbccm.com": "RBC",
    "smbcnikko-si.com": "SMBC",
    "smbcgroup.com": "SMBC",
    "nomura.com": "Nomura",
    "santander.com": "Santander",
    "mizuho-sc.com": "Mizuho",
    "mizuhogroup.com": "Mizuho",
    "capitalone.com": "Capital One",
    "bmo.com": "BMO",
    "bmocm.com": "BMO",
    "wellsfargo.com": "Wells Fargo",
    "greensledge.com": "GreensLedge",
    "sgcib.com": "Societe Generale",
    "atlas-sp.com": "Atlas",
    "sc.mufg.jp": "Mitsubishi",
}

# Also handle BNPPSC → BNP Paribas in legal text
_LEGAL_NAME_TO_AGENT = {
    "BNPPSC": "BNP Paribas",
    "BNP Paribas Securities Corp": "BNP Paribas",
    "Citigroup Global Markets": "Citigroup",
    "J.P. Morgan Securities": "JPMorgan",
    "Goldman Sachs & Co": "Goldman Sachs",
    "Barclays Capital": "Barclays",
    "SMBC Nikko": "SMBC",
    "Morgan Stanley & Co": "Morgan Stanley",
}


def _extract_sender_domain(text: str) -> Optional[str]:
    """Extract sender email domain from Nationwide security header or From: line."""
    m = re.search(r'(?:Sender|From)[:\s]*[^@]*?([a-zA-Z0-9._%+-]+@([a-zA-Z0-9.-]+))', text)
    if m:
        return m.group(2).lower()
    return None


def _agent_from_domain(domain: str) -> Optional[str]:
    """Map sender domain to placement agent name."""
    if not domain:
        return None
    for suffix, agent in _DOMAIN_TO_AGENT.items():
        if domain == suffix or domain.endswith('.' + suffix):
            return agent
    return None


def _agent_from_legal_text(text: str) -> Optional[str]:
    """Find placement agent from legal boilerplate."""
    for legal_name, agent in _LEGAL_NAME_TO_AGENT.items():
        if legal_name in text:
            return agent
    return None


# ---------------------------------------------------------------------------
# Term extraction
# ---------------------------------------------------------------------------

def _extract_term(text: str) -> Optional[str]:
    """Extract term in RPncCP format from email text."""
    # Direct "5/2 transaction" or "5/2 reset"
    m = re.search(r'(?:^|\s)(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s+(?:transaction|deal|reset|refi)',
                  text, re.I)
    if m:
        return f"{m.group(1)}nc{m.group(2)}"

    # Already formatted: "3nc1", "5NC2"
    m = re.search(r'(\d+(?:\.\d+)?)\s*[Nn][Cc]\s*(\d+(?:\.\d+)?)', text)
    if m:
        return f"{m.group(1)}nc{m.group(2)}"

    # Extract from RP/NC period descriptions
    rp = None
    nc = None
    for pat in [
        r'(?i)reinvestment\s+(?:period)?[:\s]+(?:approx\.?\s*)?~?\s*(\d+(?:\.\d+)?)\s*(?:Y|year)',
        r'(?i)~(\d+(?:\.\d+)?)Y\.?\s*(?:RP|reinvestment)',
        r'(?i)(\d+(?:\.\d+)?)(?:y|Y)\.?\s+(?:RP|reinvestment)',
        r'(?i)(\d+(?:\.\d+)?)\s*(?:year|yr)s?\s+reinvestment',
    ]:
        m = re.search(pat, text)
        if m:
            rp = m.group(1)
            break

    for pat in [
        r'(?i)non-?call\s+(?:period)?[:\s]+(?:approx\.?\s*)?~?\s*(\d+(?:\.\d+)?)\s*(?:Y|year)',
        r'(?i)~(\d+(?:\.\d+)?)Y\.?\s*(?:NC|non-?call)',
        r'(?i)(\d+(?:\.\d+)?)(?:y|Y)\.?\s+(?:NC|non-?call)',
        r'(?i)(\d+(?:\.\d+)?)\s*(?:year|yr)s?\s+non-?call',
    ]:
        m = re.search(pat, text)
        if m:
            nc = m.group(1)
            break

    if rp and nc:
        return f"{rp}nc{nc}"
    return None


# ---------------------------------------------------------------------------
# Deal name extraction
# ---------------------------------------------------------------------------

def _extract_deal_name(text: str, html: str) -> Optional[str]:
    """Extract deal name from email using multiple format patterns."""
    # 1. Citi/GS/SMBC structured: "DEAL NAME:   CARLYLE US CLO 2024-1, LTD."
    m = re.search(
        r'DEAL\s*NAME[:\s]+'
        r'([A-Z][A-Za-z0-9\s,.\-]+?(?:Ltd|LLC|LP|Corp|Inc)\.?)',
        text, re.I)
    if m:
        return _clean_html_value(m.group(1))

    # 2. BNP bold: "Refinancing of AIMCO CLO Series 2018-B"
    #    Match up to end-of-line, comma, HTML tag, or "managed by"
    m = re.search(
        r'(?:Refinancing|Reset|Re-?Issue|New\s+Issue)\s+of\s+'
        r'([A-Za-z0-9][A-Za-z0-9\s\-]+?(?:CLO|Loan)[A-Za-z0-9\s\-,]*?)'
        r'(?:\s*[<,]|\s*managed|\s*the\s+["\u201c]|\s*\(|\s*$)',
        html, re.I)
    if m:
        return _clean_html_value(m.group(1)).rstrip(',').strip()

    # 3. "Announcement - Elmwood CLO 28 Partial Refi"
    m = re.search(
        r'Announcement\s*[-\u2013\u2014]\s*'
        r'([A-Za-z0-9][A-Za-z0-9\s]+CLO[A-Za-z0-9\s\-]*)',
        text, re.I)
    if m:
        name = _clean_html_value(m.group(1))
        name = re.sub(r'\s+(?:Partial\s+)?(?:Refi|Reset|Re-?Issue).*$', '', name, flags=re.I)
        return name

    # 4. Subject: "ANNOUNCING CARLYLE 2024-1 RESET" (less precise, use as fallback)
    m = re.search(
        r'(?:ANNOUNCING|Announcing)\s+([A-Za-z0-9][A-Za-z0-9\s\-]+?)\s+'
        r'(?:RESET|Reset|REFI|Refi|REFINANCING|NEW\s+ISSUE)',
        text, re.I)
    if m:
        return _clean_html_value(m.group(1))

    # 5. Bold CLO name in HTML
    m = re.search(r'<b[^>]*>\s*(?:<[^>]+>\s*)*([^<]*CLO[^<]*?)\s*(?:<)', html, re.I)
    if m:
        val = _clean_html_value(m.group(1))
        for prefix in ['Refinancing of ', 'Reset of ', 'Re-Issue of ', 'New Issue of ']:
            if val.lower().startswith(prefix.lower()):
                val = val[len(prefix):]
        return val.strip()

    return None


# ---------------------------------------------------------------------------
# Collateral manager extraction
# ---------------------------------------------------------------------------

def _extract_manager(text: str, html: str) -> Optional[str]:
    """Extract collateral manager from email."""
    patterns = [
        # Citi/GS/SMBC structured: "MANAGER:    CARLYLE CLO MANAGEMENT LLC"
        # Require a colon after MANAGER to avoid matching boilerplate
        (r'MANAGER:\s+'
         r'([A-Za-z0-9][A-Za-z0-9\s,.\-&]{3,80}?(?:LLC|LP|Ltd|Inc|Corp|Company|L\.L\.C\.|L\.P\.)\.?)', 0),
        # "managed by <Entity, LLC>" with entity suffix — stop at the suffix
        (r'(?:managed|sponsored)\s+by\s+'
         r'([A-Za-z0-9][A-Za-z0-9\s,.\-&]{3,80}?(?:LLC|LP|Ltd|Inc|Corp|Company|L\.L\.C\.|L\.P\.)\.?)', re.I),
        # "engaged by <Entity> (the "Manager")" — legal boilerplate
        (r'engaged\s+by\s+([A-Za-z0-9][^(]{3,80}?)\s*\(the\s+["\u201c](?:Manager|Collateral\s+Manager)', re.I),
        # "<Name> (the "Manager")" — entity name just before the parenthetical
        (r'(?:by\s+)([A-Z0-9][A-Za-z0-9\s,.\-&]{3,80}?)\s*\(the\s+["\u201c](?:Manager|Collateral\s+Manager)', re.I),
        # "managed by <Entity>." — fallback without entity suffix
        (r'(?:managed|sponsored)\s+by\s+([A-Za-z0-9][A-Za-z0-9\s,.\-&]{3,60}?)(?:\s*[.(])', re.I),
    ]
    for pat, flags in patterns:
        m = re.search(pat, text, flags)
        if m:
            val = _clean_html_value(m.group(1))
            # Sanity check: manager names shouldn't be absurdly long
            if len(val) > 100:
                continue
            return val
    return None


# ---------------------------------------------------------------------------
# Collateral type extraction
# ---------------------------------------------------------------------------

def _extract_collateral_type(text: str) -> str:
    """Determine collateral type from email text."""
    lower = text.lower()
    if 'middle market' in lower:
        return "MM"
    if 'private credit' in lower or re.search(r'\bPC\b\s+(?:static\s+)?CLO', text):
        return "PC"
    if 'infrastructure' in lower or 'infra clo' in lower:
        return "Infra"
    if 'emerging market' in lower:
        return "EM"
    # Check ASSET TYPE line (Citi format)
    m = re.search(r'ASSET\s+TYPE[:\s]+(.+)', text, re.I)
    if m:
        asset = m.group(1).lower()
        if 'senior secured' in asset or 'bank loan' in asset:
            return "BSL"
        if 'middle market' in asset:
            return "MM"
    return "BSL"


# ---------------------------------------------------------------------------
# Transaction type extraction
# ---------------------------------------------------------------------------

def _extract_transaction_type(text: str) -> Optional[str]:
    """Determine transaction type."""
    # Order matters: check specific before generic
    if re.search(r'(?i)re-?issue', text):
        return "Re-Issue"
    if re.search(r'(?i)refinanc|partial\s+refi|\brefi\b', text):
        return "Refinancing"
    if re.search(r'(?i)\breset\b', text):
        return "Reset"
    if re.search(r'(?i)new\s+issue', text):
        return "New Issue"
    return None


# ---------------------------------------------------------------------------
# Status extraction
# ---------------------------------------------------------------------------

def _extract_status(text: str) -> str:
    """Determine email status/type."""
    lower = text.lower()
    if any(kw in lower for kw in ['has priced', 'pricing notification', 'final pricing',
                                   'final spread', 'transaction has priced']):
        return "Priced"
    if any(kw in lower for kw in ['cancel', 'withdrawn', 'postponed']):
        return "Cancelled"
    if any(kw in lower for kw in ['updated guidance', 'revised guidance', 'revised spread']):
        return "Announced"
    return "Announced"


# ---------------------------------------------------------------------------
# Manager short name lookup
# ---------------------------------------------------------------------------

_MANAGERS_CACHE = None

def _lookup_manager_short(legal_name: str) -> Optional[str]:
    """Look up manager short name from CSV catalog."""
    global _MANAGERS_CACHE
    if _MANAGERS_CACHE is None:
        _MANAGERS_CACHE = _read_managers_csv()
    if not _MANAGERS_CACHE or not legal_name:
        return None
    legal_lower = legal_name.strip().lower()
    for row in _MANAGERS_CACHE:
        name = (row.get('Name') or '').strip()
        if name.lower() == legal_lower:
            return (row.get('Short Name') or '').strip() or None
    return None


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def extract_from_email(email_text: str) -> dict:
    """
    Extract CLO deal fields from email text using custom rules.

    Returns dict with field names as keys. Only includes fields
    that were successfully extracted.
    """
    result: Dict[str, Any] = {}

    visible = _to_text(email_text)
    combined = f"{visible}\n{email_text}"

    # --- Deal name ---
    deal_name = _extract_deal_name(visible, email_text)
    if deal_name:
        result['deal_name'] = deal_name
        result['title'] = deal_name

    # --- Collateral manager ---
    manager = _extract_manager(visible, email_text)
    if manager:
        result['collateral_manager_legal_entity'] = manager
        short = _lookup_manager_short(manager)
        if short:
            result['collateral_manager_short'] = short

    # --- Collateral type ---
    ctype = _extract_collateral_type(visible)
    result['deal_type'] = ctype
    result['collateral_type'] = ctype

    # --- Transaction type ---
    txn_type = _extract_transaction_type(visible)
    if txn_type:
        result['transaction_type'] = txn_type

    # --- Status ---
    result['email_type'] = _extract_status(visible).lower()

    # --- Placement agent ---
    # Try explicit text patterns first
    bank_patterns = [
        r'(BNP Paribas|JPMorgan|Jefferies|Bank of America|Citigroup|Morgan Stanley|'
        r'Goldman Sachs|Wells Fargo|Barclays|Deutsche Bank|CIBC|Scotia|Natixis|RBC|'
        r'SMBC|Nomura|Santander|Mizuho|Capital One|BMO|Atlas|Societe Generale|'
        r'GreensLedge|Mitsubishi)\s+has\s+priced',
    ]
    agent = _first_match(combined, bank_patterns)
    # Try legal boilerplate
    if not agent:
        agent = _agent_from_legal_text(visible)
    # Fallback: sender domain
    if not agent:
        domain = _extract_sender_domain(email_text)
        if domain:
            agent = _agent_from_domain(domain)
    if agent:
        result['arranger'] = agent

    # --- Term ---
    term = _extract_term(visible)
    if term:
        result['term'] = term

    # --- Dates ---
    # Try filename-based date
    filename = os.getenv('CLO_EMAIL_FILENAME', '')
    m = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if m:
        date_str = m.group(1)
        status = result.get('email_type', '')
        if status == 'priced':
            result['priced_date'] = date_str
        else:
            result['announced_date'] = date_str

    return result
