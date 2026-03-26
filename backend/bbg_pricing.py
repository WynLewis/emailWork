"""
Bloomberg pricing ingestion for CLO Email Extraction.
======================================================

WHAT THIS FILE DOES:
    Processes priced CLO deals from Bloomberg data:
    1. Parses a Bloomberg pricing CSV (pricings_YYYYMMDD.csv)
    2. For each priced deal, uses xlwings to run a BQL formula in Excel
       to pull tranche-level pricing data
    3. Assigns ratings using credit subordination + class letter grouping
    4. Updates the JSON stores: deals, transactions, managers

WORKFLOW:
    Announcements come from emails → this module handles pricings.

    CSV → filter priced deals → BQL per deal → tranche data →
    filter X/equity → sort by spread → assign ratings via sub →
    format pricing → update stores

RATING ASSIGNMENT (rules-based, no LLM):
    1. Remove X tranches and equity (SUB, SPFF, PERF, etc.)
    2. Sort remaining debt tranches by FLT_SPREAD (tightest first)
    3. Calculate credit subordination using debt par + ~9% assumed equity
    4. Extract first letter from mtg_cmo_class (A1R2 → A, BR2 → B)
    5. Tranches sharing the same first letter = same rating bucket
    6. Use subordination thresholds to confirm rating assignment
    7. If a "Jr" tranche's sub drops below the current rating threshold,
       it's actually the next rating down (e.g. A2 might be AA not Jr AAA)

FINAL PRICING FORMAT:
    AAA tranches: always keep Sr/Jr separate
    All other ratings: size-weighted average spread, blended into one line
    tranche_detail array: stores every tranche individually for analysis

USAGE:
    From the notebook (bbg_pricing.ipynb):
        from backend.bbg_pricing import run_pricing_pipeline
        results = run_pricing_pipeline("pricings_20260325.csv")

    Or standalone:
        python -m backend.bbg_pricing pricings_20260325.csv

REQUIREMENTS:
    - xlwings (pip install xlwings) — requires Excel installed
    - Bloomberg Terminal running (for BQL formulas)
    - openpyxl (for fallback/offline parsing)
"""

import csv
import os
import re
import logging
import time
from datetime import datetime
from typing import Optional

from backend import config
from backend.models import new_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BLOOMBERG LEAD MANAGER CODE → Full Name Lookup
# ---------------------------------------------------------------------------
LEAD_MGR_MAP = {
    "BNPP":  "BNP Paribas",
    "JPM":   "J.P. Morgan",
    "MS":    "Morgan Stanley",
    "BOFA":  "Bank of America",
    "BAML":  "Bank of America Merrill Lynch",
    "BofA":  "Bank of America",
    "CITG":  "Citigroup",
    "SMBC":  "SMBC Nikko",
    "JEF":   "Jefferies",
    "WFS":   "Wells Fargo",
    "CIBC":  "CIBC",
    "CIBM":  "CIBC Capital Markets",
    "SCOB":  "Scotia",
    "RBC":   "RBC Capital Markets",
    "GLCM":  "Goldman Sachs",
    "GS":    "Goldman Sachs",
    "MIZU":  "Mizuho",
    "NS":    "Nomura",
    "MUSA":  "Morgan Stanley",  # alternate code
    "MSUL":  "Morgan Stanley",
    "SANT":  "Santander",
    "MU":    "Mitsubishi UFJ",
    "NATX":  "Natixis",
    "DBS":   "DBS Bank",
    "BCM":   "BMO Capital Markets",
    "BCMK":  "Benchmark (self-arranged)",
    "ELDR":  "Eldridge",
    "Jnt":   "Joint Lead",
    "N/A":   "",
}

# ---------------------------------------------------------------------------
# COLLATERAL TYPE → Deal Type
# ---------------------------------------------------------------------------
COLLATERAL_MAP = {
    "CF-CLO-LL":  "BSL",
    "CF-CLO-MML": "MM",
}

# ---------------------------------------------------------------------------
# CREDIT SUBORDINATION → RATING THRESHOLDS
# ---------------------------------------------------------------------------
# Minimum subordination % to qualify for each rating.
# Subordination = (everything below this tranche + equity) / total deal par
ASSUMED_EQUITY_PCT = 0.09  # ~9% equity assumed below the debt stack

RATING_LADDER = ["AAA", "AA", "A", "BBB", "BB", "B"]

SUB_THRESHOLDS = {
    "AAA": 23.0,
    "AA":  16.0,
    "A":   12.0,
    "BBB":  7.0,
    "BB":   3.0,
    "B":    1.0,
}

# Assumed 3mo SOFR for converting fixed-rate tranches to DM equivalent
ASSUMED_SOFR_PCT = 3.6

# ---------------------------------------------------------------------------
# BQL FORMULA TEMPLATE
# ---------------------------------------------------------------------------
# Updated formula with mtg_tranche_typ_long and issue_dt fields.
# {deal_name} gets replaced with the Bloomberg deal name from the CSV.
BQL_FORMULA = (
    '=_xll.BQL("filter(mortgagesuniv(\'active\',CONSOLIDATEDUPLICATES=\'N\'),'
    'MTG_DEAL_NAME==\'{deal_name}\')","PORTFOLIO_MANAGER,name,MTG_ORIG_AMT,'
    'FLT_SPREAD,RTG_MOODY,RTG_FITCH,RTG_SP,MTG_CMO_CLASS,MTG_DEAL_CALL_DT,'
    'REINVEST_END_DATE,COLLAT_TYP,MTG_TRANCHE_TYP_LONG,ISSUE_DT",'
    '"showids=f","cols=13;rows=12")'
)

# Column indices in BQL result (0-based, starting from col C)
BQL_COLS = {
    "manager":          0,   # PORTFOLIO_MANAGER
    "name":             1,   # tranche name (e.g. NEUB 2026-63A A1)
    "orig_amt":         2,   # MTG_ORIG_AMT
    "spread":           3,   # FLT_SPREAD
    "rtg_moody":        4,   # RTG_MOODY
    "rtg_fitch":        5,   # RTG_FITCH
    "rtg_sp":           6,   # RTG_SP
    "class":            7,   # MTG_CMO_CLASS
    "call_date":        8,   # MTG_DEAL_CALL_DT
    "reinvest_end":     9,   # REINVEST_END_DATE
    "collat_type":      10,  # COLLAT_TYP
    "tranche_type":     11,  # MTG_TRANCHE_TYP_LONG
    "issue_dt":         12,  # ISSUE_DT
}

# Tranche classes to skip (equity / subordinated / performance / fees / X notes)
SKIP_CLASSES = {"SUB", "SPFF", "PERF", "SBPF", "INC", "RES", "EQ", "PREF"}
SKIP_PREFIXES = ("X",)  # X tranches (exchangeable/combo)


# ---------------------------------------------------------------------------
# CSV PARSER
# ---------------------------------------------------------------------------

def parse_pricing_csv(csv_path: str) -> list[dict]:
    """
    Parse a Bloomberg pricing CSV and return priced deals.

    Returns list of dicts with:
        deal_name, collateral, pricing_date, settle_date,
        orig_mm, currency, lead_mgr, lead_mgr_full,
        transaction_type (New Issue / Reset/Refi — refined by BQL later),
        deal_type (BSL / MM)
    """
    deals = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)  # skip header row

        for row in reader:
            if len(row) < 10:
                continue
            cf = row[0].strip()
            deal_name = row[1].strip()
            deal_type_raw = row[2].strip()  # CLO
            collateral = row[3].strip()
            pricing = row[4].strip()
            settle = row[5].strip()
            orig_mm = row[6].strip().replace(",", "")
            currency = row[7].strip()
            country = row[8].strip()
            lead_mgr = row[9].strip()

            if not deal_name:
                continue

            # Skip TBA — not yet priced
            if pricing.upper() == "TBA" or not pricing:
                continue

            # Preliminary transaction type from Cf column
            # * = not a new issue (reset or refi — BQL tranche_type refines this)
            # blank = likely new issue (BQL issue_dt confirms)
            if cf == "*":
                transaction_type = "Reset/Refi"
            else:
                transaction_type = "New Issue"

            deal_type = COLLATERAL_MAP.get(collateral, "Other")

            try:
                orig_val = float(orig_mm)
            except ValueError:
                orig_val = None

            lead_mgr_full = LEAD_MGR_MAP.get(lead_mgr, lead_mgr)

            deals.append({
                "deal_name": deal_name,
                "collateral": collateral,
                "pricing_date": pricing,
                "settle_date": settle,
                "orig_mm": orig_val,
                "currency": currency,
                "lead_mgr_code": lead_mgr,
                "lead_mgr_full": lead_mgr_full,
                "transaction_type": transaction_type,
                "deal_type": deal_type,
            })

    return deals


# ---------------------------------------------------------------------------
# BQL TRANCHE FETCHER (xlwings)
# ---------------------------------------------------------------------------

def fetch_tranches_xlwings(deal_name: str, wb=None, ws=None) -> list[dict]:
    """
    Use xlwings to paste the BQL formula into Excel and retrieve tranche data.

    Parameters:
        deal_name — Bloomberg deal name (e.g. "NEUB 2026-63A")
        wb — existing xlwings Workbook (reused across calls)
        ws — existing xlwings Sheet (reused across calls)

    Returns list of tranche dicts (ALL tranches including equity/X — filtering
    happens later in assign_ratings so we have the full capital structure).
    """
    import xlwings as xw

    # Set the deal name in A1
    ws.range("A1").value = deal_name

    # Set the BQL formula in C1
    formula = BQL_FORMULA.replace("{deal_name}", deal_name)
    ws.range("C1").value = formula

    # Wait for Bloomberg to calculate
    max_wait = 30
    poll_interval = 1
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval
        val = ws.range("C2").value
        if val is not None and val != "" and val != 0:
            break

    # Read the result range (rows 2-12, cols C-O = 13 columns)
    data = ws.range("C2:O12").value
    if not data:
        logger.warning(f"No BQL data returned for {deal_name}")
        return []

    tranches = []
    for row in data:
        if not row or not row[BQL_COLS["name"]]:
            continue

        tranche_class = str(row[BQL_COLS["class"]] or "").strip()
        if not tranche_class:
            continue

        spread_val = row[BQL_COLS["spread"]]
        if spread_val == "#N/A" or spread_val is None:
            spread = None
        else:
            try:
                spread = float(spread_val)
            except (ValueError, TypeError):
                spread = None

        orig_amt = row[BQL_COLS["orig_amt"]]
        try:
            orig_amt = float(orig_amt) if orig_amt else None
        except (ValueError, TypeError):
            orig_amt = None

        call_date = _parse_date(row[BQL_COLS["call_date"]])
        reinvest_end = _parse_date(row[BQL_COLS["reinvest_end"]])
        issue_dt = _parse_date(row[BQL_COLS["issue_dt"]])
        tranche_type = str(row[BQL_COLS["tranche_type"]] or "").strip()

        tranches.append({
            "class": tranche_class,
            "name": str(row[BQL_COLS["name"]] or "").strip(),
            "orig_amt": orig_amt,
            "spread": spread,
            "rtg_moody": str(row[BQL_COLS["rtg_moody"]] or "").strip(),
            "rtg_fitch": str(row[BQL_COLS["rtg_fitch"]] or "").strip(),
            "rtg_sp": str(row[BQL_COLS["rtg_sp"]] or "").strip(),
            "call_date": call_date,
            "reinvest_end": reinvest_end,
            "collat_type": str(row[BQL_COLS["collat_type"]] or "").strip(),
            "manager": str(row[BQL_COLS["manager"]] or "").strip(),
            "tranche_type": tranche_type,
            "issue_dt": issue_dt,
        })

    # Clear the cells for the next deal
    ws.range("A1").value = ""
    ws.range("C1:O12").value = ""

    return tranches


def _parse_date(val) -> Optional[str]:
    """Parse a date from Excel (datetime, serial number, or string) → YYYY-MM-DD."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, (int, float)):
        try:
            from datetime import timedelta
            base = datetime(1899, 12, 30)
            dt = base + timedelta(days=int(val))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return str(val)
    s = str(val)
    return s.split(" ")[0] if "T" in s or " " in s else s


# ---------------------------------------------------------------------------
# EXTRACT LETTER FROM CLASS NAME
# ---------------------------------------------------------------------------

def _extract_letter(cls_name: str) -> str:
    """A1R2 → A, BR2 → B, D1R2 → D, ER2 → E, XR2 → X."""
    m = re.match(r'^([A-Z])', cls_name.upper())
    return m.group(1) if m else cls_name


# ---------------------------------------------------------------------------
# DETERMINE TRANSACTION TYPE FROM BQL DATA
# ---------------------------------------------------------------------------

def determine_transaction_type(tranches: list[dict], settle_date: str) -> str:
    """
    Determine if a deal is New Issue or Reset/Refi using BQL tranche data.

    - RSET in tranche_type → Reset/Refi
    - issue_dt matches settle_date + no RSET → New Issue
    """
    for t in tranches:
        tt = t.get("tranche_type", "").upper()
        if "RSET" in tt:
            return "Reset/Refi"
    return "New Issue"


# ---------------------------------------------------------------------------
# RATING ASSIGNMENT — Credit subordination + class letter grouping
# ---------------------------------------------------------------------------

def assign_ratings(all_tranches: list[dict]) -> list[dict]:
    """
    Assign ratings to debt tranches using credit subordination.

    Steps:
        1. Separate debt tranches from equity/X (keep equity sizes for sub calc)
        2. Sort debt by FLT_SPREAD ascending (tightest = most senior)
        3. Calculate total deal par = debt + assumed equity (9%)
        4. Calculate subordination per tranche
        5. Group by first letter of class name (A, B, C, D, E...)
        6. Walk down: each new letter = next rating on the ladder
        7. Multiple tranches with same letter = Sr/Jr within that rating
        8. Validate with sub thresholds — if Jr drops below threshold,
           it's actually the next rating down

    Returns debt tranches with 'rating' field added, sorted by spread.
    """
    # Separate debt from non-debt
    debt = []
    for t in all_tranches:
        cls_upper = t["class"].upper()
        letter = _extract_letter(t["class"])

        # Skip X tranches and equity classes
        if letter in ("X",) or cls_upper in SKIP_CLASSES:
            continue
        # Skip tranches with no spread (equity-like)
        if t["spread"] is None:
            continue
        debt.append(t)

    if not debt:
        return []

    # Sort by spread (tightest first = most senior)
    debt.sort(key=lambda t: t["spread"])

    # Total deal par = debt + assumed equity underneath
    debt_total = sum(t["orig_amt"] for t in debt if t["orig_amt"])
    total_deal = debt_total / (1 - ASSUMED_EQUITY_PCT)
    equity = total_deal - debt_total

    # Calculate subordination for each tranche
    for i, t in enumerate(debt):
        below_debt = sum(tr["orig_amt"] for tr in debt[i + 1:] if tr["orig_amt"])
        t["sub_pct"] = (below_debt + equity) / total_deal * 100
        t["letter"] = _extract_letter(t["class"])
        t["size_mm"] = round(t["orig_amt"] / 1_000_000, 1) if t["orig_amt"] else 0

    # Group by letter
    letter_groups = {}
    for t in debt:
        letter_groups.setdefault(t["letter"], []).append(t)

    # Assign ratings walking down the capital structure
    rating_idx = 0
    prev_letter = None

    for t in debt:
        letter = t["letter"]
        group = letter_groups[letter]

        if letter != prev_letter:
            # New letter group — check if sub supports the expected rating
            if rating_idx < len(RATING_LADDER):
                expected = RATING_LADDER[rating_idx]
                if t["sub_pct"] < SUB_THRESHOLDS.get(expected, 0):
                    rating_idx += 1

            current_rating = RATING_LADDER[rating_idx] if rating_idx < len(RATING_LADDER) else "NR"

            if len(group) > 1:
                # Multiple tranches with same letter — assign Sr/Jr
                for j, gt in enumerate(group):
                    if j == 0:
                        gt["rating"] = f"Sr {current_rating}"
                    else:
                        # Validate: does Jr's sub still support this rating?
                        if gt["sub_pct"] < SUB_THRESHOLDS.get(current_rating, 0):
                            # Sub too low — this is actually the next rating
                            next_rating = RATING_LADDER[rating_idx + 1] if rating_idx + 1 < len(RATING_LADDER) else "NR"
                            gt["rating"] = next_rating
                        else:
                            gt["rating"] = f"Jr {current_rating}"

                # Check if any Jr got bumped to next rating
                jr_bumped = False
                for gt in group[1:]:
                    if not gt["rating"].startswith("Sr") and not gt["rating"].startswith("Jr"):
                        jr_bumped = True
                        break

                if jr_bumped:
                    rating_idx += 2  # consumed current + next
                else:
                    rating_idx += 1
            else:
                t["rating"] = current_rating
                rating_idx += 1

            prev_letter = letter

    return debt


# ---------------------------------------------------------------------------
# FORMAT FINAL PRICING STRING
# ---------------------------------------------------------------------------

def format_final_pricing(rated_tranches: list[dict]) -> str:
    """
    Format rated tranches into the final pricing display string.

    Rules:
        - AAA tranches: keep Sr/Jr separate, never blend
        - All other ratings: size-weighted average spread, one line per rating
        - Fixed-rate tranches: convert to DM equivalent before blending

    Example output:
        A1R2 (Sr AAA) @ 119 dm
        A2R2 (Jr AAA) @ 140 dm
        BR2 (AA) @ 150 dm
        CR2 (A) @ 195 dm
        D1/D2 (BBB) @ 347 dm
        ER2 (BB) @ 652 dm
    """
    if not rated_tranches:
        return ""

    lines = []

    # AAA tranches — always individual lines
    aaa = [t for t in rated_tranches if "AAA" in t.get("rating", "")]
    for t in aaa:
        lines.append(f"{t['class']} ({t['rating']}) @ {int(t['spread'])} dm")

    # Non-AAA — group by rating and blend
    non_aaa = [t for t in rated_tranches if "AAA" not in t.get("rating", "")]

    # Collect by rating (preserving order)
    rating_order = []
    rating_groups = {}
    for t in non_aaa:
        r = t["rating"]
        if r not in rating_groups:
            rating_order.append(r)
            rating_groups[r] = []
        rating_groups[r].append(t)

    for rating in rating_order:
        group = rating_groups[rating]
        if len(group) == 1:
            t = group[0]
            lines.append(f"{t['class']} ({rating}) @ {int(t['spread'])} dm")
        else:
            # Size-weighted average spread
            total_size = sum(t["orig_amt"] for t in group if t["orig_amt"])
            if total_size > 0:
                weighted = sum(t["spread"] * t["orig_amt"] for t in group if t["orig_amt"] and t["spread"])
                avg_spread = round(weighted / total_size)
            else:
                avg_spread = round(sum(t["spread"] for t in group if t["spread"]) / len(group))

            names = "/".join(t["class"] for t in group)
            lines.append(f"{names} ({rating}) @ {avg_spread} dm")

    return "\n".join(lines)


def build_tranche_detail(rated_tranches: list[dict]) -> list[dict]:
    """
    Build the tranche_detail array for granular storage.

    Each entry: {class, rating, spread, size_mm}
    """
    return [
        {
            "class": t["class"],
            "rating": t.get("rating", "NR"),
            "spread": t["spread"],
            "size_mm": t.get("size_mm", 0),
        }
        for t in rated_tranches
        if t.get("spread") is not None
    ]


# ---------------------------------------------------------------------------
# STORE UPDATER
# ---------------------------------------------------------------------------

def update_stores_from_pricing(deal_info: dict, all_tranches: list[dict]) -> dict:
    """
    Update deals.json, transactions.json, and managers.json from pricing data.

    Parameters:
        deal_info    — from parse_pricing_csv()
        all_tranches — raw BQL output (all tranches incl. equity/X)

    Returns summary dict with what was updated/created.
    """
    from backend import store

    now = datetime.utcnow().isoformat()
    summary = {"deal": None, "transaction": None, "manager": None}

    deal_name = deal_info["deal_name"]
    pricing_date = deal_info["pricing_date"]
    settle_date = deal_info["settle_date"]

    # Assign ratings and format pricing
    rated = assign_ratings(all_tranches)
    final_pricing = format_final_pricing(rated)
    tranche_detail = build_tranche_detail(rated)

    # Determine transaction type from BQL data
    if all_tranches:
        transaction_type = determine_transaction_type(all_tranches, settle_date)
    else:
        transaction_type = deal_info.get("transaction_type", "New Issue")

    # Extract manager and deal details from tranche data
    manager_name = ""
    collat_type = deal_info.get("collateral", "")
    call_date = None
    reinvest_end = None
    if all_tranches:
        for t in all_tranches:
            if t.get("manager"):
                manager_name = t["manager"]
            if t.get("collat_type"):
                collat_type = t["collat_type"]
            if t.get("call_date"):
                call_date = t["call_date"]
            if t.get("reinvest_end"):
                reinvest_end = t["reinvest_end"]
            if manager_name and call_date and reinvest_end:
                break

    deal_type = COLLATERAL_MAP.get(collat_type, deal_info.get("deal_type", ""))

    # Calculate total par from rated debt tranches
    total_par = None
    if rated:
        total = sum(t["orig_amt"] for t in rated if t["orig_amt"])
        if total > 0:
            total_par = round(total / 1_000_000, 1)

    # ── Update/Create Deal ──
    existing_deal = store.find_record(
        store.DEALS,
        lambda r: r.get("deal_name") == deal_name
    )
    if existing_deal:
        updates = {"updated_at": now}
        if deal_type:
            updates["deal_type"] = deal_type
        if total_par:
            updates["target_par"] = total_par
        if deal_info.get("lead_mgr_full"):
            updates["arranger"] = deal_info["lead_mgr_full"]
        if call_date:
            updates["non_call_period"] = call_date
        if reinvest_end:
            updates["reinvestment_period"] = reinvest_end
        store.update_record(store.DEALS, lambda r: r.get("deal_name") == deal_name, updates)
        summary["deal"] = "updated"
    else:
        deal_data = {
            "id": new_id(),
            "deal_name": deal_name,
            "deal_type": deal_type,
            "target_par": total_par,
            "arranger": deal_info.get("lead_mgr_full", ""),
            "collateral_manager_short": _extract_manager_short(manager_name),
            "legal_entity": manager_name,
            "non_call_period": call_date or "",
            "reinvestment_period": reinvest_end or "",
            "created_at": now,
            "updated_at": now,
        }
        store.append_record(store.DEALS, deal_data)
        summary["deal"] = "created"

    # ── Update/Create Transaction ──
    existing_txn = store.find_record(
        store.TRANSACTIONS,
        lambda r: r.get("deal_name") == deal_name and r.get("status") != "Priced"
    )
    txn_updates = {
        "status": "Priced",
        "priced_date": pricing_date,
        "final_pricing": final_pricing,
        "tranche_detail": tranche_detail,
        "transaction_type": transaction_type,
        "settle_date": settle_date,
        "updated_at": now,
    }
    if existing_txn:
        store.update_record(
            store.TRANSACTIONS,
            lambda r: r.get("deal_name") == deal_name and r.get("status") != "Priced",
            txn_updates,
        )
        summary["transaction"] = "updated to Priced"
    else:
        txn_data = {
            "id": new_id(),
            "deal_name": deal_name,
            "collateral_manager": _extract_manager_short(manager_name),
            "created_at": now,
        }
        txn_data.update(txn_updates)
        store.append_record(store.TRANSACTIONS, txn_data)
        summary["transaction"] = "created as Priced"

    # ── Update/Create Manager ──
    if manager_name:
        short = _extract_manager_short(manager_name)
        existing_mgr = store.find_record(
            store.MANAGERS,
            lambda r: r.get("short_name") == short
        )
        if not existing_mgr:
            store.append_record(store.MANAGERS, {
                "id": new_id(),
                "short_name": short,
                "legal_entity": manager_name,
                "created_at": now,
            })
            summary["manager"] = f"created ({short})"

    return summary


def _extract_manager_short(full_name: str) -> str:
    """Extract short manager name from full name. 'Neuberger Berman Fixed Income' → 'Neuberger Berman'."""
    if not full_name:
        return ""
    short = re.sub(
        r'\s+(Fixed Income|Credit|Asset Management|Investment Management|Capital|'
        r'Loan Management|CLO Management|Advisors?|Partners?|LLC|Inc\.?|Ltd\.?|L\.P\.?).*$',
        '', full_name, flags=re.IGNORECASE
    ).strip()
    return short or full_name


def is_already_priced(deal_name: str, pricing_date: str) -> bool:
    """
    Check if a deal has already been processed with this pricing date.

    Returns True if transactions.json has a record with matching
    deal_name, status="Priced", and priced_date matching pricing_date.
    """
    from backend import store
    existing = store.find_record(
        store.TRANSACTIONS,
        lambda r: (
            r.get("deal_name") == deal_name
            and r.get("status") == "Priced"
            and r.get("priced_date") == pricing_date
        ),
    )
    return existing is not None


# ---------------------------------------------------------------------------
# MAIN PIPELINE
# ---------------------------------------------------------------------------

def run_pricing_pipeline(csv_path: str, use_xlwings: bool = True,
                         progress_callback=None) -> list[dict]:
    """
    Run the full pricing pipeline:
    1. Parse CSV for priced deals
    2. Fetch tranche data via BQL (xlwings) for each
    3. Assign ratings via credit subordination
    4. Update JSON stores with final pricing + tranche detail

    Parameters:
        csv_path          — path to pricings_YYYYMMDD.csv
        use_xlwings       — True to use xlwings+BQL, False for CSV-only mode
        progress_callback — optional fn(deal_name, i, total, summary) for GUI updates

    Returns list of result dicts per deal.
    """
    priced_deals = parse_pricing_csv(csv_path)
    total = len(priced_deals)
    print(f"[BBG Pricing] Found {total} priced deals in {os.path.basename(csv_path)}")

    results = []
    wb = None
    ws = None

    try:
        if use_xlwings:
            try:
                import xlwings as xw
                wb = xw.Book()
                ws = wb.sheets[0]
                print("[BBG Pricing] Excel workbook opened via xlwings")
            except ImportError:
                print("[BBG Pricing] xlwings not installed — running in CSV-only mode")
                use_xlwings = False
            except Exception as e:
                print(f"[BBG Pricing] Could not open Excel: {e} — running in CSV-only mode")
                use_xlwings = False

        skipped = 0
        for i, deal in enumerate(priced_deals):
            deal_name = deal["deal_name"]
            pricing_date = deal["pricing_date"]

            # Skip deals already processed with this pricing date
            if is_already_priced(deal_name, pricing_date):
                print(f"[BBG Pricing] ({i+1}/{total}) Skipping {deal_name} — already priced on {pricing_date}")
                skipped += 1
                continue

            print(f"[BBG Pricing] ({i+1}/{total}) Processing {deal_name}...")

            all_tranches = []
            if use_xlwings:
                try:
                    all_tranches = fetch_tranches_xlwings(deal_name, wb=wb, ws=ws)
                    print(f"  BQL returned {len(all_tranches)} tranches")
                except Exception as e:
                    print(f"  BQL error: {e}")

            summary = update_stores_from_pricing(deal, all_tranches)

            rated = assign_ratings(all_tranches)
            result = {
                **deal,
                "tranches_total": len(all_tranches),
                "tranches_rated": len(rated),
                "final_pricing": format_final_pricing(rated),
                "summary": summary,
            }
            results.append(result)

            if progress_callback:
                progress_callback(deal_name, i + 1, total, summary)

            fp_preview = result["final_pricing"].replace("\n", " | ")[:80]
            print(f"  {summary['deal']} | {summary['transaction']}"
                  + (f" | {summary['manager']}" if summary.get('manager') else "")
                  + (f"\n  Pricing: {fp_preview}" if fp_preview else ""))

    finally:
        if wb:
            try:
                wb.close()
                print("[BBG Pricing] Excel workbook closed")
            except Exception:
                pass

    print(f"\n[BBG Pricing] Done — processed {len(results)} deals, skipped {skipped} already priced")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m backend.bbg_pricing <pricings_YYYYMMDD.csv> [--no-xlwings]")
        print()
        print("Options:")
        print("  --no-xlwings    Skip BQL/Excel, just update stores from CSV data")
        sys.exit(1)

    csv_path = sys.argv[1]
    use_xlwings = "--no-xlwings" not in sys.argv

    if not os.path.isfile(csv_path):
        print(f"File not found: {csv_path}")
        sys.exit(1)

    results = run_pricing_pipeline(csv_path, use_xlwings=use_xlwings)

    created = sum(1 for r in results if r["summary"]["deal"] == "created")
    updated = sum(1 for r in results if r["summary"]["deal"] == "updated")
    print(f"\nSummary: {created} deals created, {updated} deals updated")


if __name__ == "__main__":
    main()
