"""
SQLite-based persistence layer for CLO Email Extraction.
=========================================================

Replaces the JSON file-based store with SQLite for:
- Atomic writes (no filelock needed)
- Instant queries (no read-all/loop/write-all)
- Unique constraints (no duplicate detection lambdas)
- Single file: data/clo.db

Tables:
    deals          — CLO deal records (Title, Collateral Type, Manager, etc.)
    transactions   — Transaction lifecycle (IPT → Priced, with pricing details)
    managers       — Collateral manager reference data
    deal_orders    — Firm's orders/allocations per deal
    training_data  — Email + extraction pairs for LLM fine-tuning
    extraction_log — Audit log of every extraction run

Migration:
    python -m backend.store_db migrate    # import existing clo-*.json
    python -m backend.store_db export     # export SQLite → CSV
"""

import json
import os
import sqlite3
import csv
from datetime import datetime
from typing import Any, Optional

from backend import config

DB_PATH = os.path.join(config.DATA_DIR, "clo.db")


def get_db(path: str = None) -> sqlite3.Connection:
    """Get a SQLite connection with row_factory for dict-like access."""
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent read performance
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection = None) -> None:
    """Create all tables if they don't exist."""
    close = False
    if conn is None:
        conn = get_db()
        close = True

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE NOT NULL,
            collateral_type TEXT DEFAULT 'BSL',
            collateral_manager TEXT DEFAULT '',
            bloomberg_deal_name TEXT DEFAULT '',
            intex_deal TEXT DEFAULT '',
            intex_preprice TEXT DEFAULT '',
            deal_documents TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS managers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            short_name TEXT DEFAULT '',
            ultimate_parent TEXT DEFAULT '',
            crd_number TEXT DEFAULT '',
            sec_number TEXT DEFAULT '',
            website TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT DEFAULT '',
            deal TEXT NOT NULL,
            collateral_type TEXT DEFAULT '',
            collateral_manager TEXT DEFAULT '',
            transaction_type TEXT DEFAULT 'New Issue',
            status TEXT DEFAULT 'Announced',
            placement_agent TEXT DEFAULT '',
            term TEXT DEFAULT '',
            ipt TEXT DEFAULT '',
            final_pricing_details TEXT DEFAULT '',
            announcement_date TEXT DEFAULT '',
            priced_date TEXT DEFAULT '',
            engaged INTEGER DEFAULT 0,
            executed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS deal_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_name TEXT NOT NULL,
            collateral_manager TEXT DEFAULT '',
            transaction_type TEXT DEFAULT '',
            engaged_date TEXT DEFAULT '',
            executed INTEGER DEFAULT 0,
            executed_date TEXT DEFAULT '',
            tranche TEXT DEFAULT '',
            allocation_mm REAL,
            spread_bps REAL,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS training_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_html TEXT NOT NULL,
            extraction_json TEXT NOT NULL,
            source TEXT DEFAULT 'email',
            filename TEXT DEFAULT '',
            accepted_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS extraction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            filename TEXT DEFAULT '',
            deal_name TEXT DEFAULT '',
            extraction_json TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            reviewed_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_deals_title ON deals(title);
        CREATE INDEX IF NOT EXISTS idx_txn_deal ON transactions(deal);
        CREATE INDEX IF NOT EXISTS idx_txn_status ON transactions(status);
        CREATE INDEX IF NOT EXISTS idx_mgr_short ON managers(short_name);
        CREATE INDEX IF NOT EXISTS idx_log_status ON extraction_log(status);
    """)

    if close:
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def upsert_deal(conn: sqlite3.Connection, data: dict) -> int:
    """Insert or update a deal by title. Returns rowid."""
    title = data.get("Title") or data.get("title") or ""
    if not title.strip():
        raise ValueError("Deal must have a Title")

    conn.execute("""
        INSERT INTO deals (title, collateral_type, collateral_manager,
                          bloomberg_deal_name, intex_deal, intex_preprice,
                          deal_documents, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(title) DO UPDATE SET
            collateral_type = COALESCE(NULLIF(excluded.collateral_type, ''), deals.collateral_type),
            collateral_manager = COALESCE(NULLIF(excluded.collateral_manager, ''), deals.collateral_manager),
            bloomberg_deal_name = COALESCE(NULLIF(excluded.bloomberg_deal_name, ''), deals.bloomberg_deal_name),
            intex_deal = COALESCE(NULLIF(excluded.intex_deal, ''), deals.intex_deal),
            intex_preprice = COALESCE(NULLIF(excluded.intex_preprice, ''), deals.intex_preprice),
            deal_documents = COALESCE(NULLIF(excluded.deal_documents, ''), deals.deal_documents),
            updated_at = datetime('now')
    """, (
        title.strip(),
        data.get("Collateral Type") or data.get("collateral_type") or "",
        data.get("Collateral Manager") or data.get("collateral_manager") or "",
        data.get("Bloomberg Deal Name") or data.get("bloomberg_deal_name") or "",
        data.get("Intex Deal") or data.get("intex_deal") or "",
        data.get("Intex Preprice") or data.get("intex_preprice") or "",
        data.get("Deal Documents") or data.get("deal_documents") or "",
    ))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def upsert_manager(conn: sqlite3.Connection, data: dict) -> int:
    """Insert or update a manager by name. Returns rowid."""
    name = data.get("Name") or data.get("name") or ""
    if not name.strip():
        raise ValueError("Manager must have a Name")

    conn.execute("""
        INSERT INTO managers (name, short_name, ultimate_parent,
                             crd_number, sec_number, website)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            short_name = COALESCE(NULLIF(excluded.short_name, ''), managers.short_name),
            ultimate_parent = COALESCE(NULLIF(excluded.ultimate_parent, ''), managers.ultimate_parent),
            crd_number = COALESCE(NULLIF(excluded.crd_number, ''), managers.crd_number),
            sec_number = COALESCE(NULLIF(excluded.sec_number, ''), managers.sec_number),
            website = COALESCE(NULLIF(excluded.website, ''), managers.website)
    """, (
        name.strip(),
        data.get("Short Name") or data.get("short_name") or "",
        data.get("Ultimate Parent") or data.get("ultimate_parent") or "",
        data.get("CRD Number") or data.get("crd_number") or "",
        data.get("SEC Number") or data.get("sec_number") or "",
        data.get("Website") or data.get("website") or "",
    ))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def insert_transaction(conn: sqlite3.Connection, data: dict) -> int:
    """Insert a new transaction. Returns rowid."""
    deal = data.get("Deal") or data.get("deal") or data.get("Title") or data.get("title") or ""
    conn.execute("""
        INSERT INTO transactions (title, deal, collateral_type, collateral_manager,
                                  transaction_type, status, placement_agent, term,
                                  ipt, final_pricing_details, announcement_date,
                                  priced_date, engaged, executed, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        data.get("Title") or data.get("title") or deal,
        deal,
        data.get("Collateral Type") or data.get("collateral_type") or "",
        data.get("Collateral Manager") or data.get("collateral_manager") or "",
        data.get("Transaction Type") or data.get("transaction_type") or "New Issue",
        data.get("Status") or data.get("status") or "Announced",
        data.get("Placement Agent") or data.get("placement_agent") or "",
        data.get("Term") or data.get("term") or "",
        data.get("IPT") or data.get("ipt") or "",
        data.get("Final Pricing Details") or data.get("final_pricing_details") or data.get("final_pricing") or "",
        data.get("Announcement Date") or data.get("announcement_date") or "",
        data.get("Priced Date") or data.get("priced_date") or "",
        1 if str(data.get("Engaged", "")).lower() in ("true", "1", "yes") else 0,
        1 if str(data.get("Executed", "")).lower() in ("true", "1", "yes") else 0,
    ))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_transaction_to_priced(conn: sqlite3.Connection, deal: str, data: dict) -> bool:
    """Update an announced transaction to priced status. Returns True if updated."""
    result = conn.execute("""
        UPDATE transactions SET
            status = 'Priced',
            priced_date = ?,
            final_pricing_details = COALESCE(NULLIF(?, ''), final_pricing_details),
            placement_agent = COALESCE(NULLIF(?, ''), placement_agent),
            transaction_type = COALESCE(NULLIF(?, ''), transaction_type),
            term = COALESCE(NULLIF(?, ''), term),
            updated_at = datetime('now')
        WHERE deal = ? AND status != 'Priced'
    """, (
        data.get("Priced Date") or data.get("priced_date") or datetime.utcnow().strftime("%Y-%m-%d"),
        data.get("Final Pricing Details") or data.get("final_pricing_details") or data.get("final_pricing") or "",
        data.get("Placement Agent") or data.get("placement_agent") or "",
        data.get("Transaction Type") or data.get("transaction_type") or "",
        data.get("Term") or data.get("term") or "",
        deal.strip(),
    ))
    return result.rowcount > 0


def save_training_pair(conn: sqlite3.Connection, email_html: str, extraction: dict,
                       source: str = "email", filename: str = "") -> int:
    """Save an email + extraction pair for LLM training. Returns rowid."""
    conn.execute("""
        INSERT INTO training_data (email_html, extraction_json, source, filename)
        VALUES (?, ?, ?, ?)
    """, (email_html, json.dumps(extraction), source, filename))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def log_extraction(conn: sqlite3.Connection, source: str, filename: str,
                   deal_name: str, extraction: dict, status: str = "pending") -> int:
    """Log an extraction for batch review. Returns rowid."""
    conn.execute("""
        INSERT INTO extraction_log (source, filename, deal_name, extraction_json, status)
        VALUES (?, ?, ?, ?, ?)
    """, (source, filename, deal_name, json.dumps(extraction), status))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def accept_extraction(conn: sqlite3.Connection, log_id: int) -> None:
    """Mark an extraction log entry as accepted."""
    conn.execute("""
        UPDATE extraction_log SET status = 'accepted', reviewed_at = datetime('now')
        WHERE id = ?
    """, (log_id,))


def skip_extraction(conn: sqlite3.Connection, log_id: int) -> None:
    """Mark an extraction log entry as skipped."""
    conn.execute("""
        UPDATE extraction_log SET status = 'skipped', reviewed_at = datetime('now')
        WHERE id = ?
    """, (log_id,))


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def query_df(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a query and return list of dicts."""
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_all_deals(conn: sqlite3.Connection) -> list[dict]:
    return query_df(conn, "SELECT * FROM deals ORDER BY title")


def get_all_transactions(conn: sqlite3.Connection) -> list[dict]:
    return query_df(conn, "SELECT * FROM transactions ORDER BY id DESC")


def get_all_managers(conn: sqlite3.Connection) -> list[dict]:
    return query_df(conn, "SELECT * FROM managers ORDER BY short_name")


def get_pending_extractions(conn: sqlite3.Connection) -> list[dict]:
    return query_df(conn, "SELECT * FROM extraction_log WHERE status = 'pending' ORDER BY id")


def get_training_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM training_data").fetchone()[0]


# ---------------------------------------------------------------------------
# CSV Export (for SharePoint import)
# ---------------------------------------------------------------------------

def export_deals_csv(conn: sqlite3.Connection, path: str) -> int:
    """Export deals table to CSV. Returns row count."""
    rows = query_df(conn, "SELECT title as Title, collateral_type as 'Collateral Type', "
                         "collateral_manager as 'Collateral Manager', "
                         "bloomberg_deal_name as 'Bloomberg Deal Name', "
                         "intex_deal as 'Intex Deal', intex_preprice as 'Intex Preprice', "
                         "deal_documents as 'Deal Documents' FROM deals ORDER BY title")
    return _write_csv(rows, path)


def export_transactions_csv(conn: sqlite3.Connection, path: str) -> int:
    """Export transactions table to CSV. Returns row count."""
    rows = query_df(conn, """
        SELECT
            CASE WHEN engaged THEN 'True' ELSE 'False' END as Engaged,
            CASE WHEN executed THEN 'True' ELSE 'False' END as Executed,
            deal as Deal, ipt as IPT,
            final_pricing_details as 'Final Pricing Details',
            placement_agent as 'Placement Agent', term as Term,
            transaction_type as 'Transaction Type', status as Status,
            announcement_date as 'Announcement Date',
            priced_date as 'Priced Date', title as Title,
            collateral_type as 'Collateral Type',
            collateral_manager as 'Collateral Manager'
        FROM transactions ORDER BY id
    """)
    return _write_csv(rows, path)


def export_managers_csv(conn: sqlite3.Connection, path: str) -> int:
    """Export managers table to CSV. Returns row count."""
    rows = query_df(conn, """
        SELECT name as Name, short_name as 'Short Name',
               ultimate_parent as 'Ultimate Parent',
               crd_number as 'CRD Number', sec_number as 'SEC Number',
               website as Website
        FROM managers ORDER BY short_name
    """)
    return _write_csv(rows, path)


def _write_csv(rows: list[dict], path: str) -> int:
    if not rows:
        return 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys(), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Migration from JSON
# ---------------------------------------------------------------------------

def migrate_from_json(conn: sqlite3.Connection = None) -> dict:
    """
    Import existing clo-*.json data into SQLite tables.
    Returns dict with counts per table.
    """
    close = False
    if conn is None:
        conn = get_db()
        close = True

    init_db(conn)
    counts = {"deals": 0, "managers": 0, "transactions": 0}

    # Deals
    deals_path = os.path.join(config.DATA_DIR, "clo-deals.json")
    if os.path.isfile(deals_path):
        with open(deals_path, "r", encoding="utf-8-sig") as f:
            deals = json.load(f)
        for d in deals:
            try:
                upsert_deal(conn, d)
                counts["deals"] += 1
            except Exception:
                pass

    # Managers
    mgrs_path = os.path.join(config.DATA_DIR, "clo-managers.json")
    if os.path.isfile(mgrs_path):
        with open(mgrs_path, "r", encoding="utf-8-sig") as f:
            mgrs = json.load(f)
        for m in mgrs:
            try:
                upsert_manager(conn, m)
                counts["managers"] += 1
            except Exception:
                pass

    # Transactions
    txns_path = os.path.join(config.DATA_DIR, "clo-transactions.json")
    if os.path.isfile(txns_path):
        with open(txns_path, "r", encoding="utf-8-sig") as f:
            txns = json.load(f)
        for t in txns:
            try:
                insert_transaction(conn, t)
                counts["transactions"] += 1
            except Exception:
                pass

    conn.commit()
    if close:
        conn.close()

    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m backend.store_db migrate   # import clo-*.json → SQLite")
        print("  python -m backend.store_db export     # export SQLite → CSV")
        print("  python -m backend.store_db stats      # show table counts")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "migrate":
        print("Migrating JSON data to SQLite...")
        counts = migrate_from_json()
        print(f"  Deals:        {counts['deals']}")
        print(f"  Managers:     {counts['managers']}")
        print(f"  Transactions: {counts['transactions']}")
        print(f"\nDatabase: {DB_PATH}")

    elif cmd == "export":
        conn = get_db()
        export_dir = os.path.join(config.DATA_DIR, "export")
        os.makedirs(export_dir, exist_ok=True)
        n = export_deals_csv(conn, os.path.join(export_dir, "deals.csv"))
        print(f"  Deals: {n} rows → data/export/deals.csv")
        n = export_transactions_csv(conn, os.path.join(export_dir, "transactions.csv"))
        print(f"  Transactions: {n} rows → data/export/transactions.csv")
        n = export_managers_csv(conn, os.path.join(export_dir, "managers.csv"))
        print(f"  Managers: {n} rows → data/export/managers.csv")
        conn.close()

    elif cmd == "stats":
        conn = get_db()
        init_db(conn)
        for table in ["deals", "transactions", "managers", "deal_orders", "training_data", "extraction_log"]:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:20s} {n:>6d} rows")
        conn.close()
