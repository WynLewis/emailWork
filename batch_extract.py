"""
Batch extraction — process all emails + BBG CSV non-interactively.
===================================================================

Runs all extraction pipelines without the GUI, then shows a single
review table of every pending update so you can accept/edit at once.

Usage:
    python batch_extract.py                      # process inbox + latest BBG CSV
    python batch_extract.py --emails-only        # only process emails
    python batch_extract.py --bbg-only           # only process BBG CSV
    python batch_extract.py --csv path/to/file   # specify BBG CSV path
    python batch_extract.py --dry-run            # show what would be updated, don't save

Output:
    Prints a review table to stdout. With --save, writes accepted
    records to clo-deals.json, clo-transactions.json, clo-managers.json.
"""

import sys
import os
import glob
import json
import argparse
from datetime import datetime

# Ensure project root is on path
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Force rules-based extractor (no LLM needed for batch)
os.environ.setdefault("CLO_EXTRACTOR", "rules")
os.environ.setdefault("CLO_RULES_MODULE", "my_rules.py")

from backend import config, store
from backend.watcher import EmailQueue
from backend.extractor import get_extractor, format_tranche_pricing
from backend.field_mapping import (
    load_mapping, map_extraction_to_gui, build_store_record,
    STORE_DEALS, STORE_TRANSACTIONS, STORE_MANAGERS,
)
from backend.bbg_pricing import parse_pricing_csv, is_already_priced


# ---------------------------------------------------------------------------
# Email extraction
# ---------------------------------------------------------------------------

def extract_all_emails(extractor) -> list[dict]:
    """Extract fields from all emails in the inbox. Returns list of extraction dicts."""
    queue = EmailQueue()
    results = []

    if queue.size() == 0:
        print("  No emails in inbox.")
        return results

    print(f"  Found {queue.size()} emails in inbox.")

    while queue.size() > 0:
        fname = queue.peek()
        if not fname:
            break

        html = queue.read_file(fname)

        # Set filename env var for date extraction
        os.environ["CLO_EMAIL_FILENAME"] = fname

        # Run extraction
        try:
            result = extractor.extract(html)
            extraction = result.model_dump()
        except Exception as e:
            print(f"  ERROR extracting {fname}: {e}")
            queue.pop()
            continue

        # Add pricing from HTML tables
        pricing = format_tranche_pricing(html)
        for key in ("ipt", "updated_guidance", "final_pricing"):
            if pricing.get(key):
                extraction[key] = pricing[key]

        # Map to GUI field names
        extraction = map_extraction_to_gui(extraction)

        results.append({
            "source": "email",
            "filename": fname,
            "extraction": extraction,
        })

        queue.pop()

    return results


# ---------------------------------------------------------------------------
# BBG CSV extraction
# ---------------------------------------------------------------------------

def extract_all_bbg(csv_path: str) -> list[dict]:
    """Extract deal info from BBG pricing CSV. Returns list of extraction dicts."""
    if not csv_path or not os.path.isfile(csv_path):
        print(f"  No BBG CSV found at {csv_path}")
        return []

    priced_deals = parse_pricing_csv(csv_path)
    print(f"  Found {len(priced_deals)} priced deals in {os.path.basename(csv_path)}")

    results = []
    for deal in priced_deals:
        deal_name = deal["deal_name"]

        # Skip already processed
        if is_already_priced(deal_name, deal["pricing_date"]):
            continue

        # Build extraction dict in the same format as email extractions
        extraction = {
            "email_type": "priced",
            "deal_name": deal_name,
            "title": deal_name,
            "arranger": deal.get("lead_mgr_full", ""),
            "transaction_type": deal.get("transaction_type", "New Issue"),
            "deal_type": deal.get("deal_type", ""),
            "collateral_type": deal.get("deal_type", ""),
            "priced_date": deal.get("pricing_date", ""),
        }

        # Map to GUI field names
        extraction = map_extraction_to_gui(extraction)

        results.append({
            "source": "bbg",
            "filename": os.path.basename(csv_path),
            "deal_info": deal,
            "extraction": extraction,
        })

    skipped = len(priced_deals) - len(results)
    if skipped:
        print(f"  Skipped {skipped} already-priced deals.")

    return results


# ---------------------------------------------------------------------------
# Review display
# ---------------------------------------------------------------------------

def _get_val(ext: dict, *keys: str) -> str:
    """Get first non-empty value from extraction dict."""
    for k in keys:
        v = ext.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def display_review(all_results: list[dict]) -> None:
    """Print a review table of all pending extractions."""
    if not all_results:
        print("\nNo pending extractions to review.")
        return

    print(f"\n{'='*120}")
    print(f"BATCH EXTRACTION REVIEW — {len(all_results)} pending updates")
    print(f"{'='*120}")

    # Group by source
    email_results = [r for r in all_results if r["source"] == "email"]
    bbg_results = [r for r in all_results if r["source"] == "bbg"]

    if email_results:
        print(f"\n{'─'*120}")
        print(f"EMAILS ({len(email_results)} extractions)")
        print(f"{'─'*120}")
        _print_table(email_results)

    if bbg_results:
        print(f"\n{'─'*120}")
        print(f"BLOOMBERG ({len(bbg_results)} priced deals)")
        print(f"{'─'*120}")
        _print_table(bbg_results)

    # Summary
    print(f"\n{'='*120}")
    print("STORE IMPACT SUMMARY")
    print(f"{'='*120}")
    _print_store_impact(all_results)


def _print_table(results: list[dict]) -> None:
    """Print extraction results as a table."""
    print(f"\n{'#':>3s}  {'Source':6s}  {'Status':10s}  {'Type':12s}  {'Deal Name':40s}  {'Manager':30s}  {'Bank':15s}  {'Term':8s}  {'CType':5s}")
    print(f"{'─'*3}  {'─'*6}  {'─'*10}  {'─'*12}  {'─'*40}  {'─'*30}  {'─'*15}  {'─'*8}  {'─'*5}")

    for i, r in enumerate(results, 1):
        ext = r["extraction"]
        deal = _get_val(ext, "clo-deals__Title", "deal_name", "title", "Title")[:40]
        mgr = _get_val(ext, "clo-deals__Collateral Manager", "collateral_manager_legal_entity", "Collateral Manager")[:30]
        bank = _get_val(ext, "clo-transactions__Placement Agent", "arranger", "Placement Agent")[:15]
        txn = _get_val(ext, "clo-transactions__Transaction Type", "transaction_type", "Transaction Type")[:12]
        term = _get_val(ext, "clo-transactions__Term", "term", "Term")[:8]
        ctype = _get_val(ext, "clo-deals__Collateral Type", "deal_type", "Collateral Type")[:5]
        status = _get_val(ext, "clo-transactions__Status", "email_type")[:10]
        src = r["source"][:6]

        print(f"{i:3d}  {src:6s}  {status:10s}  {txn:12s}  {deal:40s}  {mgr:30s}  {bank:15s}  {term:8s}  {ctype:5s}")

    # Show pricing details for any that have them
    has_pricing = False
    for i, r in enumerate(results, 1):
        ext = r["extraction"]
        ipt = _get_val(ext, "clo-transactions__IPT", "ipt", "IPT")
        fpd = _get_val(ext, "clo-transactions__Final Pricing Details", "final_pricing", "Final Pricing Details")
        pricing = fpd or ipt
        if pricing:
            if not has_pricing:
                print(f"\n  PRICING DETAILS:")
                has_pricing = True
            deal = _get_val(ext, "clo-deals__Title", "deal_name")[:35]
            label = "FINAL" if fpd else "IPT"
            # Truncate multi-line pricing to first 3 lines
            lines = pricing.split("\n")[:3]
            suffix = f" (+{len(pricing.split(chr(10)))-3} more)" if len(pricing.split("\n")) > 3 else ""
            print(f"  #{i} {deal} [{label}]: {' | '.join(lines)}{suffix}")


def _print_store_impact(all_results: list[dict]) -> None:
    """Show what would be created/updated in each store."""
    mapping = load_mapping()
    new_deals = 0
    update_deals = 0
    new_txns = 0
    update_txns = 0
    new_mgrs = 0

    for r in all_results:
        ext = r["extraction"]
        deal_title = _get_val(ext, "clo-deals__Title", "deal_name", "title", "Title")
        mgr_short = _get_val(ext, "clo-managers__Short Name", "collateral_manager_short", "Short Name")

        if deal_title:
            existing = store.find_record(store.DEALS, lambda r, dt=deal_title: r.get("Title", "").strip() == dt.strip())
            if existing:
                update_deals += 1
            else:
                new_deals += 1

        if deal_title:
            status = _get_val(ext, "clo-transactions__Status", "email_type")
            is_priced = status.lower() == "priced"
            existing_txn = store.find_record(
                store.TRANSACTIONS,
                lambda r, dt=deal_title: r.get("Deal", "").strip() == dt.strip() and r.get("Status") != "Priced"
            )
            if existing_txn and is_priced:
                update_txns += 1
            else:
                new_txns += 1

        if mgr_short:
            existing_mgr = store.find_record(
                store.MANAGERS,
                lambda r, ms=mgr_short: r.get("Short Name", "").strip().lower() == ms.strip().lower()
            )
            if not existing_mgr:
                new_mgrs += 1

    print(f"  clo-deals.json:        {new_deals} new, {update_deals} updates")
    print(f"  clo-transactions.json: {new_txns} new, {update_txns} updates")
    print(f"  clo-managers.json:     {new_mgrs} new")
    print()


# ---------------------------------------------------------------------------
# Save to stores
# ---------------------------------------------------------------------------

def save_all(all_results: list[dict]) -> None:
    """Write all extractions to the JSON stores."""
    mapping = load_mapping()
    now = datetime.utcnow().isoformat()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    saved_deals = 0
    saved_txns = 0
    saved_mgrs = 0

    for r in all_results:
        ext = r["extraction"]
        deal_title = _get_val(ext, "clo-deals__Title", "deal_name", "title", "Title")
        status = _get_val(ext, "clo-transactions__Status", "email_type")
        is_priced = status.lower() == "priced"

        # Build store records
        deal_data = build_store_record(ext, STORE_DEALS, mapping)
        txn_data = build_store_record(ext, STORE_TRANSACTIONS, mapping)
        mgr_data = build_store_record(ext, STORE_MANAGERS, mapping)

        # --- Deal ---
        if deal_data and deal_title:
            existing = store.find_record(store.DEALS, lambda rec, dt=deal_title: rec.get("Title", "").strip() == dt.strip())
            if existing:
                store.update_record(store.DEALS, lambda rec, dt=deal_title: rec.get("Title", "").strip() == dt.strip(), deal_data)
            else:
                store.append_record(store.DEALS, deal_data)
            saved_deals += 1

        # --- Transaction ---
        if txn_data and deal_title:
            txn_data.setdefault("Deal", deal_title)
            txn_data.setdefault("Title", deal_title)
            if is_priced:
                txn_data.setdefault("Status", "Priced")
                txn_data.setdefault("Priced Date", _get_val(ext, "clo-transactions__Priced Date", "priced_date") or today)
                existing_txn = store.find_record(
                    store.TRANSACTIONS,
                    lambda rec, dt=deal_title: rec.get("Deal", "").strip() == dt.strip() and rec.get("Status") != "Priced"
                )
                if existing_txn:
                    store.update_record(
                        store.TRANSACTIONS,
                        lambda rec, dt=deal_title: rec.get("Deal", "").strip() == dt.strip() and rec.get("Status") != "Priced",
                        txn_data,
                    )
                else:
                    store.append_record(store.TRANSACTIONS, txn_data)
            else:
                txn_data.setdefault("Status", "Announced")
                txn_data.setdefault("Announcement Date", _get_val(ext, "clo-transactions__Announcement Date", "announced_date") or today)
                store.append_record(store.TRANSACTIONS, txn_data)
            saved_txns += 1

        # --- Manager ---
        mgr_short = _get_val(ext, "clo-managers__Short Name", "collateral_manager_short", "Short Name")
        if mgr_data and mgr_short:
            existing_mgr = store.find_record(
                store.MANAGERS,
                lambda rec, ms=mgr_short: rec.get("Short Name", "").strip().lower() == ms.strip().lower()
            )
            if not existing_mgr:
                store.append_record(store.MANAGERS, mgr_data)
                saved_mgrs += 1

    print(f"\nSAVED: {saved_deals} deals, {saved_txns} transactions, {saved_mgrs} new managers")


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def export_json(all_results: list[dict], path: str) -> None:
    """Export all extractions to a JSON file for review/editing."""
    export = []
    for r in all_results:
        ext = r["extraction"]
        export.append({
            "source": r["source"],
            "filename": r.get("filename", ""),
            "deal_name": _get_val(ext, "clo-deals__Title", "deal_name"),
            "status": _get_val(ext, "clo-transactions__Status", "email_type"),
            "transaction_type": _get_val(ext, "clo-transactions__Transaction Type", "transaction_type"),
            "placement_agent": _get_val(ext, "clo-transactions__Placement Agent", "arranger"),
            "manager": _get_val(ext, "clo-deals__Collateral Manager", "collateral_manager_legal_entity"),
            "manager_short": _get_val(ext, "clo-managers__Short Name", "collateral_manager_short"),
            "term": _get_val(ext, "clo-transactions__Term", "term"),
            "collateral_type": _get_val(ext, "clo-deals__Collateral Type", "deal_type"),
            "ipt": _get_val(ext, "clo-transactions__IPT", "ipt"),
            "final_pricing": _get_val(ext, "clo-transactions__Final Pricing Details", "final_pricing"),
            "priced_date": _get_val(ext, "clo-transactions__Priced Date", "priced_date"),
            "announced_date": _get_val(ext, "clo-transactions__Announcement Date", "announced_date"),
        })

    with open(path, "w") as f:
        json.dump(export, f, indent=2)
    print(f"\nExported {len(export)} extractions to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Batch extraction — process all emails + BBG CSV")
    parser.add_argument("--emails-only", action="store_true", help="Only process emails")
    parser.add_argument("--bbg-only", action="store_true", help="Only process BBG CSV")
    parser.add_argument("--csv", type=str, help="Path to BBG pricing CSV")
    parser.add_argument("--dry-run", action="store_true", help="Show review only, don't save")
    parser.add_argument("--save", action="store_true", help="Save all extractions to stores")
    parser.add_argument("--export", type=str, help="Export extractions to JSON file for editing")
    args = parser.parse_args()

    all_results = []

    # --- Emails ---
    if not args.bbg_only:
        print("\n[1/2] PROCESSING EMAILS")
        print("─" * 40)
        extractor = get_extractor()
        email_results = extract_all_emails(extractor)
        all_results.extend(email_results)

    # --- BBG CSV ---
    if not args.emails_only:
        print("\n[2/2] PROCESSING BLOOMBERG CSV")
        print("─" * 40)
        csv_path = args.csv
        if not csv_path:
            csvs = sorted(glob.glob(os.path.join(ROOT, "pricings_*.csv")))
            csv_path = csvs[-1] if csvs else None
        bbg_results = extract_all_bbg(csv_path)
        all_results.extend(bbg_results)

    # --- Review ---
    display_review(all_results)

    # --- Export ---
    if args.export:
        export_json(all_results, args.export)

    # --- Save ---
    if args.save and not args.dry_run:
        save_all(all_results)
    elif all_results and not args.dry_run and not args.export:
        print("\nTo save these extractions, re-run with --save")
        print("To export for editing, re-run with --export batch_results.json")


if __name__ == "__main__":
    main()
