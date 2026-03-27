"""
Pluggable LLM extraction for CLO deal emails.
================================================

WHAT THIS FILE DOES:
    Takes raw email text (HTML or plain text) and uses an LLM to extract
    structured deal information (deal name, manager, arranger, pricing, etc.)
    into a JSON object.
#
#
#

ARCHITECTURE — "Strategy Pattern":
    There are multiple extraction backends, all sharing the same interface:

        BaseExtractor  (abstract base class)
            ├── AnthropicExtractor   — Uses Anthropic's Claude API (cloud)
            ├── BedrockExtractor     — Uses AWS Bedrock API (cloud)
            ├── NuExtractExtractor   — Uses local NuExtract model (on your GPU/CPU)
            ├── RuleBasedExtractor   — HTML parsing + custom rules (no LLM needed)
            └── FallbackExtractor    — No LLM; just detects email type heuristically

    The `get_extractor()` factory function creates the right one based on
    the CLO_EXTRACTOR config setting. If the chosen backend fails to load
    (e.g., missing API key, model download fails), it falls back gracefully
    to FallbackExtractor so the app still works.

HOW NuExtract WORKS (the local model):
    NuExtract is an open-source model from NuMind that's specifically designed
    for structured extraction tasks. It uses a special prompt format:

        <|input|>
        ... (email content) ...
        <|template|>
        {"field1": "", "field2": "", ...}
        <|output|>

    The model then generates JSON filling in the template with values from
    the input. The template has EMPTY STRING values — the model fills them in.
    This is different from the cloud LLMs which get type descriptions.

HOW TO ADAPT TO YOUR DATA:
    1. ADDING/REMOVING FIELDS:
       - Edit EXTRACTION_SCHEMA (for Anthropic/Bedrock — uses type descriptions)
       - Edit NUEXTRACT_TEMPLATE (for NuExtract — uses empty string values)
       - Edit ExtractionResult in models.py to match
       - Update the notebook's Cell 2 FORM_FIELDS and _on_accept() to handle the new field

    2. CHANGING THE PROMPT:
       - For cloud LLMs: edit SYSTEM_PROMPT below
       - For NuExtract: the prompt is built in NuExtractExtractor.extract()

    3. USING A DIFFERENT LOCAL MODEL:
       - Change NUEXTRACT_MODEL in config.py
       - The model must be compatible with AutoModelForCausalLM (decoder-only)
       - If it uses a different prompt format, update the `extract()` method

LoRA FINE-TUNING INTEGRATION:
    When a LoRA adapter exists at models/nuextract-lora/, the NuExtractExtractor
    automatically loads and merges it on startup. This adapter contains small
    weight adjustments trained on YOUR corrected extractions, making the model
    better at extracting the specific fields you care about.

    See finetune.py for how the adapter is created.
"""

import json
import logging
import re
from abc import ABC, abstractmethod

from backend import config
from backend.models import ExtractionResult
from backend.field_mapping import load_mapping, get_skipped_fields, get_active_fields, map_extraction_to_gui

logger = logging.getLogger(__name__)


# EXTRACTION SCHEMA — tuned to your CSV fields + pipeline expectations
# Values are TYPE DESCRIPTIONS to guide cloud LLMs (Anthropic, Bedrock, etc.).
# Notes:
#   - Keep tranche strings "verbatim"; post-process for formatting downstream.
#   - Include common aliases (e.g., title/deal_name, arranger/placement_agent).
# -------------------------------------------------------------------------

EXTRACTION_SCHEMA = {
    # ---------------- Core pipeline / status ----------------
    "email_type": "string (announced | updated | priced)",
    # Aligns with your transactions 'Status' but normalized to 3 states above.
    "status": "string (Announced | Priced | Upcoming | Cancelled)",

    # ---------------- Identity (Deals / Transactions) ----------------
    # Prefer 'title' for CSV (Deals.Title / Transactions.Title), but keep deal_name.
    "title": "verbatim-string (deal title as shown in email; preferred for CSV rows)",
    "deal_name": "verbatim-string (canonical name; alias of 'title' if both present)",
    # CSV: Deals.Collateral Type / Transactions.Collateral Type
    "collateral_type": "string (BSL | MM | EM | Infra | PC | MM Rated Feeder | Infra Rated Feeder)",
    # Template: maps to type family for deal classification
    "deal_type": "string (BSL | MM | EM | Infra | PC | MM Rated Feeder | Infra Rated Feeder)",

    # ---------------- Manager (Managers / Deals / Transactions) ----------------
    # CSV: Managers.Name / Deals.Collateral Manager / Transactions.Collateral Manager
    "collateral_manager_legal_entity": "verbatim-string",
    "collateral_manager_short": "string",
    # CSV: Managers extras (populated if available)
    "ultimate_parent": "string (optional)",
    "crd_number": "string (optional)",
    "sec_number": "string (optional)",
    "website": "url-string (optional)",

    # ---------------- Bank / Arranger ----------------
    # Template key used in your pipeline:
    "arranger": "verbatim-string",
    # CSV alias:
    "placement_agent": "verbatim-string (alias of 'arranger')",

    # ---------------- Dates ----------------
    # CSV: Transactions.Announcement Date / Priced Date
    "announced_date": "string (YYYY-MM-DD)",    # maps to 'Announcement Date'
    "priced_date": "string (YYYY-MM-DD)",       # maps to 'Priced Date'",

    # ---------------- Transaction meta (Transactions CSV) ----------------
    "transaction_type": "string (New Issue | Reset | Refinancing | Re-Issue)",
    "term": "verbatim-string (e.g., '5nc2', '3nc1.5', '1.5nc0.5')",
    "engaged": "boolean",
    "executed": "boolean",

    # ---------------- Tranche-level text blocks (keep verbatim) ----------------
    # Preserve ranges, hyphens, and trailing 'a' (e.g., '135a'); exclude “loan tranches” in post-processing, not here.
    "ipt": "verbatim-string (full tranche-level IPT as written; preserve trailing 'a')",
    "updated_guidance": "verbatim-string (if present)",
    "final_pricing": "verbatim-string (full tranche-level final pricing as written; class name first, ratings in parentheses)",

    # ---------------- Deal catalog fields (Deals CSV) ----------------
    "bloomberg_deal_name": "string (optional)",
    "intex_deal": "string (optional)",
    "intex_preprice": "string (optional)",
    "deal_documents": "url-string (optional)",

    # ---------------- Optional extras kept for compatibility ----------------
    "target_par_mm": "number (optional)",
    "reinvestment_period": "verbatim-string (optional)",
    "non_call_period": "verbatim-string (optional)",
    "stated_maturity": "verbatim-string (optional)",
    "warehouse_provider": "verbatim-string (optional)",
    "trustee": "verbatim-string (optional)",
    # If you decide to extract WACC as a separate number instead of in-text:
    "wacc_bps": "number (optional; weighted average DM in basis points)",
}



#-------------------------------------------------------------------------
# NUEXTRACT TEMPLATE — NuExtract requires EMPTY STRING values, not type hints.
# Must have the exact same keys as EXTRACTION_SCHEMA.
# ---------------------------------------------------------------------------
NUEXTRACT_TEMPLATE = {
    # Core pipeline / status
    "email_type": "",
    "status": "",

    # Identity (Deals / Transactions)
    "title": "",
    "deal_name": "",
    "collateral_type": "",
    "deal_type": "",

    # Manager (Managers / Deals / Transactions)
    "collateral_manager_legal_entity": "",
    "collateral_manager_short": "",
    "ultimate_parent": "",
    "crd_number": "",
    "sec_number": "",
    "website": "",

    # Bank / Arranger
    "arranger": "",
    "placement_agent": "",

    # Dates
    "announced_date": "",
    "priced_date": "",

    # Transaction meta
    "transaction_type": "",
    "term": "",
    "engaged": "",
    "executed": "",

    # Tranche-level text blocks (keep verbatim; post-process later)
    "ipt": "",
    "updated_guidance": "",
    "final_pricing": "",

    # Deal catalog fields (deals CSV)
    "bloomberg_deal_name": "",
    "intex_deal": "",
    "intex_preprice": "",
    "deal_documents": "",

    # Optional compatibility/extras
    "target_par_mm": "",
    "reinvestment_period": "",
    "non_call_period": "",
    "stated_maturity": "",
    "warehouse_provider": "",
    "trustee": "",
    "wacc_bps": "",
}


# ---------------------------------------------------------------------------
# SYSTEM PROMPT — Sent to cloud LLMs (Anthropic/Bedrock) as instructions.
# Customize this to improve extraction quality for your specific emails.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a CLO deal email parser. Extract the following fields from the email and return ONLY valid JSON matching this schema. If a field is not present in the email, use null. Do not hallucinate values.

Schema:
""" + json.dumps(EXTRACTION_SCHEMA, indent=2)


def _get_custom_field_names() -> list[str]:
    """
    Return field names from field_mapping.json that are NOT in the default
    ExtractionResult model. These are user-added custom fields.
    """
    try:
        active = get_active_fields()
        default_fields = set(ExtractionResult.model_fields.keys())
        return [f["name"] for f in active if f["name"] not in default_fields]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def detect_email_type(text: str) -> str:
    """
    Heuristic email type detection based on keywords.

    Used as a fallback when no LLM is available, and also to provide
    a quick guess before the LLM runs.

    Returns: "announced", "updated", or "priced"
    """
    lower = text.lower()
    # Check for pricing signals — be specific to avoid false positives from "pricing date"
    if any(kw in lower for kw in ["has priced", "pricing notification", "final spread", "final pricing", "transaction has priced"]):
        return "priced"
    if any(kw in lower for kw in ["updated guidance", "revised guidance", "updated spread", "guidance update", "revised spread"]):
        return "updated"
    return "announced"


def parse_extraction(raw: str) -> dict:
    """
    Parse a JSON object from the LLM's raw text output.

    The LLM might return:
      - Pure JSON: {"field": "value", ...}
      - Markdown code block: ```json\n{...}\n```
      - JSON with trailing text or commentary
      - Truncated JSON (if the model hit max_new_tokens)

    This function handles all of those cases, including attempting to
    recover partial JSON from truncated output.
    """
    # Strategy 1: Look for JSON inside a markdown code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        return json.loads(m.group(1))

    # Strategy 2: Find a complete JSON object {...} anywhere in the text
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        return json.loads(m.group(0))

    # Strategy 3: Try to recover TRUNCATED JSON (model hit max_new_tokens)
    # The output starts with { but never closes — we try to patch it up.
    m = re.search(r"\{.*", raw, re.DOTALL)
    if m:
        partial = m.group(0).rstrip()
        # If there's an unclosed string (odd number of quotes), close it
        if partial.count('"') % 2 == 1:
            partial += '"'
        if not partial.rstrip().endswith("}"):
            # Remove the trailing incomplete key-value pair after the last comma
            last_comma = partial.rfind(",")
            if last_comma > 0:
                partial = partial[:last_comma]
            partial += "}"
        try:
            return json.loads(partial)
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ---------------------------------------------------------------------------
# HTML DEAL FIELD PARSER
# ---------------------------------------------------------------------------
# Extracts structured deal fields directly from the email HTML tables.
# More reliable than the LLM for emails with structured HTML tables
# (e.g., "Deal Summary" tables with label/value rows).
# ---------------------------------------------------------------------------

# Maps common HTML label text → extraction field name
_FIELD_MAP = {
    "collateral manager": "collateral_manager_legal_entity",
    "manager": "collateral_manager_legal_entity",
    "arranger": "arranger",
    "lead arranger": "arranger",
    "placement agent": "arranger",
    "deal type": "deal_type",
    "asset type": "deal_type",
    "target par": "target_par_mm",
    "target par amount": "target_par_mm",
    "asset par": "target_par_mm",
    "reinvestment period": "reinvestment_period",
    "reinvestment": "reinvestment_period",
    "non-call period": "non_call_period",
    "non-call": "non_call_period",
    "non call period": "non_call_period",
    "stated maturity": "stated_maturity",
    "maturity": "stated_maturity",
    "warehouse provider": "warehouse_provider",
    "warehouse": "warehouse_provider",
    "trustee": "trustee",
    "pricing date": "priced_date",
    "expected pricing date": "priced_date",
    "target pricing date": "priced_date",
    "closing date": "closing_date",
    "transaction type": "transaction_type",
}



def parse_deal_fields(email_html: str) -> dict:
    """
    Parse deal fields from HTML table label/value rows.

    Looks for two-column table rows where the first cell is a label
    (e.g. "Collateral Manager") and the second cell is the value.

    NOTE: Free-text extraction (deal name, manager, arranger, term,
    transaction type, status, collateral type) is handled by my_rules.py
    which runs as Step 4 in RuleBasedExtractor and overrides these results.
    This function focuses on structured HTML table data that my_rules.py
    doesn't parse.

    Returns a dict with only the fields it found (non-empty).
    """
    result = {}

    # Parse two-column table rows: <td>Label</td><td>Value</td>
    for row_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', email_html, re.DOTALL | re.IGNORECASE):
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row_match.group(1), re.DOTALL | re.IGNORECASE)
        if len(cells) != 2:
            continue

        label = re.sub(r'<[^>]+>', '', cells[0]).strip().lower()
        value = re.sub(r'<[^>]+>', '', cells[1]).strip()

        if not label or not value:
            continue

        # Match label to field name
        field_name = None
        for pattern, fname in _FIELD_MAP.items():
            if pattern in label:
                field_name = fname
                break

        if not field_name:
            continue

        # Special handling for target_par_mm: convert "$500,000,000" → 500.0
        if field_name == "target_par_mm":
            cleaned = re.sub(r'[^\d.]', '', value)
            if cleaned:
                val = float(cleaned)
                if val >= 1_000_000:
                    val = val / 1_000_000
                result[field_name] = val
            continue

        # Special handling for deal_type: normalize to standard values
        if field_name == "deal_type":
            dtype = value.upper()
            if "MIDDLE MARKET" in dtype or dtype.strip() == "MM":
                result[field_name] = "MM"
            elif "PRIVATE CREDIT" in dtype or dtype.strip() == "PC":
                result[field_name] = "PC"
            elif "INFRASTRUCTURE" in dtype or "INFRA" in dtype:
                result[field_name] = "Infra"
            elif "EMERGING" in dtype or dtype.strip() == "EM":
                result[field_name] = "EM"
            elif "BSL" in dtype or "BROADLY SYNDICATED" in dtype or "SENIOR SECURED" in dtype or "BANK LOAN" in dtype:
                result[field_name] = "BSL"
            else:
                result[field_name] = value
            continue

        result[field_name] = value

    # Extract collateral_manager_short from the legal entity
    legal = result.get("collateral_manager_legal_entity", "")
    if legal:
        short = re.split(r'[,(]', legal)[0].strip()
        short = re.sub(r'\s+(Inc\.?|LLC|Ltd\.?|L\.P\.?|LP|Management|Asset\s+Management)$',
                        '', short, flags=re.IGNORECASE).strip()
        if short:
            result["collateral_manager_short"] = short

    # Heuristic email type detection (fallback for when my_rules.py doesn't run)
    result.setdefault("email_type", detect_email_type(email_html))

    return result


# ---------------------------------------------------------------------------
# TRANCHE PRICING FORMATTER
# ---------------------------------------------------------------------------
# Parses tranche pricing tables from email HTML and formats them as:
#   A1 (Sr AAA) @ 130a dm
#   A2 (AA) @ 175a dm
#   B (A) @ 215a dm
#   C (BBB) @ 325a dm
#   D (BB) @ 575a dm
#   E (NR) @ 850a dm
#
# Rules:
#   - Multiple AAA tranches → Sr AAA / Jr AAA
#   - Non-AAA: one line per rating, blended spread if multiple tranches
#   - Fixed-rate tranches: convert using assumed 3mo SOFR
#   - Skip subordinated / residual / equity rows
#   - "Preplaced" / "Subject" annotations replace "@ XXX dm"
# ---------------------------------------------------------------------------

# Assumed 3mo SOFR rate for converting fixed-rate tranches to DM equivalent.
# TODO: pull current 3mo SOFR programmatically, e.g. from FRED API:
#   https://fred.stlouisfed.org/series/SOFR90DAYAVG
#   import fredapi; fred = fredapi.Fred(api_key='...'); sofr = fred.get_series('SOFR90DAYAVG').iloc[-1]
ASSUMED_SOFR_PCT = 3.6

_SP_SIMPLIFY = {
    "AAA": "AAA", "AA+": "AA", "AA": "AA", "AA-": "AA",
    "A+": "A", "A": "A", "A-": "A",
    "BBB+": "BBB", "BBB": "BBB", "BBB-": "BBB",
    "BB+": "BB", "BB": "BB", "BB-": "BB",
    "B+": "B", "B": "B", "B-": "B",
    "CCC+": "CCC", "CCC": "CCC", "CCC-": "CCC",
    "NR": "NR",
}

_MOODY_TO_SP = {
    "Aaa": "AAA", "Aa1": "AA", "Aa2": "AA", "Aa3": "AA",
    "A1": "A", "A2": "A", "A3": "A",
    "Baa1": "BBB", "Baa2": "BBB", "Baa3": "BBB",
    "Ba1": "BB", "Ba2": "BB", "Ba3": "BB",
    "B1": "B", "B2": "B", "B3": "B",
    "Caa1": "CCC", "Caa2": "CCC", "Caa3": "CCC",
    "NR": "NR",
}


def _simplify_sp_rating(rating_cell: str) -> str:
    """'Aaa/AAA' or 'Aa2/AA' or '[AAA](sf)/-' → simplified S&P equivalent e.g. 'AAA'."""
    # Strip HTML entities, brackets, (sf), sf suffix, and whitespace
    cleaned = rating_cell.strip()
    cleaned = re.sub(r'&nbsp;', ' ', cleaned)
    cleaned = re.sub(r'&\w+;', ' ', cleaned)
    cleaned = re.sub(r'[\[\]]', '', cleaned)
    cleaned = re.sub(r'\(sf\)', '', cleaned, flags=re.I)
    cleaned = re.sub(r'sf\b', '', cleaned, flags=re.I)
    cleaned = cleaned.strip()
    parts = [p.strip() for p in cleaned.split("/")]
    # Remove empty, dash-only, and NR parts for initial pass
    non_nr_parts = [p for p in parts if p and p != '-' and p.upper() != 'NR']
    # First try non-NR parts (prefer actual ratings over NR)
    for p in reversed(non_nr_parts):  # prefer S&P (usually second/third)
        if p in _SP_SIMPLIFY:
            return _SP_SIMPLIFY[p]
    for p in non_nr_parts:
        if p in _MOODY_TO_SP:
            return _MOODY_TO_SP[p]
    # If all parts are NR or empty, return NR
    all_parts = [p for p in parts if p and p != '-']
    return all_parts[-1] if all_parts else "NR"


def _short_tranche(name: str) -> str:
    """'Class A-1' → 'A1', 'Class B' → 'B'."""
    name = re.sub(r'(?i)\bclass\b\s*', '', name).strip()
    return name.replace('-', '').replace(' ', '')


def _format_spread(cell_text: str):
    """
    Parse a spread cell and return (formatted_text, skip).
    skip=True for residual/sub/equity rows.

    Handles formats from all banks:
    - BNP: "117-119", "150-155"
    - Citi: "124-125", "145a", "Call Desk (127)"
    - SMBC: "S + 145a", "SUBJECT (115)", "RETAINED"
    - GS: "SOFR + 123-124"
    - Barclays: "122a", "155-165", "NOT BEING REFINANCED"
    - BNP: "122 / Call Desk", "100-105 / New Tranche"
    """
    t = cell_text.strip()
    # Strip bracket notation: "[115]" → "115"
    t = re.sub(r'\[(\d+(?:\.\d+)?)\]', r'\1', t)
    low = t.lower()

    if any(kw in low for kw in ('residual', 'equity')):
        return None, True

    if 'retained' in low:
        return 'retained', False
    if 'preplaced' in low or 'pre-placed' in low:
        return 'preplaced', False
    if 'not being refinanced' in low:
        return None, True

    # "SUBJECT (115)" → "@ 115 dm" (subject with indicative level)
    m = re.search(r'subject\s*\((\d+(?:\.\d+)?)\)', low)
    if m:
        return f"@ {m.group(1)} dm", False
    if 'subject' in low:
        return 'subject', False

    # "Call Desk (127)" → "@ 127 dm"
    m = re.search(r'call\s*desk\s*\((\d+(?:\.\d+)?)\)', low)
    if m:
        return f"@ {m.group(1)} dm", False

    # Strip "/ Call Desk", "/ New Tranche" suffixes before parsing
    cleaned = re.sub(r'\s*/\s*(?:call\s*desk|new\s*tranche|roller)\s*$', '', t, flags=re.I).strip()
    cleaned_low = cleaned.lower()

    # Floating with prefix: "SOFR + 130 area", "S + 145a", "SOFR + 125-127", "L + 126"
    m = re.search(r'[+]\s*(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?\s*(area|a\b)?', cleaned_low)
    if m:
        s1 = m.group(1)
        s2 = m.group(2)
        area = 'a' if m.group(3) else ''
        if s2:
            return f"@ {s1}-{s2} dm", False
        return f"@ {s1}{area} dm", False

    # Plain numeric: "130 area", "120-130", "126", "145a"
    m = re.search(r'^(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?\s*(area|a\b)?$', cleaned_low.strip())
    if m:
        s1 = m.group(1)
        s2 = m.group(2)
        area = 'a' if m.group(3) else ''
        if s2:
            return f"@ {s1}-{s2} dm", False
        return f"@ {s1}{area} dm", False

    # Fixed rate: "5.50%"
    m = re.search(r'(\d+\.\d+)\s*%', t)
    if m:
        fixed = float(m.group(1))
        eq = round((fixed - ASSUMED_SOFR_PCT) * 100)
        return f"@ {eq} dm (fixed {m.group(1)}%)", False

    return f"@ {t}", False


def _parse_text_tranche_table(email_html: str) -> list:
    """
    Parse tranche tables from plain-text-formatted emails (Barclays, GS).

    These emails embed data as individual <td> cells in vertical layout (Barclays)
    or as space-aligned text lines (GS), not in standard HTML <table> rows.

    Returns list of (short_name, rating, formatted_spread, numeric_spread) tuples.
    """
    # Strip HTML to get clean text lines
    text = re.sub(r'<[^>]+>', '\n', email_html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    tranches = []

    # Pattern 1: GS space-aligned format
    # "A-1         $[288.00]    [35.6]%    [AAA](sf)/-    SOFR + 123-124    100.00    [4.5]"
    # "B-1 (Sr)     $[31.50]    [26.1]%    -/[AAsf]       SOFR + 150-155    100.00    [6.5]"
    for line in lines:
        m = re.match(
            r'^([A-Z][\w\-]*(?:\s*\([A-Z][a-z]\))?)\s+'     # tranche name
            r'\$?\[?[\d,.]+\]?\s+'                            # size
            r'\[?[\d.]+\]?%\s+'                               # C/E or sub
            r'([\[\]A-Za-z()\-/\s]+?)\s{2,}'                 # rating (2+ spaces end it)
            r'((?:SOFR|S|L)\s*\+\s*[\d\-.]+(?:\s*(?:area|a))?'  # SOFR + spread
            r'|[\d]+(?:\s*-\s*[\d]+)?(?:\s*a)?)',             # or plain numeric spread
            line, re.I
        )
        if m:
            name = _short_tranche(m.group(1))
            rating = _simplify_sp_rating(m.group(2))
            fmt, skip = _format_spread(m.group(3))
            if skip:
                continue
            numeric = None
            nm = re.search(r'@\s*(\d+(?:\.\d+)?)', fmt) if fmt else None
            if nm:
                numeric = float(nm.group(1))
            tranches.append((name, rating, fmt, numeric))

    if tranches:
        return tranches

    # Pattern 2: Barclays vertical format — columns repeat in sequence:
    # Class, Rating, Size, Sub%, MVOC, WAL, Type, Guidance (8 fields per tranche)
    # Detect by finding the header sequence
    header_idx = None
    num_cols = 0
    # Known header labels for Barclays-style vertical tables
    _header_labels = {'class', 's&p', 'moody', 'fitch', 'rating', 'class size', 'size',
                      'par sub', 'subordination', 'mvoc', 'wal', 'type', 'flt/fix',
                      'guidance', 'guidance/status', 'px talk', 'ipt', 'spread',
                      'notional', 'par', 'c/e'}
    for i, line in enumerate(lines):
        if line.lower().rstrip('^') in ('class',):
            # Count header lines: keep going while lines look like headers
            for j in range(i + 1, min(i + 12, len(lines))):
                if lines[j].startswith('---'):
                    header_idx = i
                    num_cols = j - i
                    break
                # If this line is NOT a known header label, data starts here
                if lines[j].lower().rstrip('^') not in _header_labels:
                    header_idx = i
                    num_cols = j - i
                    break
            break

    if header_idx is not None and num_cols >= 4:
        # Find which columns are rating and guidance
        header_lines = [lines[header_idx + k].lower() for k in range(num_cols)]
        rating_col = None
        guidance_col = None
        for k, h in enumerate(header_lines):
            if any(kw in h for kw in ('s&p', 'moody', 'fitch', 'rating')):
                rating_col = k
            if any(kw in h for kw in ('guidance', 'px talk', 'talk', 'ipt')):
                guidance_col = k

        if rating_col is not None and guidance_col is not None:
            # Data starts after the headers (and optional dashed line)
            data_start = header_idx + num_cols
            while data_start < len(lines) and lines[data_start].startswith('---'):
                data_start += 1

            # Read tranches: each tranche is num_cols consecutive lines
            while data_start + num_cols <= len(lines):
                chunk = lines[data_start:data_start + num_cols]
                # Stop at dashed separator, notes, or non-tranche data
                if chunk[0].startswith('---') or chunk[0].lower().startswith('note'):
                    break

                name = _short_tranche(chunk[0])
                rating = _simplify_sp_rating(chunk[rating_col]) if rating_col < len(chunk) else "NR"
                spread_text = chunk[guidance_col] if guidance_col < len(chunk) else ""
                fmt, skip = _format_spread(spread_text)
                if not skip:
                    numeric = None
                    nm = re.search(r'@\s*(\d+(?:\.\d+)?)', fmt) if fmt else None
                    if nm:
                        numeric = float(nm.group(1))
                    tranches.append((name, rating, fmt, numeric))

                data_start += num_cols

    return tranches


def format_tranche_pricing(email_html: str) -> dict:
    """
    Parse all tranche pricing tables from an email and return formatted strings.

    Returns {"ipt": "...", "updated_guidance": "...", "final_pricing": "..."}.
    Missing sections return "".
    """
    results = {"ipt": "", "updated_guidance": "", "final_pricing": ""}

    # Find all section headings and their positions (for context when column
    # headers are generic like "Spread / Coupon")
    heading_matches = list(re.finditer(
        r'<h[23][^>]*>(.*?)</h[23]>', email_html, re.DOTALL | re.IGNORECASE
    ))
    heading_pos = [
        (m.start(), re.sub(r'<[^>]+>', '', m.group(1)).strip().lower())
        for m in heading_matches
    ]

    # Parse each HTML table
    for tmatch in re.finditer(r'<table[^>]*>(.*?)</table>', email_html, re.DOTALL | re.IGNORECASE):
        thtml = tmatch.group(1)
        tstart = tmatch.start()

        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', thtml, re.DOTALL | re.IGNORECASE)
        if len(rows) < 2:
            continue

        # Parse header row — strip HTML tags and decode entities
        hcells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', rows[0], re.DOTALL | re.IGNORECASE)
        headers = [re.sub(r'<[^>]+>', '', c).strip() for c in hcells]
        # Strip footnote markers (^, ^^, ^^^) and decode &amp;
        headers = [re.sub(r'\^+$', '', h).strip() for h in headers]
        headers = [h.replace('&amp;', '&') for h in headers]
        hlower = [h.lower() for h in headers]

        # Identify tranche and rating columns
        tranche_idx = rating_idx = None
        for i, h in enumerate(hlower):
            if tranche_idx is None and ('tranche' in h or 'class' in h):
                tranche_idx = i
            # Rating: "S&P", "Moody", "Fitch", "Rating", "S&P / M / F", "Moody's/Fitch"
            if rating_idx is None and any(kw in h for kw in ('rating', 's&p', 'moody', 'fitch', 'mdys')):
                rating_idx = i
        if tranche_idx is None:
            continue

        # Columns to skip (not spread data)
        # Note: "status" alone is skipped, but "guidance/status" is kept (handled below)
        _skip_keywords = ('size', '$', 'amount', 'notional', 'par sub', 'subordination',
                          'wal', 'mvoc', 'flt', 'fix', 'type', 'price',
                          'c/e', 'credit enhancement')

        # Map spread columns → pricing type
        spread_map = {}
        for i, h in enumerate(hlower):
            if i == tranche_idx or i == rating_idx:
                continue
            if any(kw in h for kw in _skip_keywords):
                continue
            if 'final' in h:
                spread_map[i] = 'final_pricing'
            elif any(kw in h for kw in ('revised', 'updated')):
                spread_map[i] = 'updated_guidance'
            elif 'ipt' in h:
                spread_map[i] = 'ipt'
            # "Guidance", "Guidance/Status", "PX TALK"
            elif any(kw in h for kw in ('guidance', 'px talk', 'talk')):
                spread_map[i] = '_generic'
            elif 'spread' in h or 'coupon' in h:
                spread_map[i] = '_generic'
            # Skip standalone "Status" column (not "Guidance/Status")
            # — already handled above via 'guidance' match

        # Resolve generic columns using the section heading before this table
        if '_generic' in spread_map.values():
            preceding_heading = ""
            for hpos, htxt in heading_pos:
                if hpos < tstart:
                    preceding_heading = htxt
            generic_keys = [k for k, v in spread_map.items() if v == '_generic']
            for gk in generic_keys:
                if 'final' in preceding_heading or 'priced' in preceding_heading:
                    spread_map[gk] = 'final_pricing'
                elif 'guidance' in preceding_heading or 'revised' in preceding_heading or 'updated' in preceding_heading:
                    spread_map[gk] = 'updated_guidance'
                else:
                    spread_map[gk] = 'ipt'

        if not spread_map:
            continue

        # Process each spread column
        for col_idx, pkey in spread_map.items():
            raw_tranches = []

            for row_html in rows[1:]:
                cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row_html, re.DOTALL | re.IGNORECASE)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

                if len(cells) <= max(tranche_idx, col_idx):
                    continue

                tname = cells[tranche_idx]
                if not tname:
                    continue
                # Skip sub/subordinated/equity/residual rows
                if any(kw in tname.lower() for kw in ('sub', 'equity', 'residual', 'preferred')):
                    continue

                rating_str = cells[rating_idx] if rating_idx is not None and rating_idx < len(cells) else "NR"
                spread_text = cells[col_idx] if col_idx < len(cells) else ""

                short = _short_tranche(tname)
                rating = _simplify_sp_rating(rating_str)
                fmt, skip = _format_spread(spread_text)

                if skip:
                    continue

                # Extract numeric spread for blending
                numeric = None
                nm = re.search(r'@\s*(\d+(?:\.\d+)?)', fmt) if fmt else None
                if nm:
                    numeric = float(nm.group(1))

                raw_tranches.append((short, rating, fmt, numeric))

            if not raw_tranches:
                continue

            # Build output: Sr AAA / Jr AAA for multiple AAA tranches
            lines = []
            aaa = [(s, r, f, n) for s, r, f, n in raw_tranches if r == "AAA"]
            others = [(s, r, f, n) for s, r, f, n in raw_tranches if r != "AAA"]

            if len(aaa) > 1:
                for i, (short, _, fmt, _) in enumerate(aaa):
                    label = "Sr AAA" if i == 0 else "Jr AAA"
                    lines.append(f"{short} ({label}) {fmt}")
            elif aaa:
                short, _, fmt, _ = aaa[0]
                lines.append(f"{short} (AAA) {fmt}")

            # Non-AAA: one line per rating, blend if multiple tranches share a rating
            groups = {}
            for short, rating, fmt, numeric in others:
                groups.setdefault(rating, []).append((short, fmt, numeric))

            for rating, items in groups.items():
                if len(items) == 1:
                    short, fmt, _ = items[0]
                    lines.append(f"{short} ({rating}) {fmt}")
                else:
                    names = [s for s, _, _ in items]
                    numerics = [n for _, _, n in items if n is not None]
                    blended_name = "/".join(names)
                    if numerics:
                        avg = round(sum(numerics) / len(numerics))
                        has_area = any('a ' in (f or '') for _, f, _ in items)
                        suffix = "a" if has_area else ""
                        lines.append(f"{blended_name} ({rating}) @ {avg}{suffix} dm")
                    else:
                        _, fmt, _ = items[0]
                        lines.append(f"{blended_name} ({rating}) {fmt}")

            results[pkey] = "\n".join(lines)

    # Fallback: parse plain-text tables (Barclays, GS) if no HTML tables found
    if not any(results.values()):
        text_tranches = _parse_text_tranche_table(email_html)
        if text_tranches:
            # Build output using same Sr AAA / Jr AAA logic
            lines = []
            aaa = [(s, r, f, n) for s, r, f, n in text_tranches if r == "AAA"]
            others = [(s, r, f, n) for s, r, f, n in text_tranches if r != "AAA"]

            if len(aaa) > 1:
                for i, (short, _, fmt, _) in enumerate(aaa):
                    label = "Sr AAA" if i == 0 else "Jr AAA"
                    lines.append(f"{short} ({label}) {fmt}")
            elif aaa:
                short, _, fmt, _ = aaa[0]
                lines.append(f"{short} (AAA) {fmt}")

            groups = {}
            for short, rating, fmt, numeric in others:
                groups.setdefault(rating, []).append((short, fmt, numeric))

            for rating, items in groups.items():
                if len(items) == 1:
                    short, fmt, _ = items[0]
                    lines.append(f"{short} ({rating}) {fmt}")
                else:
                    names = [s for s, _, _ in items]
                    numerics = [n for _, _, n in items if n is not None]
                    blended_name = "/".join(names)
                    if numerics:
                        avg = round(sum(numerics) / len(numerics))
                        has_area = any('a ' in (f or '') for _, f, _ in items)
                        suffix = "a" if has_area else ""
                        lines.append(f"{blended_name} ({rating}) @ {avg}{suffix} dm")
                    else:
                        _, fmt, _ = items[0]
                        lines.append(f"{blended_name} ({rating}) {fmt}")

            if lines:
                results["ipt"] = "\n".join(lines)

    return results


# ---------------------------------------------------------------------------
# BASE CLASS — All extractors implement this interface.
# ---------------------------------------------------------------------------

class BaseExtractor(ABC):
    @abstractmethod
    def extract(self, email_text: str) -> ExtractionResult:
        """
        Extract structured fields from email text.

        Parameters:
            email_text — Raw email content (HTML or plain text).

        Returns:
            ExtractionResult with populated fields.
        """
        ...


# ---------------------------------------------------------------------------
# ANTHROPIC EXTRACTOR — Uses Claude API directly.
# ---------------------------------------------------------------------------
# Requires: ANTHROPIC_API_KEY environment variable
# Cost: ~$0.003-0.01 per email (depends on length)

class AnthropicExtractor(BaseExtractor):
    def __init__(self):
        import anthropic  # pip install anthropic
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        self.model = config.ANTHROPIC_MODEL

    def extract(self, email_text: str) -> ExtractionResult:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": email_text}],
        )
        raw = response.content[0].text
        data = parse_extraction(raw)
        return ExtractionResult(**{k: v for k, v in data.items() if k in ExtractionResult.model_fields})


# ---------------------------------------------------------------------------
# BEDROCK EXTRACTOR — Uses AWS Bedrock (Claude on AWS).
# ---------------------------------------------------------------------------
# Requires: AWS credentials configured (via ~/.aws/credentials, IAM role, etc.)
# Cost: Similar to Anthropic direct

class BedrockExtractor(BaseExtractor):
    def __init__(self):
        import boto3  # pip install boto3
        self.client = boto3.client("bedrock-runtime", region_name=config.BEDROCK_REGION)
        self.model_id = config.BEDROCK_MODEL_ID

    def extract(self, email_text: str) -> ExtractionResult:
        import json as _json
        body = _json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": email_text}],
        })
        resp = self.client.invoke_model(modelId=self.model_id, body=body)
        result = _json.loads(resp["body"].read())
        raw = result["content"][0]["text"]
        data = parse_extraction(raw)
        return ExtractionResult(**{k: v for k, v in data.items() if k in ExtractionResult.model_fields})


# ---------------------------------------------------------------------------
# NuExtract EXTRACTOR — Runs locally on your machine's GPU or CPU.
# ---------------------------------------------------------------------------
# Requires: transformers, torch (installed via requirements.txt)
# Cost: Free (runs locally)
# Speed: ~5-10 tok/s on Apple M1 Max MPS, ~30+ tok/s on NVIDIA GPU
#
# FIRST RUN: Downloads the model (~7.5GB) from HuggingFace. Cached at
#            ~/.cache/huggingface/hub/models--numind--NuExtract-1.5/
#
# LoRA ADAPTER: If models/nuextract-lora/adapter_config.json exists,
#               the fine-tuned adapter is automatically loaded and merged.

class NuExtractExtractor(BaseExtractor):
    def __init__(self):
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer
            import torch
        except ImportError:
            raise RuntimeError("transformers and torch required for NuExtract")

        # --- Determine the best available device ---
        # Auto-fallback chain: requested device → mps → cpu
        device = config.NUEXTRACT_DEVICE
        if device == "cuda":
            if not torch.cuda.is_available():
                logger.warning("CUDA not available, falling back to MPS/CPU for NuExtract")
                device = "mps" if torch.backends.mps.is_available() else "cpu"
        elif device == "mps":
            if not torch.backends.mps.is_available():
                logger.warning("MPS not available, falling back to CPU for NuExtract")
                device = "cpu"

        self.device = device

        # --- Load tokenizer (converts text ↔ token IDs) ---
        print(f"[NuExtract] Loading tokenizer for {config.NUEXTRACT_MODEL}...")
        self.tokenizer = AutoTokenizer.from_pretrained(config.NUEXTRACT_MODEL)

        # --- Load model weights ---
        # torch_dtype="auto" lets the model use its native precision (bfloat16).
        # On CPU, we force float32 since CPU doesn't benefit from reduced precision.
        # device_map="cuda" enables automatic GPU placement on NVIDIA; for MPS
        # and CPU we load to CPU first then manually move with .to(device).
        print(f"[NuExtract] Loading model weights on {device} (this may take a minute)...")
        self.model = AutoModelForCausalLM.from_pretrained(
            config.NUEXTRACT_MODEL,
            torch_dtype=torch.float32 if device == "cpu" else "auto",
            device_map=device if device == "cuda" else None,
        )
        if device in ("cpu", "mps"):
            print(f"[NuExtract] Moving model to {device}...")
            self.model = self.model.to(device)

        # --- Load LoRA adapter if one exists from previous fine-tuning ---
        # The adapter is a small set of extra weights (~50-100MB) that adjust
        # the model's behavior based on your corrected extractions.
        # merge_and_unload() folds the adapter into the base weights so there's
        # no speed penalty during inference.
        import os
        if os.path.isdir(config.LORA_ADAPTER_DIR) and os.path.exists(
            os.path.join(config.LORA_ADAPTER_DIR, "adapter_config.json")
        ):
            try:
                from peft import PeftModel  # pip install peft
                print(f"[NuExtract] Loading fine-tuned LoRA adapter from {config.LORA_ADAPTER_DIR}...")
                self.model = PeftModel.from_pretrained(self.model, config.LORA_ADAPTER_DIR)
                self.model = self.model.merge_and_unload()
                print("[NuExtract] LoRA adapter merged. Using fine-tuned model.")
            except Exception as e:
                print(f"[NuExtract] Warning: could not load LoRA adapter: {e}")
        else:
            print("[NuExtract] No fine-tuned adapter found, using base model.")

        print("[NuExtract] Model ready.")

    def extract(self, email_text: str) -> ExtractionResult:
        """
        Run extraction using NuExtract's prompt format:
            <|input|>   — the raw email
            <|template|> — JSON with empty values (fields to extract)
            <|output|>   — model generates filled JSON here
        """
        import time

        # Build the prompt using NuExtract's special token format
        template = json.dumps(NUEXTRACT_TEMPLATE, indent=2)
        prompt = f"<|input|>\n{email_text}\n<|template|>\n{template}\n<|output|>\n"

        # Tokenize: convert text to numerical token IDs the model understands
        print(f"[NuExtract] Tokenizing input ({len(email_text)} chars)...")
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        n_tokens = inputs["input_ids"].shape[1]

        # Generate: the model predicts tokens one-by-one (autoregressive)
        # max_new_tokens=2048 prevents infinite generation
        # do_sample=False means greedy decoding (always pick most likely token)
        print(f"[NuExtract] Input: {n_tokens} tokens. Generating (max 2048 new tokens)...")
        t0 = time.time()
        with __import__("torch").no_grad():  # Disable gradient computation (faster, less memory)
            outputs = self.model.generate(
                **inputs, max_new_tokens=2048, do_sample=False
            )
        elapsed = time.time() - t0
        n_out = outputs[0].shape[0] - n_tokens
        print(f"[NuExtract] Generated {n_out} tokens in {elapsed:.1f}s ({n_out/elapsed:.1f} tok/s)")

        # Decode: convert token IDs back to text
        raw = self.tokenizer.decode(outputs[0][n_tokens:], skip_special_tokens=True)

        # Parse the JSON from the model's output
        print(f"[NuExtract] Parsing JSON from output...")
        data = parse_extraction(raw)

        # --- Clean up: the model sometimes returns "$500,000,000" instead of 500.0 ---
        # We strip non-numeric chars and convert to millions if needed.
        if "target_par_mm" in data and isinstance(data["target_par_mm"], str):
            cleaned = re.sub(r"[^\d.]", "", data["target_par_mm"])
            if cleaned:
                val = float(cleaned)
                # If value looks like full dollars (>= 1,000,000), convert to $MM
                if val >= 1_000_000:
                    val = val / 1_000_000
                data["target_par_mm"] = val
            else:
                data["target_par_mm"] = None

        return ExtractionResult(**{k: v for k, v in data.items() if k in ExtractionResult.model_fields})


# ---------------------------------------------------------------------------
# RULE-BASED EXTRACTOR — Full extraction without any LLM.
# ---------------------------------------------------------------------------
# Uses the HTML table parser (parse_deal_fields + format_tranche_pricing)
# as the default engine, and optionally loads a custom rules module for
# additional / overriding extraction logic.
#
# WHY USE THIS:
#   - Work machine where HuggingFace / torch can't be installed
#   - No GPU available and CPU inference is too slow
#   - Want deterministic, auditable extraction (no model randomness)
#   - Building training data for later AI fine-tuning
#
# CUSTOM RULES MODULE:
#   Set CLO_RULES_MODULE env var to a Python module path. The module must
#   define: extract_from_email(email_text: str, rules: yaml) -> dict
#
#   The dict should use ExtractionResult field names as keys. Values from
#   your custom module OVERRIDE the built-in HTML parser results, so you
#   only need to return the fields your rules handle.
#
#   Example custom module (my_rules.py):
#
#       import re
#       def extract_from_email(email_text: str) -> dict:
#           result = {}
#           # Your custom logic here — regex, keyword matching, etc.
#           m = re.search(r'Manager:\s*(.+)', email_text)
#           if m:
#               result["collateral_manager_legal_entity"] = m.group(1).strip()
#           return result

class RuleBasedExtractor(BaseExtractor):
    """
    Rule-based extractor that uses HTML parsing + optional custom rules module.

    Extraction pipeline:
        1. Built-in HTML parser (parse_deal_fields) → base fields
        2. Built-in tranche pricing parser (format_tranche_pricing) → ipt/guidance/pricing
        3. Heuristic email type detection → email_type fallback
        4. Custom rules module (if configured) → overrides any of the above
    """

    def __init__(self):
        self._custom_extract = None

        rules_module = config.RULES_MODULE
        if rules_module:
            self._custom_extract = _load_rules_module(rules_module)
            if self._custom_extract:
                print(f"[RuleBased] Custom rules module loaded: {rules_module}")
            else:
                print(f"[RuleBased] Warning: could not load custom rules from '{rules_module}', using built-in only")
        else:
            print("[RuleBased] No custom rules module configured, using built-in HTML parser")

        print("[RuleBased] Extractor ready (no LLM required)")

    def extract(self, email_text: str) -> ExtractionResult:
        # Step 1: Built-in HTML table parser for structured fields
        fields = parse_deal_fields(email_text)

        # Step 2: Built-in tranche pricing parser
        pricing = format_tranche_pricing(email_text)
        for key in ("ipt", "updated_guidance", "final_pricing"):
            if pricing.get(key):
                fields.setdefault(key, pricing[key])

        # Step 3: Email type fallback (if not detected from HTML)
        fields.setdefault("email_type", detect_email_type(email_text))

        # Step 4: Custom rules module overrides
        if self._custom_extract:
            try:
                custom_fields = self._custom_extract(email_text)
                if isinstance(custom_fields, dict):
                    # Custom rules override built-in results
                    for k, v in custom_fields.items():
                        if v is not None and v != "":
                            fields[k] = v
            except Exception as e:
                logger.warning(f"Custom rules module error: {e}")

        # Map extraction fields to GUI-compatible namespaced keys
        # (e.g. "deal_name" → "clo-deals__Title")
        fields = map_extraction_to_gui(fields)

        # Build ExtractionResult — include both model fields and extra GUI keys
        # ExtractionResult has extra="allow" so namespaced keys pass through
        return ExtractionResult(**fields)


def _load_rules_module(module_path: str):
    """
    Load a custom rules module and return its extract_from_email function.

    Supports two formats:
        - Python dotted module path: "my_rules.clo_extractor"
        - File system path: "/path/to/my_rules.py"

    Returns the extract_from_email callable, or None if loading fails.
    """
    import importlib
    import importlib.util
    import os

    try:
        # Try as a file path first
        if os.path.isfile(module_path):
            spec = importlib.util.spec_from_file_location("custom_rules", module_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:
            # Try as a dotted module path
            mod = importlib.import_module(module_path)

        fn = getattr(mod, "extract_from_email", None)
        if fn is None:
            logger.error(f"Rules module '{module_path}' has no extract_from_email() function")
            return None
        if not callable(fn):
            logger.error(f"extract_from_email in '{module_path}' is not callable")
            return None
        return fn
    except Exception as e:
        logger.error(f"Failed to load rules module '{module_path}': {e}")
        return None


# ---------------------------------------------------------------------------
# FALLBACK EXTRACTOR — No LLM, just heuristic email type detection.
# ---------------------------------------------------------------------------
# Used automatically when no LLM backend is available (missing API key,
# model failed to load, etc.). Only detects email_type; all other fields
# will be None and must be filled in manually.

class FallbackExtractor(BaseExtractor):
    """Heuristic-only extractor when no LLM is available."""

    def extract(self, email_text: str) -> ExtractionResult:
        return ExtractionResult(email_type=detect_email_type(email_text))


# ---------------------------------------------------------------------------
# FACTORY FUNCTION — Creates the right extractor based on config.
# ---------------------------------------------------------------------------

class _MappingAwareExtractor(BaseExtractor):
    """Wrapper that clears skipped fields from extraction results based on field_mapping.json."""

    def __init__(self, inner: BaseExtractor):
        self._inner = inner
        self._skipped = get_skipped_fields()
        if self._skipped:
            print(f"[FieldMapping] Skipping {len(self._skipped)} fields: {', '.join(self._skipped)}")

    def extract(self, email_text: str) -> ExtractionResult:
        result = self._inner.extract(email_text)
        if self._skipped:
            data = result.model_dump()
            for field in self._skipped:
                if field in data:
                    data[field] = None
            return ExtractionResult(**data)
        return result


def get_extractor() -> BaseExtractor:
    """
    Create and return the configured extractor.

    Reads CLO_EXTRACTOR from config (set via env var or .env file):
        "anthropic"  → AnthropicExtractor
        "bedrock"    → BedrockExtractor
        "nuextract"  → NuExtractExtractor
        "rules"      → RuleBasedExtractor (no LLM, uses HTML parsing + custom rules)

    If the chosen backend fails to initialize (missing API key, GPU error,
    download failure, etc.), falls back to FallbackExtractor gracefully.

    If a field_mapping.json exists with skipped fields, the extractor is
    wrapped to automatically clear those fields from results.
    """
    ext_type = config.EXTRACTOR.lower()
    inner = None
    try:
        if ext_type == "anthropic":
            inner = AnthropicExtractor()
        elif ext_type == "bedrock":
            inner = BedrockExtractor()
        elif ext_type == "nuextract":
            inner = NuExtractExtractor()
        elif ext_type == "rules":
            inner = RuleBasedExtractor()
    except Exception as e:
        logger.warning(f"Failed to load {ext_type} extractor: {e}. Using fallback.")

    if inner is None:
        inner = FallbackExtractor()

    # Wrap with field mapping awareness (clears skipped fields)
    try:
        skipped = get_skipped_fields()
        if skipped:
            return _MappingAwareExtractor(inner)
    except Exception as e:
        logger.warning(f"Could not load field mapping: {e}")

    return inner
