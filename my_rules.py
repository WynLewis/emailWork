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

def _normalize_term(rp: str, nc: str) -> str:
    """Normalize term to standard format like 5nc2, 3nc1, 0nc6m."""
    def _fmt(val: str) -> str:
        val = val.strip()
        # Already has month suffix
        if val.lower().endswith('m'):
            return val.lower()
        try:
            n = float(val)
            # Express as integer if whole number
            if n == int(n):
                return str(int(n))
            return val
        except ValueError:
            return val
    return f"{_fmt(rp)}nc{_fmt(nc)}"


def _months_between(date_str: str, ref_year: int = 2026, ref_month: int = 4) -> Optional[float]:
    """Estimate months from an approximate closing date to a target date.
    Returns years (rounded to 0.25) or None."""
    from datetime import datetime
    for fmt in ['%B %d, %Y', '%b %d, %Y', '%m/%d/%Y', '%Y-%m-%d']:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            # Approximate closing as ref_year April (common CLO closing month)
            closing = datetime(ref_year, ref_month, 15)
            diff_months = (dt.year - closing.year) * 12 + (dt.month - closing.month)
            years = diff_months / 12.0
            if years < 0:
                return None
            # Round to nearest 0.25
            return round(years * 4) / 4
        except ValueError:
            continue
    return None


def _extract_term(text: str) -> Optional[str]:
    """Extract term in RPncCP format from email text (e.g. 5nc2, 3nc1, 0nc6m)."""
    # Direct "5/2 transaction" or "5/2 reset"
    m = re.search(r'(?:^|\s)(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s+(?:transaction|deal|reset|refi)',
                  text, re.I)
    if m:
        return _normalize_term(m.group(1), m.group(2))

    # Already formatted: "3nc1", "5NC2"
    m = re.search(r'(\d+(?:\.\d+)?)\s*[Nn][Cc]\s*(\d+(?:\.\d+)?)', text)
    if m:
        return _normalize_term(m.group(1), m.group(2))

    # Extract from RP/NC period descriptions
    rp = None
    nc = None

    # RP patterns — including bracket notation [3] and "N/A" for static deals
    rp_is_zero = False
    for pat in [
        r'(?i)reinvestment\s+(?:period)?[:\s]+(?:approx\.?\s*)?~?\s*\[?(\d+(?:\.\d+)?)\]?\s*(?:Y|year)',
        r'(?i)~(\d+(?:\.\d+)?)Y\.?\s*(?:RP|reinvestment)',
        r'(?i)(\d+(?:\.\d+)?)(?:y|Y)\.?\s+(?:RP|reinvestment)',
        r'(?i)(\d+(?:\.\d+)?)\s*(?:year|yr)s?\s+reinvestment',
    ]:
        m = re.search(pat, text)
        if m:
            rp = m.group(1)
            break

    # Static deals: "Reinvestment Period: N/A" → RP = 0
    if rp is None and re.search(r'(?i)reinvestment\s+(?:period)?[:\s]+N/?A', text):
        rp = '0'
        rp_is_zero = True

    # RP from exact date: "End of Reinvestment Period: February 25, 2029"
    if rp is None:
        m = re.search(r'(?i)(?:end\s+of\s+)?reinvestment\s+(?:period)?[:\s]+(\w+\s+\d{1,2},?\s+\d{4})', text)
        if m:
            years = _months_between(m.group(1))
            if years is not None:
                rp = str(years)
        # Also try MM/DD/YYYY: "Reinvestment Period (unch): 04/17/2029"
        if rp is None:
            m = re.search(r'(?i)reinvestment\s+(?:period\s*)?(?:\([^)]*\)\s*)?[:\s]+(\d{2}/\d{2}/\d{4})', text)
            if m:
                years = _months_between(m.group(1))
                if years is not None:
                    rp = str(years)

    # NC patterns — including bracket notation [1]
    for pat in [
        r'(?i)non-?call\s+(?:period)?[:\s]+(?:approx\.?\s*)?~?\s*\[?(\d+(?:\.\d+)?)\]?\s*(?:Y|year)',
        r'(?i)~\[?(\d+(?:\.\d+)?)\]?Y\.?\s*(?:NC|non-?call)',
        r'(?i)(\d+(?:\.\d+)?)(?:y|Y)\.?\s+(?:NC|non-?call)',
        r'(?i)(\d+(?:\.\d+)?)\s*(?:year|yr)s?\s+non-?call',
        # Month-based: "6M non-call" or "non-call period: 6 months"
        r'(?i)non-?call\s+(?:period)?[:\s]+(?:approx\.?\s*)?~?\s*(\d+)\s*(?:M(?:onth)?)',
    ]:
        m = re.search(pat, text)
        if m:
            # Check if this is months
            if re.search(r'(?i)month', text[m.start():m.end()+10]):
                nc = m.group(1) + 'm'
            else:
                nc = m.group(1)
            break

    # NC from exact date: "Non-Call Period: 04/17/2027" or "End of Non-Call Period: ~[1] year"
    if nc is None:
        m = re.search(r'(?i)non-?call\s+(?:period\s*)?(?:\([^)]*\)\s*)?[:\s]+(\d{2}/\d{2}/\d{4})', text)
        if m:
            years = _months_between(m.group(1))
            if years is not None:
                nc = str(years)

    if rp and nc:
        return _normalize_term(rp, nc)
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

    # 1b. GS format: "CLO Refi: Allegro CLO XVI, Ltd. -- Announcement"
    m = re.search(
        r'CLO\s+(?:Refi|Reset|New\s+Issue|Re-?Issue)[:\s]+\s*'
        r'([A-Za-z0-9][A-Za-z0-9\s,.\-]+?(?:Ltd|LLC|LP|Corp|Inc)\.?)'
        r'\s*(?:--|–|\u2013|\u2014|$)',
        text, re.I)
    if m:
        return _clean_html_value(m.group(1)).rstrip(',').strip()

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
         r'([A-Za-z0-9][A-Za-z0-9\s,.\-&]{3,80}?(?:LLC|LP|Ltd|Inc|Corp|Company|L\.L\.C\.|L\.P\.|Fund|BDC)\.?)', 0),
        # GS/Barclays: "Collateral Manager:    AXA Investment Managers US Inc"
        (r'Collateral\s+Manager:\s+'
         r'([A-Za-z0-9][A-Za-z0-9\s,.\-&]{3,80}?(?:LLC|LP|Ltd|Inc|Corp|Company|L\.L\.C\.|L\.P\.|Fund|BDC)\.?)', re.I),
        # "managed by <Entity, LLC>" with entity suffix — stop at the suffix
        (r'(?:managed|sponsored)\s+by\s+'
         r'([A-Za-z0-9][A-Za-z0-9\s,.\-&]{3,80}?(?:LLC|LP|Ltd|Inc|Corp|Company|L\.L\.C\.|L\.P\.|Fund|BDC)\.?)', re.I),
        # "engaged by <Entity> (the "Manager")" — legal boilerplate (handles smart quotes)
        (r'engaged\s+by\s+([A-Za-z0-9][^(]{3,80}?)\s*\([\s""\u201c]*(?:the\s+)?["\u201c]?(?:Manager|Collateral\s+Manager)', re.I),
        # "engaged by <Entity> ("Manager")" — variant with direct quote
        (r'engaged\s+by\s+([A-Za-z0-9][^(]{3,80}?)\s*\(["\u201c]Manager', re.I),
        # "<Name> (the "Manager")" — entity name just before the parenthetical
        (r'(?:by\s+)([A-Z0-9][A-Za-z0-9\s,.\-&]{3,80}?)\s*\(["\u201c](?:the\s+)?(?:Manager|Collateral\s+Manager)', re.I),
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

def _extract_transaction_type(text: str) -> str:
    """Determine transaction type. Defaults to 'New Issue' when no other type detected."""
    # Order matters: check specific before generic
    if re.search(r'(?i)re-?issue', text):
        return "Re-Issue"
    if re.search(r'(?i)refinanc|partial\s+refi|\brefi\b', text):
        return "Refinancing"
    if re.search(r'(?i)\breset\b', text):
        return "Reset"
    # Default: if no reset/refi/re-issue found, it's a new issue
    return "New Issue"


# ---------------------------------------------------------------------------
# Status extraction
# ---------------------------------------------------------------------------

def _extract_status(text: str) -> str:
    """Determine email status/type."""
    lower = text.lower()
    if any(kw in lower for kw in ['has priced', 'pricing notification', 'final pricing',
                                   'final spread', 'transaction has priced']):
        return "Priced"
    if re.search(r'(?:deal|transaction|offering)\s+(?:has\s+been\s+)?(?:cancelled|withdrawn|postponed)', lower):
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

    Returns dict with field names as keys. Also includes a '_confidence'
    dict mapping field names to confidence levels:
        'high'   — strong regex match on structured text (DEAL NAME:, MANAGER:, etc.)
        'medium' — matched from legal boilerplate or derived from other fields
        'low'    — fallback/default value or inferred from sender domain
    """
    result: Dict[str, Any] = {}
    confidence: Dict[str, str] = {}

    visible = _to_text(email_text)
    combined = f"{visible}\n{email_text}"

    # --- Deal name ---
    deal_name = _extract_deal_name(visible, email_text)
    if deal_name:
        result['deal_name'] = deal_name
        result['title'] = deal_name
        # Confidence: structured label gets high, bold/subject gets medium
        if re.search(r'DEAL\s*NAME[:\s]', visible, re.I) or re.search(r'CLO\s+(?:Refi|Reset)', visible, re.I):
            confidence['deal_name'] = 'high'
        elif re.search(r'(?:Refinancing|Reset)\s+of\s+', email_text, re.I):
            confidence['deal_name'] = 'high'
        else:
            confidence['deal_name'] = 'medium'
    else:
        confidence['deal_name'] = 'missing'

    # --- Collateral manager ---
    manager = _extract_manager(visible, email_text)
    if manager:
        result['collateral_manager_legal_entity'] = manager
        short = _lookup_manager_short(manager)
        if short:
            result['collateral_manager_short'] = short
            confidence['collateral_manager_short'] = 'high'  # from CSV catalog
        else:
            confidence['collateral_manager_short'] = 'missing'
        # Confidence based on extraction method
        if re.search(r'(?:MANAGER|Collateral\s+Manager):', visible, re.I):
            confidence['collateral_manager_legal_entity'] = 'high'
        elif re.search(r'(?:managed|sponsored)\s+by\s+', visible, re.I):
            confidence['collateral_manager_legal_entity'] = 'high'
        else:
            confidence['collateral_manager_legal_entity'] = 'medium'
    else:
        confidence['collateral_manager_legal_entity'] = 'missing'
        confidence['collateral_manager_short'] = 'missing'

    # --- Collateral type ---
    ctype = _extract_collateral_type(visible)
    result['deal_type'] = ctype
    result['collateral_type'] = ctype
    if ctype == 'BSL' and not re.search(r'(?i)senior\s+secured|bank\s+loan|broadly\s+syndicated|BSL', visible):
        confidence['collateral_type'] = 'low'  # defaulted to BSL
    else:
        confidence['collateral_type'] = 'high'

    # --- Transaction type ---
    txn_type = _extract_transaction_type(visible)
    if txn_type:
        result['transaction_type'] = txn_type
        if txn_type == 'New Issue' and not re.search(r'(?i)new\s+issue', visible):
            confidence['transaction_type'] = 'low'  # defaulted
        else:
            confidence['transaction_type'] = 'high'

    # --- Status ---
    result['email_type'] = _extract_status(visible).lower()
    if result['email_type'] in ('priced',):
        confidence['status'] = 'high'
    elif re.search(r'(?i)announc|roller', visible):
        confidence['status'] = 'high'
    else:
        confidence['status'] = 'medium'

    # --- Placement agent ---
    bank_patterns = [
        r'(BNP Paribas|JPMorgan|Jefferies|Bank of America|Citigroup|Morgan Stanley|'
        r'Goldman Sachs|Wells Fargo|Barclays|Deutsche Bank|CIBC|Scotia|Natixis|RBC|'
        r'SMBC|Nomura|Santander|Mizuho|Capital One|BMO|Atlas|Societe Generale|'
        r'GreensLedge|Mitsubishi)\s+has\s+priced',
    ]
    agent = _first_match(combined, bank_patterns)
    if agent:
        confidence['arranger'] = 'high'
    if not agent:
        agent = _agent_from_legal_text(visible)
        if agent:
            confidence['arranger'] = 'medium'
    if not agent:
        domain = _extract_sender_domain(email_text)
        if domain:
            agent = _agent_from_domain(domain)
            if agent:
                confidence['arranger'] = 'low'
    if agent:
        result['arranger'] = agent
    else:
        confidence['arranger'] = 'missing'

    # --- Term ---
    term = _extract_term(visible)
    if term:
        result['term'] = term
        # Direct "5/2" or "3nc1" → high, derived from RP/NC periods → medium
        if re.search(r'(\d+)\s*/\s*(\d+)\s+(?:transaction|deal|reset|refi)', visible, re.I) or \
           re.search(r'\d+\s*[Nn][Cc]\s*\d+', visible):
            confidence['term'] = 'high'
        else:
            confidence['term'] = 'medium'
    else:
        confidence['term'] = 'missing'

    # --- Dates ---
    filename = os.getenv('CLO_EMAIL_FILENAME', '')
    m = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if m:
        date_str = m.group(1)
        status = result.get('email_type', '')
        if status == 'priced':
            result['priced_date'] = date_str
            confidence['priced_date'] = 'medium'
        else:
            result['announced_date'] = date_str
            confidence['announced_date'] = 'medium'

    # Store confidence dict
    result['_confidence'] = confidence

    # --- Map template fields → SharePoint CSV column names ---
    _map_to_csv_columns(result)

    return result


# ---------------------------------------------------------------------------
# CSV column mapping and catalog enrichment
# ---------------------------------------------------------------------------
# Maps extraction template keys to SharePoint list column names so downstream
# code can write directly into clo-deals, clo-managers, clo-transactions.

DEALS_COLUMNS = [
    'Title', 'Collateral Type', 'Collateral Manager',
    'Bloomberg Deal Name', 'Intex Deal', 'Intex Preprice', 'Deal Documents'
]
MANAGERS_COLUMNS = [
    'Name', 'Short Name', 'Ultimate Parent', 'CRD Number', 'SEC Number', 'Website'
]
TRANSACTIONS_COLUMNS = [
    'Engaged', 'Executed', 'Deal', 'IPT', 'Final Pricing Details', 'Placement Agent',
    'Term', 'Transaction Type', 'Status', 'Announcement Date', 'Priced Date',
    'Title', 'Collateral Type', 'Collateral Manager'
]


def _map_to_csv_columns(result: dict) -> None:
    """Add SharePoint CSV column names alongside template field names."""
    # Deals columns
    if result.get('deal_name'):
        result.setdefault('Title', result['deal_name'])
        result.setdefault('Deal', result['deal_name'])
    if result.get('collateral_manager_legal_entity'):
        result.setdefault('Collateral Manager', result['collateral_manager_legal_entity'])
        result.setdefault('Name', result['collateral_manager_legal_entity'])
    if result.get('collateral_type'):
        result.setdefault('Collateral Type', result['collateral_type'])
    if result.get('collateral_manager_short'):
        result.setdefault('Short Name', result['collateral_manager_short'])

    # Transaction columns
    if result.get('arranger'):
        result.setdefault('Placement Agent', result['arranger'])
    if result.get('transaction_type'):
        result.setdefault('Transaction Type', result['transaction_type'])
    if result.get('email_type'):
        status = result['email_type']
        result.setdefault('Status', status.capitalize() if status != 'priced' else 'Priced')
    if result.get('term'):
        result.setdefault('Term', result['term'])
    if result.get('priced_date'):
        result.setdefault('Priced Date', result['priced_date'])
    if result.get('announced_date'):
        result.setdefault('Announcement Date', result['announced_date'])

    # Booleans default to False
    result.setdefault('Engaged', False)
    result.setdefault('Executed', False)

    # Enrich from manager catalog (fills Short Name, Ultimate Parent, etc.)
    _enrich_from_manager_catalog(result)

    # Enrich from deals catalog (fills Bloomberg Deal Name, Intex IDs, etc.)
    _enrich_from_deals_catalog(result)

    # Backfill all CSV columns with empty string if not set
    for col in DEALS_COLUMNS + MANAGERS_COLUMNS + TRANSACTIONS_COLUMNS:
        result.setdefault(col, '')


def _enrich_from_manager_catalog(fields: dict) -> None:
    """Fill manager metadata (Short Name, Ultimate Parent, etc.) from CSV catalog."""
    mgr = fields.get('Collateral Manager')
    if not mgr:
        return
    rows = _read_managers_csv()
    if not rows:
        return
    mgr_lower = mgr.strip().lower()
    for r in rows:
        if (r.get('Name') or '').strip().lower() == mgr_lower:
            for col in MANAGERS_COLUMNS:
                if col == 'Name':
                    continue  # don't overwrite
                fields.setdefault(col, (r.get(col) or '').strip())
            break


def _enrich_from_deals_catalog(fields: dict) -> None:
    """Fill deal metadata (Bloomberg Deal Name, Intex IDs) from CSV catalog."""
    title = fields.get('Title')
    if not title:
        return
    path = os.path.join(os.path.dirname(__file__), 'data', 'csv', 'clo-deals.csv')
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            title_lower = ' '.join(title.split()).lower()
            for r in reader:
                cand = ' '.join((r.get('Title') or '').split()).lower()
                if cand == title_lower:
                    for col in DEALS_COLUMNS:
                        if col in ('Title', 'Collateral Type', 'Collateral Manager'):
                            continue  # already set
                        fields.setdefault(col, (r.get(col) or '').strip())
                    break
    except FileNotFoundError:
        pass
