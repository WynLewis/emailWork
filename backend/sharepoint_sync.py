"""
Sharepoint JSON converter for CLO Email Extraction.
=====================================================

WHAT THIS FILE DOES:
    Converts between Sharepoint list export format (schema XML keys with
    display name values) and the flat JSON format used by the extractor.

    Two directions:
        1. flatten_sharepoint()  — Sharepoint export → flat records for data/
        2. to_sharepoint()       — flat records → Sharepoint-compatible format

    This lets you:
        - Drop Sharepoint exports into data/, flatten them, and work with them
        - Accept email extractions through the GUI
        - Sync new/updated records back to Sharepoint format

USAGE:
    python -m backend.sharepoint_sync flatten   # Sharepoint → flat
    python -m backend.sharepoint_sync export    # flat → Sharepoint

SHAREPOINT SCHEMA:
    Sharepoint list exports have keys that are XML field definitions and
    values that are display names or data values. The actual field identity
    is in the StaticName attribute of the XML key.

    This module parses that schema, extracts the StaticName → DisplayName
    mapping, and uses it to convert between formats.
"""

import json
import os
import re
import logging
from datetime import datetime

from backend import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SHAREPOINT SCHEMA — Parsed from your actual list exports
# ---------------------------------------------------------------------------
# Maps StaticName → (display_name, flat_field_name, field_type)
#
# flat_field_name is what the extractor uses internally.
# field_type helps with serialization (dates, booleans, etc.)

DEALS_SCHEMA = {
    "Title":              ("Deal Name",           "deal_name",                        "text"),
    "CollateralType":     ("Collateral Type",     "deal_type",                        "text"),
    "CollateralManager":  ("Collateral Manager",  "collateral_manager_legal_entity",  "text"),
    "BloombergDealName":  ("Bloomberg Deal Name", "bloomberg_deal_name",              "text"),
    "IntexDeal":          ("Intex Deal",          "intex_deal",                       "text"),
    "IntexPreprice":      ("Intex Preprice",      "intex_preprice",                   "text"),
    "DealDocuments":      ("Deal Documents",      "deal_documents",                   "url"),
}

MANAGERS_SCHEMA = {
    "Title":              ("Name",                "manager_name",                     "text"),
    "ShortName":          ("Short Name",          "collateral_manager_short",         "text"),
    "UltimateParent":     ("Ultimate Parent",     "ultimate_parent",                  "text"),
    "SECNo_x002e_":       ("SEC Number",          "sec_number",                       "text"),
    "CRDNo_x002e_":       ("CRD Number",          "crd_number",                       "text"),
    "Website":            ("Website",             "website",                          "url"),
}

TRANSACTIONS_SCHEMA = {
    "Title":              ("Title",               "deal_name",                        "text"),
    "TransactionType":    ("Transaction Type",    "transaction_type",                 "choice"),
    "Status":             ("Status",              "status",                           "choice"),
    "AnnouncementDate":   ("Announcement Date",   "announced_date",                   "datetime"),
    "PricedDate":         ("Priced Date",         "priced_date",                      "datetime"),
    "PlacementAgent":     ("Placement Agent",     "arranger",                         "choice"),
    "Engaged":            ("Engaged",             "engaged",                          "boolean"),
    "IPT":                ("IPT",                 "ipt",                              "note"),
    "Term":               ("Term",                "term",                             "text"),
    "FinalPricingDetails":("Final Pricing Details","final_pricing",                   "note"),
    "Executed":           ("Executed",            "executed",                         "boolean"),
}

ALL_SCHEMAS = {
    "deals": DEALS_SCHEMA,
    "managers": MANAGERS_SCHEMA,
    "transactions": TRANSACTIONS_SCHEMA,
}

# Reverse lookup: flat_field_name → (store, static_name, field_type)
_FLAT_TO_SP = {}
for store_name, schema in ALL_SCHEMAS.items():
    for static_name, (display, flat_name, ftype) in schema.items():
        _FLAT_TO_SP[flat_name] = (store_name, static_name, ftype)


# ---------------------------------------------------------------------------
# PARSE SHAREPOINT SCHEMA FROM EXPORT
# ---------------------------------------------------------------------------

def parse_sharepoint_schema(record: dict) -> dict:
    """
    Parse field definitions from a Sharepoint list export record.

    The keys contain XML field definitions with StaticName attributes.
    Returns: {StaticName: display_name_or_value}
    """
    fields = {}
    for key, value in record.items():
        # Extract StaticName from the XML
        m = re.search(r'StaticName=\\*"([^"\\]+)', key)
        if m:
            static_name = m.group(1)
            # Skip computed/system fields
            ftype_match = re.search(r'Type=\\*"([^"\\]+)', key)
            ftype = ftype_match.group(1) if ftype_match else ""
            if ftype == "Computed":
                continue
            fields[static_name] = value
    return fields


# ---------------------------------------------------------------------------
# FLATTEN — Sharepoint export → flat records
# ---------------------------------------------------------------------------

def flatten_sharepoint_record(sp_record: dict, schema: dict) -> dict:
    """
    Convert one Sharepoint record to a flat record using the schema mapping.

    Parameters:
        sp_record — dict with Sharepoint StaticName keys (already parsed)
        schema    — one of DEALS_SCHEMA, MANAGERS_SCHEMA, TRANSACTIONS_SCHEMA
    """
    flat = {}
    for static_name, (display, flat_name, ftype) in schema.items():
        value = sp_record.get(static_name)
        if value is None:
            continue

        # Type conversions
        if ftype == "boolean":
            if isinstance(value, str):
                value = value.lower() in ("1", "true", "yes")
            else:
                value = bool(value)
        elif ftype == "datetime":
            # Sharepoint dates come as ISO strings, keep as-is
            if isinstance(value, str) and "T" in value:
                value = value.split("T")[0]  # Just the date part
        elif ftype == "url":
            # Sharepoint URLs can be {"Url": "...", "Description": "..."}
            if isinstance(value, dict):
                value = value.get("Url", str(value))

        flat[flat_name] = value

    return flat


def flatten_sharepoint_file(input_path: str, store_name: str) -> list[dict]:
    """
    Read a Sharepoint JSON export and return flat records.

    Handles two formats:
        1. Schema export: keys are XML definitions, values are display names
           (like the json_samples.txt format — this is just schema, no data)
        2. Data export: array of records with StaticName keys and actual values
    """
    with open(input_path, "r") as f:
        data = json.load(f)

    schema = ALL_SCHEMAS.get(store_name)
    if not schema:
        logger.warning(f"No schema defined for store '{store_name}'")
        return []

    records = data if isinstance(data, list) else [data]
    flat_records = []

    for record in records:
        # Check if this is a schema-only record (values are display names / null)
        # vs actual data (values are real data)
        parsed = parse_sharepoint_schema(record)
        if parsed:
            # Keys had XML — this is Sharepoint format, use parsed StaticNames
            flat = flatten_sharepoint_record(parsed, schema)
        else:
            # Keys are already plain — try direct mapping
            flat = flatten_sharepoint_record(record, schema)

        if flat:
            flat_records.append(flat)

    return flat_records


# ---------------------------------------------------------------------------
# TO SHAREPOINT — flat records → Sharepoint-compatible format
# ---------------------------------------------------------------------------

def to_sharepoint_record(flat_record: dict, schema: dict) -> dict:
    """
    Convert a flat record back to Sharepoint StaticName format.

    Returns a dict with StaticName keys and properly typed values,
    ready to be merged back into a Sharepoint export or used with
    the Sharepoint REST API.
    """
    sp = {}

    # Build reverse map for this schema: flat_name → (static_name, ftype)
    reverse = {}
    for static_name, (display, flat_name, ftype) in schema.items():
        reverse[flat_name] = (static_name, ftype)

    for flat_name, value in flat_record.items():
        if flat_name not in reverse:
            continue

        static_name, ftype = reverse[flat_name]

        # Type conversions back to Sharepoint format
        if ftype == "boolean":
            value = 1 if value else 0
        elif ftype == "datetime" and value:
            # Sharepoint expects ISO datetime
            if isinstance(value, str) and "T" not in value:
                value = value + "T00:00:00Z"

        sp[static_name] = value

    return sp


# ---------------------------------------------------------------------------
# SYNC — Compare flat records with Sharepoint export, find new/updated
# ---------------------------------------------------------------------------

def find_new_records(flat_records: list[dict], sp_records: list[dict],
                     key_field: str = "deal_name") -> list[dict]:
    """
    Find records in flat_records that don't exist in sp_records.

    Parameters:
        flat_records — current flat records from data/*.json
        sp_records   — existing Sharepoint records (already flattened)
        key_field    — field to match on (default: deal_name)

    Returns: list of flat records that are new (not in Sharepoint)
    """
    existing_keys = {r.get(key_field) for r in sp_records if r.get(key_field)}
    return [r for r in flat_records if r.get(key_field) and r[key_field] not in existing_keys]


def find_updated_records(flat_records: list[dict], sp_records: list[dict],
                         key_field: str = "deal_name") -> list[tuple]:
    """
    Find records that exist in both but have different values.

    Returns: [(flat_record, sp_record, changed_fields), ...]
    """
    sp_by_key = {}
    for r in sp_records:
        k = r.get(key_field)
        if k:
            sp_by_key[k] = r

    updates = []
    for flat in flat_records:
        k = flat.get(key_field)
        if not k or k not in sp_by_key:
            continue
        sp = sp_by_key[k]
        changed = []
        for field, value in flat.items():
            if value is not None and value != "" and value != sp.get(field):
                changed.append(field)
        if changed:
            updates.append((flat, sp, changed))

    return updates


# ---------------------------------------------------------------------------
# EXPORT — Write flat records back to Sharepoint format
# ---------------------------------------------------------------------------

def export_to_sharepoint(store_name: str, output_path: str = None) -> str:
    """
    Export a flat JSON store to Sharepoint-compatible format.

    Reads from data/{store_name}.json, converts each record using the
    schema, and writes to output_path (default: data/{store_name}_sharepoint.json).

    Returns the output file path.
    """
    from backend import store as store_module

    schema = ALL_SCHEMAS.get(store_name)
    if not schema:
        raise ValueError(f"No schema for store '{store_name}'")

    store_file = f"{store_name}.json"
    flat_records = store_module.read_store(store_file)

    sp_records = []
    for flat in flat_records:
        sp = to_sharepoint_record(flat, schema)
        if sp:
            sp_records.append(sp)

    if output_path is None:
        output_path = os.path.join(config.DATA_DIR, f"{store_name}_sharepoint.json")

    with open(output_path, "w") as f:
        json.dump(sp_records, f, indent=2)

    logger.info(f"Exported {len(sp_records)} records to {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m backend.sharepoint_sync flatten  — Sharepoint exports → flat JSONs")
        print("  python -m backend.sharepoint_sync export   — flat JSONs → Sharepoint format")
        print("  python -m backend.sharepoint_sync diff     — show new/updated records vs Sharepoint")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "flatten":
        for store_name in ALL_SCHEMAS:
            sp_path = os.path.join(config.DATA_DIR, f"{store_name}_sp_export.json")
            if not os.path.isfile(sp_path):
                print(f"  Skip {store_name} — no {sp_path} found")
                continue
            flat = flatten_sharepoint_file(sp_path, store_name)
            out_path = os.path.join(config.DATA_DIR, f"{store_name}.json")
            with open(out_path, "w") as f:
                json.dump(flat, f, indent=2)
            print(f"  {store_name}: {len(flat)} records → {out_path}")

    elif cmd == "export":
        for store_name in ALL_SCHEMAS:
            try:
                out = export_to_sharepoint(store_name)
                print(f"  {store_name} → {out}")
            except Exception as e:
                print(f"  {store_name}: {e}")

    elif cmd == "diff":
        from backend import store as store_module
        for store_name, schema in ALL_SCHEMAS.items():
            sp_path = os.path.join(config.DATA_DIR, f"{store_name}_sp_export.json")
            if not os.path.isfile(sp_path):
                continue
            sp_flat = flatten_sharepoint_file(sp_path, store_name)
            store_file = f"{store_name}.json"
            current = store_module.read_store(store_file)

            key = "deal_name" if store_name != "managers" else "collateral_manager_short"
            new = find_new_records(current, sp_flat, key)
            updated = find_updated_records(current, sp_flat, key)

            print(f"\n{store_name}:")
            print(f"  {len(new)} new records")
            for r in new:
                print(f"    + {r.get(key, '?')}")
            print(f"  {len(updated)} updated records")
            for flat, sp, changed in updated:
                print(f"    ~ {flat.get(key, '?')} ({', '.join(changed)})")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
