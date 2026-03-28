"""
Field mapping configuration for CLO Email Extraction.
======================================================

WHAT THIS FILE DOES:
    Manages the mapping between extraction fields (what the LLM/rules extract)
    and data stores (where values get saved: deals.json, transactions.json, etc.).

    This allows you to:
        - Remap fields to different stores than the defaults
        - Mark fields as "skip" (never extracted, always blank)
        - Add new extraction fields that map to your actual JSON structure
        - Keep the same GUI and training pipeline regardless of mapping

WHY THIS EXISTS:
    The default field mapping assumes a specific JSON structure. If your work
    computer has different JSON schemas, you can reconfigure the mapping
    without changing any code. Run configure_fields.ipynb to set it up.

CONFIG FILE:
    data/field_mapping.json — created by configure_fields.ipynb
    If the file doesn't exist, the DEFAULT_MAPPING below is used.
"""

import json
import os
import logging

from backend import config

logger = logging.getLogger(__name__)

# Path to the field mapping config file
MAPPING_FILE = os.path.join(config.DATA_DIR, "field_mapping.json")

# ---------------------------------------------------------------------------
# STORE NAMES — Must match the "store" values in data/field_mapping.json
# AND the store.py file constants (clo-deals.json, clo-managers.json, etc.)
# ---------------------------------------------------------------------------
STORE_DEALS = "clo-deals"
STORE_TRANSACTIONS = "clo-transactions"
STORE_MANAGERS = "clo-managers"
STORE_DEAL_ORDERS = "deal_orders"
STORE_NONE = None  # Field is used for routing/display only (e.g. email_type)

ALL_STORES = [STORE_DEALS, STORE_TRANSACTIONS, STORE_MANAGERS, STORE_DEAL_ORDERS]

# ---------------------------------------------------------------------------
# DEFAULT FIELD MAPPING — Matches the original hardcoded behavior
# ---------------------------------------------------------------------------
# Each field entry:
#   label       — Human-readable name shown in the GUI form
#   store       — Which JSON store this field saves to (or null for routing-only)
#   store_field — Field name in the target store (may differ from extraction name)
#   extract     — Whether to extract this field from emails (False = always blank)
#   form_type   — Widget type: "text", "textarea", "float", "dropdown"
#   notes       — Optional description for the config GUI

DEFAULT_MAPPING = {
    "version": 1,
    "fields": {
        "email_type": {
            "label": "Email Type",
            "store": None,
            "store_field": None,
            "extract": True,
            "form_type": "dropdown",
            "notes": "Controls processing flow — not saved to a store directly",
        },
        "deal_name": {
            "label": "Deal Name",
            "store": "deals",
            "store_field": "deal_name",
            "extract": True,
            "form_type": "text",
            "notes": "Also used as the link key for transactions and deal orders",
        },
        "collateral_manager_legal_entity": {
            "label": "Manager (Legal Entity)",
            "store": "deals",
            "store_field": "legal_entity",
            "extract": True,
            "form_type": "text",
            "notes": "Full legal name; also used to create manager records",
        },
        "collateral_manager_short": {
            "label": "Manager (Short)",
            "store": "deals",
            "store_field": "collateral_manager_short",
            "extract": True,
            "form_type": "text",
            "notes": "e.g. PGIM, Ares, Carlyle",
        },
        "arranger": {
            "label": "Arranger",
            "store": "deals",
            "store_field": "arranger",
            "extract": True,
            "form_type": "text",
            "notes": "Investment bank arranging the deal",
        },
        "deal_type": {
            "label": "Deal Type",
            "store": "deals",
            "store_field": "deal_type",
            "extract": True,
            "form_type": "text",
            "notes": "BSL, MM, Euro BSL, Euro MM, Other",
        },
        "target_par_mm": {
            "label": "Target Par ($MM)",
            "store": "deals",
            "store_field": "target_par",
            "extract": True,
            "form_type": "float",
            "notes": "Target par amount in millions",
        },
        "transaction_type": {
            "label": "Transaction Type",
            "store": "transactions",
            "store_field": "transaction_type",
            "extract": True,
            "form_type": "text",
            "notes": "New Issue, Reset, Refi",
        },
        "reinvestment_period": {
            "label": "Reinvestment Period",
            "store": "deals",
            "store_field": "reinvestment_period",
            "extract": True,
            "form_type": "text",
            "notes": "e.g. 5 years",
        },
        "non_call_period": {
            "label": "Non-Call Period",
            "store": "deals",
            "store_field": "non_call_period",
            "extract": True,
            "form_type": "text",
            "notes": "e.g. 2 years",
        },
        "stated_maturity": {
            "label": "Stated Maturity",
            "store": "deals",
            "store_field": "stated_maturity",
            "extract": True,
            "form_type": "text",
            "notes": "e.g. April 2037",
        },
        "warehouse_provider": {
            "label": "Warehouse Provider",
            "store": "deals",
            "store_field": "warehouse_provider",
            "extract": True,
            "form_type": "text",
            "notes": "",
        },
        "trustee": {
            "label": "Trustee",
            "store": "deals",
            "store_field": "trustee",
            "extract": True,
            "form_type": "text",
            "notes": "e.g. U.S. Bank",
        },
        "announced_date": {
            "label": "Announced Date",
            "store": "transactions",
            "store_field": "announced_date",
            "extract": True,
            "form_type": "text",
            "notes": "YYYY-MM-DD",
        },
        "priced_date": {
            "label": "Priced Date",
            "store": "transactions",
            "store_field": "priced_date",
            "extract": True,
            "form_type": "text",
            "notes": "YYYY-MM-DD",
        },
        "ipt": {
            "label": "IPT (Initial Pricing)",
            "store": "transactions",
            "store_field": "ipt",
            "extract": True,
            "form_type": "textarea",
            "notes": "Tranche-level initial price talk",
        },
        "updated_guidance": {
            "label": "Updated Guidance",
            "store": "transactions",
            "store_field": "updated_guidance",
            "extract": True,
            "form_type": "textarea",
            "notes": "Revised spread guidance",
        },
        "final_pricing": {
            "label": "Final Pricing",
            "store": "transactions",
            "store_field": "final_pricing",
            "extract": True,
            "form_type": "textarea",
            "notes": "Final tranche-level pricing",
        },
    },
}


# ---------------------------------------------------------------------------
# KNOWN EXTRACTABLE FIELDS — Fields we know how to pull from CLO emails.
# Maps a JSON field name (as found in stores) to extraction metadata.
# Used by scan_stores() to auto-match JSON fields to extraction capabilities.
# ---------------------------------------------------------------------------
_KNOWN_EXTRACTABLE = {
    # field_name_in_json: (extraction_name, label, form_type, notes)
    "deal_name":                    ("deal_name",                       "Deal Name",               "text",     "Full legal name of the CLO deal"),
    "legal_entity":                 ("collateral_manager_legal_entity", "Manager (Legal Entity)",  "text",     "Full legal name of the manager"),
    "collateral_manager_short":     ("collateral_manager_short",        "Manager (Short)",         "text",     "e.g. PGIM, Ares, Carlyle"),
    "short_name":                   ("collateral_manager_short",        "Manager (Short)",         "text",     "e.g. PGIM, Ares, Carlyle"),
    "collateral_manager":           ("collateral_manager_short",        "Manager (Short)",         "text",     "e.g. PGIM, Ares, Carlyle"),
    "arranger":                     ("arranger",                        "Arranger",                "text",     "Investment bank arranging the deal"),
    "deal_type":                    ("deal_type",                       "Deal Type",               "text",     "BSL, MM, Euro BSL, Euro MM, Other"),
    "target_par":                   ("target_par_mm",                   "Target Par ($MM)",        "float",    "Target par amount in millions"),
    "target_par_mm":                ("target_par_mm",                   "Target Par ($MM)",        "float",    "Target par amount in millions"),
    "transaction_type":             ("transaction_type",                "Transaction Type",        "text",     "New Issue, Reset, Refi"),
    "reinvestment_period":          ("reinvestment_period",             "Reinvestment Period",     "text",     "e.g. 5 years"),
    "non_call_period":              ("non_call_period",                 "Non-Call Period",         "text",     "e.g. 2 years"),
    "stated_maturity":              ("stated_maturity",                 "Stated Maturity",         "text",     "e.g. April 2037"),
    "warehouse_provider":           ("warehouse_provider",              "Warehouse Provider",      "text",     ""),
    "trustee":                      ("trustee",                         "Trustee",                 "text",     "e.g. U.S. Bank"),
    "announced_date":               ("announced_date",                  "Announced Date",          "text",     "YYYY-MM-DD"),
    "priced_date":                  ("priced_date",                     "Priced Date",             "text",     "YYYY-MM-DD"),
    "pricing_date":                 ("priced_date",                     "Priced Date",             "text",     "YYYY-MM-DD"),
    "ipt":                          ("ipt",                             "IPT (Initial Pricing)",   "textarea", "Tranche-level initial price talk"),
    "initial_price_talk":           ("ipt",                             "IPT (Initial Pricing)",   "textarea", "Tranche-level initial price talk"),
    "updated_guidance":             ("updated_guidance",                "Updated Guidance",        "textarea", "Revised spread guidance"),
    "revised_guidance":             ("updated_guidance",                "Updated Guidance",        "textarea", "Revised spread guidance"),
    "final_pricing":                ("final_pricing",                   "Final Pricing",           "textarea", "Final tranche-level pricing"),
    "spread":                       ("spread",                          "Spread (bps)",            "float",    "Spread in basis points"),
    "tranche":                      ("tranche",                         "Tranche",                 "text",     "e.g. Class A-1, Class B"),
    "allocation":                   ("allocation",                      "Allocation ($MM)",        "float",    "Allocation amount in millions"),
    "aum_bn":                       ("aum_bn",                          "AUM ($B)",                "float",    "Assets under management in billions"),
    "hq":                           ("hq",                              "Headquarters",            "text",     ""),
    "rating":                       ("rating",                          "Rating",                  "text",     "Credit rating"),
    "coupon":                       ("coupon",                          "Coupon",                  "text",     "Coupon rate"),
    "collateral_type":              ("collateral_type",                 "Collateral Type",         "text",     ""),
    "weighted_avg_spread":          ("weighted_avg_spread",             "WAS",                     "text",     "Weighted average spread"),
    "was":                          ("weighted_avg_spread",             "WAS",                     "text",     "Weighted average spread"),
    "diversity_score":              ("diversity_score",                 "Diversity Score",         "float",    ""),
    "overcollateralization":        ("overcollateralization",           "OC Test",                 "text",     ""),
    "warf":                         ("warf",                            "WARF",                    "float",    "Weighted avg rating factor"),
    "recovery_rate":                ("recovery_rate",                   "Recovery Rate",           "text",     ""),
    # Sharepoint-specific field names
    "bloomberg_deal_name":          ("bloomberg_deal_name",             "Bloomberg Deal Name",     "text",     ""),
    "intex_deal":                   ("intex_deal",                      "Intex Deal",              "text",     ""),
    "intex_preprice":               ("intex_preprice",                  "Intex Preprice",          "text",     ""),
    "ultimate_parent":              ("ultimate_parent",                 "Ultimate Parent",         "text",     ""),
    "sec_number":                   ("sec_number",                      "SEC Number",              "text",     ""),
    "crd_number":                   ("crd_number",                      "CRD Number",              "text",     ""),
    "term":                         ("term",                            "Term",                    "text",     ""),
    "manager_name":                 ("collateral_manager_legal_entity", "Manager (Legal Entity)",  "text",     "Full legal name of the manager"),
    "placement_agent":              ("arranger",                        "Arranger",                "text",     "Placement agent / arranger"),
}

# Fields that are system/metadata — never extractable from emails
_SYSTEM_FIELDS = {
    "id", "created_at", "updated_at", "status", "engaged", "executed",
    "executed_date", "engaged_date", "notes",
}


def scan_stores() -> dict:
    """
    Scan all JSON stores in data/, discover their schemas, and auto-generate
    a field mapping by matching JSON fields to known extractable fields.

    Returns a mapping dict with:
      - Matched fields: JSON field → extraction field with correct store routing
      - Unmatched fields: JSON fields we don't know how to extract, marked extract=False
        so you can see them and decide what to do
      - email_type: always included as the routing field

    The idea: drop your real JSONs into data/, run this, and get a mapping
    that matches your actual structure. Then review and tweak.
    """
    from backend import store as store_module

    # Map store filename → short name used in field_mapping
    store_name_map = {
        store_module.DEALS: STORE_DEALS,
        store_module.MANAGERS: STORE_MANAGERS,
        store_module.TRANSACTIONS: STORE_TRANSACTIONS,
        store_module.DEAL_ORDERS: STORE_DEAL_ORDERS,
    }

    # Discover all unique field names per store
    discovered = {}  # {store_short_name: {field_name: sample_value}}
    for store_file, store_name in store_name_map.items():
        try:
            records = store_module.read_store(store_file)
        except Exception:
            continue
        if not records:
            continue
        fields = {}
        for record in records:
            for key, val in record.items():
                if key not in fields and val is not None and val != "":
                    fields[key] = val
        discovered[store_name] = fields

    # Also scan for any other .json files in data/ that aren't the standard stores
    for fname in os.listdir(config.DATA_DIR):
        if not fname.endswith(".json"):
            continue
        if fname in store_name_map:
            continue
        if fname == "field_mapping.json":
            continue
        store_name = fname.replace(".json", "")
        fpath = os.path.join(config.DATA_DIR, fname)
        try:
            with open(fpath, "r") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                fields = {}
                for record in data:
                    if isinstance(record, dict):
                        for key, val in record.items():
                            if key not in fields and val is not None and val != "":
                                fields[key] = val
                if fields:
                    discovered[store_name] = fields
                    if store_name not in ALL_STORES:
                        ALL_STORES.append(store_name)
        except Exception:
            continue

    # Build the mapping
    mapping = {"version": 1, "fields": {}}

    # Always include email_type as routing field
    mapping["fields"]["email_type"] = {
        "label": "Email Type",
        "store": None,
        "store_field": None,
        "extract": True,
        "form_type": "dropdown",
        "notes": "Controls processing flow (announced/updated/priced)",
    }

    # Track which extraction names we've already added (avoid duplicates
    # when multiple stores have the same logical field like deal_name)
    seen_extraction_names = {"email_type"}

    for store_name, fields in discovered.items():
        for field_name, sample_value in fields.items():
            if field_name in _SYSTEM_FIELDS:
                continue

            if field_name in _KNOWN_EXTRACTABLE:
                ext_name, label, form_type, notes = _KNOWN_EXTRACTABLE[field_name]

                # Skip if we already mapped this extraction name
                if ext_name in seen_extraction_names:
                    continue
                seen_extraction_names.add(ext_name)

                mapping["fields"][ext_name] = {
                    "label": label,
                    "store": store_name,
                    "store_field": field_name,
                    "extract": True,
                    "form_type": form_type,
                    "notes": notes,
                }
            else:
                # Unknown field — include it but mark as not-yet-extractable
                # User can enable extraction and write custom rules for it
                ext_name = f"{store_name}__{field_name}"
                if ext_name in seen_extraction_names:
                    continue
                seen_extraction_names.add(ext_name)

                # Guess form_type from sample value
                form_type = "text"
                if isinstance(sample_value, (int, float)):
                    form_type = "float"
                elif isinstance(sample_value, str) and len(sample_value) > 100:
                    form_type = "textarea"

                mapping["fields"][ext_name] = {
                    "label": _prettify(field_name),
                    "store": store_name,
                    "store_field": field_name,
                    "extract": False,
                    "form_type": form_type,
                    "notes": f"Found in {store_name}.json — enable + add extraction rule if needed",
                }

    return mapping


def _prettify(field_name: str) -> str:
    """Convert 'some_field_name' → 'Some Field Name' for GUI labels."""
    return field_name.replace("_", " ").replace("-", " ").title()


def load_mapping() -> dict:
    """
    Load the field mapping config. Falls back to DEFAULT_MAPPING if no config file exists.
    """
    if os.path.isfile(MAPPING_FILE):
        try:
            with open(MAPPING_FILE, "r") as f:
                mapping = json.load(f)
            logger.info(f"Loaded field mapping from {MAPPING_FILE}")
            return mapping
        except Exception as e:
            logger.warning(f"Failed to load {MAPPING_FILE}: {e}. Using defaults.")
    return json.loads(json.dumps(DEFAULT_MAPPING))  # deep copy


def save_mapping(mapping: dict) -> None:
    """Save the field mapping config to disk."""
    os.makedirs(os.path.dirname(MAPPING_FILE), exist_ok=True)
    with open(MAPPING_FILE, "w") as f:
        json.dump(mapping, f, indent=2)
    logger.info(f"Saved field mapping to {MAPPING_FILE}")


def get_active_fields(mapping: dict = None) -> list[dict]:
    """
    Return list of active (extract=True) field configs.

    Each entry: {"name": str, "label": str, "store": str|None,
                 "store_field": str|None, "form_type": str, "notes": str}
    """
    if mapping is None:
        mapping = load_mapping()
    result = []
    for name, cfg in mapping["fields"].items():
        if cfg.get("extract", True):
            result.append({
                "name": name,
                "label": cfg.get("label", name),
                "store": cfg.get("store"),
                "store_field": cfg.get("store_field", name),
                "form_type": cfg.get("form_type", "text"),
                "notes": cfg.get("notes", ""),
            })
    return result


def get_skipped_fields(mapping: dict = None) -> list[str]:
    """Return list of field names that are marked as skip (extract=False)."""
    if mapping is None:
        mapping = load_mapping()
    return [name for name, cfg in mapping["fields"].items() if not cfg.get("extract", True)]


def get_store_fields(mapping: dict = None, store_name: str = None) -> list[dict]:
    """
    Return fields that map to a specific store (or all stores if store_name is None).

    Each entry: {"name": str, "store_field": str, "extract": bool}
    """
    if mapping is None:
        mapping = load_mapping()
    result = []
    for name, cfg in mapping["fields"].items():
        s = cfg.get("store")
        if store_name is None or s == store_name:
            result.append({
                "name": name,
                "store": s,
                "store_field": cfg.get("store_field", name),
                "extract": cfg.get("extract", True),
            })
    return result


def build_store_record(extraction: dict, store_name: str, mapping: dict = None) -> dict:
    """
    Build a store record dict from extraction values using the field mapping.

    Takes the flat extraction dict (field_name → value) and maps each field
    to its configured store_field name in the target store.

    Parameters:
        extraction  — flat dict of extraction field names → values
        store_name  — which store to build for ("deals", "transactions", etc.)
        mapping     — field mapping config (loads default if None)

    Returns:
        dict ready to save to the target store
    """
    if mapping is None:
        mapping = load_mapping()

    record = {}
    for name, cfg in mapping["fields"].items():
        if cfg.get("store") != store_name:
            continue
        if not cfg.get("extract", True):
            continue

        value = extraction.get(name)
        if value is None or value == "":
            continue

        store_field = cfg.get("store_field", name)
        record[store_field] = value

    return record


def get_form_fields(mapping: dict = None) -> list[tuple]:
    """
    Return FORM_FIELDS list compatible with the notebook GUI.

    Returns: [(field_name, label, form_type), ...]
    Only includes fields where extract=True.
    """
    if mapping is None:
        mapping = load_mapping()
    result = []
    for name, cfg in mapping["fields"].items():
        if cfg.get("extract", True):
            result.append((name, cfg.get("label", name), cfg.get("form_type", "text")))
    return result


# ---------------------------------------------------------------------------
# EXTRACTION → GUI FIELD MAPPING
# ---------------------------------------------------------------------------
# The extraction produces template keys like "deal_name", "arranger", "Title",
# "Placement Agent" etc. The GUI form uses namespaced keys from field_mapping.json
# like "clo-deals__Title", "clo-transactions__Placement Agent".
#
# This function bridges the two: given an extraction dict, it produces a new
# dict with the GUI-compatible namespaced keys populated.
# ---------------------------------------------------------------------------

# Maps extraction template field names → (store, store_field).
# These are aliases for common extraction names that differ from store_field names.
# The store_field → (store, store_field) mappings are auto-derived from field_mapping.json.
_TEMPLATE_ALIASES = {
    "deal_name":                        ("clo-deals", "Title"),
    "title":                            ("clo-deals", "Title"),
    "collateral_manager_legal_entity":  ("clo-deals", "Collateral Manager"),
    "collateral_type":                  ("clo-deals", "Collateral Type"),
    "deal_type":                        ("clo-deals", "Collateral Type"),
    "collateral_manager_short":         ("clo-managers", "Short Name"),
    "arranger":                         ("clo-transactions", "Placement Agent"),
    "transaction_type":                 ("clo-transactions", "Transaction Type"),
    "term":                             ("clo-transactions", "Term"),
    "ipt":                              ("clo-transactions", "IPT"),
    "final_pricing":                    ("clo-transactions", "Final Pricing Details"),
    "announced_date":                   ("clo-transactions", "Announcement Date"),
    "priced_date":                      ("clo-transactions", "Priced Date"),
}


def _build_extraction_to_store(mapping: dict) -> dict:
    """Build full extraction→store mapping from aliases + field_mapping.json config."""
    result = dict(_TEMPLATE_ALIASES)
    for gui_key, cfg in mapping.get("fields", {}).items():
        store = cfg.get("store")
        store_field = cfg.get("store_field")
        if store and store_field:
            result[store_field] = (store, store_field)
    return result


def map_extraction_to_gui(extraction: dict, mapping: dict = None) -> dict:
    """
    Map extraction field names to GUI-compatible namespaced keys.

    Takes a flat extraction dict (e.g. {"deal_name": "...", "arranger": "..."})
    and returns a new dict with BOTH the original keys AND the namespaced keys
    (e.g. {"deal_name": "...", "clo-deals__Title": "...", "clo-transactions__Placement Agent": "..."}).

    This ensures the GUI form fields (which use namespaced keys from field_mapping.json)
    get populated from rule-based extraction results.
    """
    if mapping is None:
        mapping = load_mapping()

    result = dict(extraction)  # preserve original keys

    # Strategy 1: Use template aliases + auto-derived store_field mappings
    ext_to_store = _build_extraction_to_store(mapping)
    for ext_key, (store, store_field) in ext_to_store.items():
        val = extraction.get(ext_key)
        if val is not None and val != "":
            gui_key = f"{store}__{store_field}"
            result.setdefault(gui_key, val)

    # Strategy 2: For any field in the mapping config, try to find the value
    # by matching on store_field name (handles fields not in the static table)
    for gui_key, cfg in mapping.get("fields", {}).items():
        if gui_key in result and result[gui_key]:
            continue  # already set
        store_field = cfg.get("store_field")
        if store_field and store_field in extraction:
            val = extraction[store_field]
            if val is not None and val != "":
                result[gui_key] = val

    # Also map email_type → Status for the transactions store
    email_type = extraction.get("email_type", "")
    if email_type:
        status_map = {"announced": "Announced", "updated": "Announced", "priced": "Priced"}
        status = status_map.get(email_type, email_type.capitalize())
        result.setdefault("clo-transactions__Status", status)

    # Map deal_name → clo-transactions__Deal and clo-transactions__Title
    deal_name = extraction.get("deal_name") or extraction.get("title") or extraction.get("Title")
    if deal_name:
        result.setdefault("clo-transactions__Deal", deal_name)
        result.setdefault("clo-transactions__Title", deal_name)
        result.setdefault("clo-transactions__Collateral Type",
                          extraction.get("collateral_type") or extraction.get("deal_type") or "")

    # Map manager to both deals and managers store
    mgr = extraction.get("collateral_manager_legal_entity") or extraction.get("Collateral Manager")
    if mgr:
        result.setdefault("clo-managers__Name", mgr)

    return result
