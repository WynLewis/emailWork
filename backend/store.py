"""
JSON file-based persistence layer with file locking.
=====================================================

WHAT THIS FILE DOES:
    Provides CRUD (Create, Read, Update, Delete) operations for the
    application's data, stored as JSON files on disk.

    Each "store" is a single JSON file containing an array of objects.
    For example, `data/deals.json` might look like:
        [
          {"id": "abc-123", "deal_name": "Dryden 120", ...},
          {"id": "def-456", "deal_name": "Ares LXVII", ...}
        ]

WHY JSON FILES (not a database)?
    - Zero setup — no database server to install or configure.
    - Human-readable — you can open the files in any text editor.
    - Portable — just copy the `data/` folder to move your data.
    - Good enough for the volume we handle (dozens to hundreds of records).

FILE LOCKING:
    We use `filelock` to prevent corruption if two processes try to write
    at the same time (e.g., the notebook and the web server). The lock
    files (*.lock) are temporary and can be safely deleted.

HOW TO ADAPT:
    If you want to switch to a real database (PostgreSQL, SQLite, etc.):
    1. Replace the functions below with equivalent DB queries.
    2. Keep the same function signatures so the rest of the code still works.
    3. The callers (app.py, notebook) only use: read_store, append_record,
       update_record, find_record, clear_store, append_training_example.

STORES (JSON files in data/):
    deals.json          — CLO deals
    managers.json       — Collateral managers
    transactions.json   — Transaction lifecycle records
    deal_orders.json    — Your firm's orders/allocations
"""

import json
import os
from typing import Any

from filelock import FileLock

from backend import config


def _path(name: str) -> str:
    """Convert a store name (e.g. 'deals.json') to its full file path."""
    return os.path.join(config.DATA_DIR, name)


def _lock_path(name: str) -> str:
    """Filelock path for a store. The lock prevents concurrent write corruption."""
    return _path(name) + ".lock"


def _ensure(name: str) -> None:
    """Create the store file (as an empty JSON array) if it doesn't exist yet."""
    p = _path(name)
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump([], f)


# ---------------------------------------------------------------------------
# CORE OPERATIONS — These are the building blocks used by everything else.
# ---------------------------------------------------------------------------

def read_store(name: str) -> list[dict[str, Any]]:
    """Read all records from a JSON store. Returns a list of dicts."""
    _ensure(name)
    with FileLock(_lock_path(name)):
        with open(_path(name), "r") as f:
            return json.load(f)


def write_store(name: str, data: list[dict[str, Any]]) -> None:
    """Overwrite an entire JSON store with new data. Use with care."""
    _ensure(name)
    with FileLock(_lock_path(name)):
        with open(_path(name), "w") as f:
            json.dump(data, f, indent=2, default=str)


def append_record(name: str, record: dict[str, Any]) -> None:
    """Add a single record to the end of a store."""
    data = read_store(name)
    data.append(record)
    write_store(name, data)


def update_record(name: str, match_fn, updates: dict[str, Any]) -> bool:
    """
    Update the FIRST record where match_fn(record) returns True.

    Parameters:
        name     — Store filename (e.g. "transactions.json")
        match_fn — A function that takes a record dict and returns True/False.
                   Example: lambda r: r.get("deal_name") == "Dryden 120"
        updates  — Dict of fields to update on the matched record.

    Returns True if a record was found and updated, False otherwise.
    """
    data = read_store(name)
    for i, rec in enumerate(data):
        if match_fn(rec):
            data[i].update(updates)
            write_store(name, data)
            return True
    return False


def find_record(name: str, match_fn) -> dict[str, Any] | None:
    """Find and return the first record matching match_fn, or None if not found."""
    data = read_store(name)
    for rec in data:
        if match_fn(rec):
            return rec
    return None


def clear_store(name: str) -> None:
    """Delete all records from a store (resets it to an empty array)."""
    write_store(name, [])


# ---------------------------------------------------------------------------
# STORE NAME CONSTANTS — Use these instead of raw strings for safety.
# ---------------------------------------------------------------------------
DEALS = "deals.json"
MANAGERS = "managers.json"
TRANSACTIONS = "transactions.json"
DEAL_ORDERS = "deal_orders.json"

ALL_STORES = [DEALS, MANAGERS, TRANSACTIONS, DEAL_ORDERS]


# ---------------------------------------------------------------------------
# TRAINING DATA — Separate from the main stores (JSONL format, not JSON).
# ---------------------------------------------------------------------------

def append_training_example(email_text: str, corrected: dict) -> int:
    """
    Save a training example for future fine-tuning.

    Each time the user accepts an extraction (with or without corrections),
    the email text + corrected extraction is appended to a JSONL file
    (one JSON object per line). These examples are used by finetune.py
    to train a LoRA adapter that improves the model over time.

    Parameters:
        email_text — The raw email content (HTML/text).
        corrected  — The corrected extraction dict (what the user approved).

    Returns:
        The total number of training examples saved so far.
    """
    import datetime
    path = config.TRAINING_DATA
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "email_text": email_text,
        "extraction": corrected,
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }
    with FileLock(path + ".lock"):
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    # Return count
    with open(path, "r") as f:
        return sum(1 for _ in f)
