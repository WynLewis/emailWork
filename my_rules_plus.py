
# my_rules_plus.py
# --------------------------------------------------------------------------------------
# Purpose
#   A drop-in rules module like `my_rules.py`, but it also returns **all field names**
#   that exist in your three CSVs (clo-deals.csv, clo-managers.csv, clo-transactions.csv)
#   so downstream code can write directly into those tables without extra mapping.
#
# How it works
#   - Loads your YAML rules (same as my_rules).
#   - Extracts core fields (deal name, manager, type, arranger, status, dates, etc.).
#   - Emits BOTH the template keys AND the CSV schemas' keys.
#   - Optionally enriches values by looking up matches in the local CSV catalogs
#     (if those CSV files are present on disk).
#
# Environment variables (optional)
#   CLO_YAML_RULES       -> path to extraction_rules.yaml (default: './extraction_rules.yaml')
#   CLO_EMAIL_FILENAME   -> filename to help filename-based regex detection (optional)
#   CLO_CSV_DEALS        -> path to clo-deals.csv (default: './clo-deals.csv')
#   CLO_CSV_MANAGERS     -> path to clo-managers.csv (default: './clo-managers.csv')
#
# Return contract (same as template):
#   def extract_from_email(email_text: str) -> dict
#   Return ONLY the fields you want to set/override.
# --------------------------------------------------------------------------------------
from __future__ import annotations
import os
import re
import csv
from typing import Any, Dict, Iterable, List, Optional
from datetime import datetime

# Optional deps
try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

# --------------------------------------------------------------------------------------
# CSV schemas (column names) inferred from the provided files
# --------------------------------------------------------------------------------------
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

# --------------------------------------------------------------------------------------
# HTML → visible text helper
# --------------------------------------------------------------------------------------

def _to_visible_text(email_html_or_text: str) -> str:
    if BeautifulSoup is None:
        return email_html_or_text
    try:
        soup = BeautifulSoup(email_html_or_text, 'html.parser')
        return soup.get_text(separator=' ', strip=True)
    except Exception:
        return email_html_or_text

# --------------------------------------------------------------------------------------
# YAML loading & field extraction helpers (compatible with your prior module)
# --------------------------------------------------------------------------------------

def _load_yaml_rules() -> Dict[str, Any]:
    path = os.getenv('CLO_YAML_RULES', 'extraction_rules.yaml')
    if yaml is None:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _search_first_match(patterns: Iterable[Any], text: str, *, filename: Optional[str] = None) -> Optional[str]:
    if not patterns:
        return None
    for spec in patterns:
        m = None
        source_text = text
        group = None
        value = None
        value_if_match = None
        value_if_no_match = None
        if isinstance(spec, str):
            try:
                m = re.search(spec, source_text, re.IGNORECASE | re.DOTALL)
            except re.error:
                m = None
        elif isinstance(spec, dict):
            patt = spec.get('pattern')
            src = spec.get('source')
            if src == 'filename' and filename:
                source_text = filename
            group = spec.get('group')
            value = spec.get('value')
            value_if_match = spec.get('value_if_match')
            value_if_no_match = spec.get('value_if_no_match')
            try:
                if patt:
                    m = re.search(patt, source_text, re.IGNORECASE | re.DOTALL)
            except re.error:
                m = None
        else:
            continue
        if m:
            if value is not None:
                return str(value).strip()
            if value_if_match is not None:
                return str(value_if_match).strip()
            if group is not None:
                try:
                    g = int(group)
                    return (m.group(g) or '').strip()
                except Exception:
                    return (m.group(0) or '').strip()
            return (m.group(1) if m.groups() else m.group(0)).strip()
        else:
            if isinstance(spec, dict) and value_if_no_match is not None:
                return str(value_if_no_match).strip()
    return None


def _get_field_from_yaml(rules: Dict[str, Any], section: str, field: str,
                         *, text: str, filename: Optional[str]) -> Optional[str]:
    sec = rules.get(section, {}) or {}
    spec = sec.get(field, {}) or {}
    patterns = spec.get('extraction_patterns') or spec.get('extraction\_patterns')
    val = _search_first_match(patterns or [], text, filename=filename)
    if (val is None or val == '') and 'default' in spec:
        defval = spec.get('default')
        if defval is not None:
            val = str(defval)
    return val if (val is not None and str(val).strip() != '') else None

# --------------------------------------------------------------------------------------
# Light transforms
# --------------------------------------------------------------------------------------

def _normalize_entity_suffix(name: str) -> str:
    if not name:
        return name
    suffixes = ['LLC', 'L.P.', 'LP', 'Inc.', 'Inc', 'Ltd.', 'Ltd', 'Corp.', 'Corp']
    name = ' '.join(name.split())
    for s in suffixes:
        name = re.sub(rf"{re.escape(s)}", s.replace('.', ''), name, flags=re.IGNORECASE)
    return name


def _strip_prefixes(text: str, prefixes: List[str]) -> str:
    for p in prefixes:
        if text.lower().startswith(p.lower()):
            return text[len(p):].lstrip()
    return text


def _parse_date(value: str) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.strptime(value, '%Y-%m-%d')
        return dt.strftime('%Y-%m-%d')
    except Exception:
        pass
    fmts = ['%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y', '%b %d,%Y', '%B %d,%Y']
    for f in fmts:
        try:
            dt = datetime.strptime(value, f)
            return dt.strftime('%Y-%m-%d')
        except Exception:
            continue
    return None

# --------------------------------------------------------------------------------------
# Catalog lookups (optional): managers & deals CSVs if present
# --------------------------------------------------------------------------------------

def _read_csv_dict(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    try:
        with open(path, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append({k: (v or '').strip() for k, v in r.items()})
    except FileNotFoundError:
        pass
    return rows


def _enrich_from_manager_catalog(fields: Dict[str, Any]) -> None:
    mgr = fields.get('Collateral Manager')
    if not mgr:
        return
    mgr_path = os.getenv('CLO_CSV_MANAGERS', 'clo-managers.csv')
    rows = _read_csv_dict(mgr_path)
    if not rows:
        return
    # exact case-insensitive match on 'Name'
    for r in rows:
        if r.get('Name', '').strip().lower() == mgr.strip().lower():
            # Fill any missing manager columns
            for col in MANAGERS_COLUMNS:
                key = col
                if key in ('Name',):
                    continue  # don't overwrite
                fields.setdefault(col, r.get(col, ''))
            # also set collateral_manager_short for template if available
            if r.get('Short Name'):
                fields.setdefault('collateral_manager_short', r['Short Name'])
            break


def _enrich_from_deals_catalog(fields: Dict[str, Any]) -> None:
    title = fields.get('Title')
    if not title:
        return
    deals_path = os.getenv('CLO_CSV_DEALS', 'clo-deals.csv')
    rows = _read_csv_dict(deals_path)
    if not rows:
        return
    title_norm = ' '.join(title.split()).lower()
    for r in rows:
        cand = ' '.join((r.get('Title') or '').split()).lower()
        if cand == title_norm:
            for col in DEALS_COLUMNS:
                if col in ('Title', 'Collateral Type', 'Collateral Manager'):
                    continue  # already set
                fields.setdefault(col, r.get(col, ''))
            break

# --------------------------------------------------------------------------------------
# Main entrypoint (compatible with RuleBasedExtractor template)
# --------------------------------------------------------------------------------------

def extract_from_email(email_text: str) -> dict:
    """Return ONLY the fields you want to set or override.

    This function emits:
      (a) Standard template fields used by the engine, and
      (b) All columns from your three CSVs when we can derive them from YAML/heuristics.
    """
    out: Dict[str, Any] = {}

    rules = _load_yaml_rules()
    if not rules:
        return out

    visible = _to_visible_text(email_text)
    combined = f"{visible}\n{email_text}"
    filename = os.getenv('CLO_EMAIL_FILENAME')

    # ---------------------- Core extractions from YAML ----------------------
    # Deal name / Title
    deal_name = _get_field_from_yaml(rules, 'deals', 'title', text=combined, filename=filename)
    if deal_name:
        trans = (rules.get('deals', {}).get('title', {}) or {}).get('transformations', [])
        if isinstance(trans, list):
            for t in trans:
                if isinstance(t, dict) and 'strip_prefixes' in t:
                    deal_name = _strip_prefixes(deal_name, t['strip_prefixes'])
                if isinstance(t, dict) and t.get('trim_whitespace'):
                    deal_name = deal_name.strip()
        out['deal_name'] = deal_name  # template field
        out['Title'] = deal_name      # CSV: deals & transactions
        out.setdefault('Deal', deal_name)  # CSV: transactions

    # Collateral Manager
    mgr = _get_field_from_yaml(rules, 'deals', 'collateral_manager', text=combined, filename=filename)
    if mgr:
        trans = (rules.get('deals', {}).get('collateral_manager', {}) or {}).get('transformations', [])
        if isinstance(trans, list):
            for t in trans:
                if isinstance(t, dict) and t.get('normalize_entity_suffix'):
                    mgr = _normalize_entity_suffix(mgr)
                if isinstance(t, dict) and t.get('trim_whitespace'):
                    mgr = mgr.strip()
        out['collateral_manager_legal_entity'] = mgr  # template
        out['Collateral Manager'] = mgr               # CSV: deals & txns
        out['Name'] = mgr                             # CSV: managers

    # Collateral Type → deal_type (template) + CSV column
    ctype = _get_field_from_yaml(rules, 'deals', 'collateral_type', text=combined, filename=filename)
    if ctype:
        norm = ctype.strip()
        out['Collateral Type'] = norm
        low = norm.lower()
        if 'middle' in low and 'market' in low:
            out['deal_type'] = 'MM'
        elif 'bsl' in low:
            out['deal_type'] = 'BSL'
        elif 'euro' in low:
            out['deal_type'] = 'Euro MM' if 'middle' in low else 'Euro BSL'
        else:
            out['deal_type'] = 'Other'

    # Placement Agent / Arranger
    arranger = _get_field_from_yaml(rules, 'transactions', 'placement_agent', text=combined, filename=filename)
    if arranger:
        out['arranger'] = arranger
        out['Placement Agent'] = arranger

    # Transaction type
    txn_type = _get_field_from_yaml(rules, 'transactions', 'transaction_type', text=combined, filename=filename)
    if txn_type:
        t = txn_type.strip().lower()
        if 'refinanc' in t:
            out['transaction_type'] = 'Refi'
            out['Transaction Type'] = 'Refinancing'
        elif 'reset' in t:
            out['transaction_type'] = 'Reset'
            out['Transaction Type'] = 'Reset'
        elif 're-issue' in t or 'reissue' in t:
            # If you treat re-issue independently, change this mapping
            out['transaction_type'] = 'Reset'
            out['Transaction Type'] = 'Re-Issue'
        else:
            out['transaction_type'] = 'New Issue'
            out['Transaction Type'] = 'New Issue'

    # Status → email_type
    status = _get_field_from_yaml(rules, 'transactions', 'status', text=combined, filename=filename)
    if status:
        out['Status'] = status
        s = status.strip().lower()
        if 'price' in s:
            out['email_type'] = 'priced'
        elif 'announce' in s:
            out['email_type'] = 'announced'
        elif 'updat' in s or 'upcoming' in s:
            out['email_type'] = 'updated'

    # Dates
    priced_date = _get_field_from_yaml(rules, 'transactions', 'priced_date', text=combined, filename=filename)
    if priced_date:
        out['priced_date'] = _parse_date(priced_date) or priced_date
        out['Priced Date'] = out['priced_date']
    ann_date = _get_field_from_yaml(rules, 'transactions', 'announcement_date', text=combined, filename=filename)
    if ann_date:
        out['announced_date'] = _parse_date(ann_date) or ann_date
        out['Announcement Date'] = out['announced_date']

    # Optional: booleans from YAML defaults (engaged/executed)
    engaged = _get_field_from_yaml(rules, 'transactions', 'engaged', text=combined, filename=filename)
    if engaged is not None:
        out['Engaged'] = str(engaged).strip().lower() in ('true','1','yes','y')
    executed = _get_field_from_yaml(rules, 'transactions', 'executed', text=combined, filename=filename)
    if executed is not None:
        out['Executed'] = str(executed).strip().lower() in ('true','1','yes','y')

    # Term (string like '5nc2')
    term = _get_field_from_yaml(rules, 'transactions', 'term', text=combined, filename=filename)
    if term:
        out['Term'] = term

    # IPT and Final Pricing Details (if your YAML provides explicit captures)
    ipt = _get_field_from_yaml(rules, 'transactions', 'ipt', text=combined, filename=filename)
    if ipt:
        out['IPT'] = ipt
    # If you add patterns to your YAML for final_pricing_details, lift them here
    fpd = _get_field_from_yaml(rules, 'transactions', 'final_pricing_details', text=combined, filename=filename)
    if fpd:
        out['Final Pricing Details'] = fpd

    # ---------------------- Optional catalog enrichment ----------------------
    #   * Use manager catalog to fill Short Name, Ultimate Parent, IDs, Website
    #   * Use deals catalog to supply Bloomberg Deal Name / Intex IDs / Docs link
    _enrich_from_manager_catalog(out)
    _enrich_from_deals_catalog(out)

    # ---------------------- Backfill CSV-required fields if missing ---------
    # Ensure all CSV columns exist in the payload (empty string if unknown)
    for col in DEALS_COLUMNS + MANAGERS_COLUMNS + TRANSACTIONS_COLUMNS:
        out.setdefault(col, '')

    # Keep the template core alongside CSV keys
    return out
