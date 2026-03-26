"""
FastAPI web server for the CLO Email Extraction application.
=============================================================

WHAT THIS FILE DOES:
    Provides an HTTP API (and simple web UI) as an ALTERNATIVE to the
    Jupyter notebook interface. You can run this instead of the notebook:

        python -m backend          # starts the server on localhost:8000
        # then open http://localhost:8000 in your browser

    The notebook (extractor_gui.ipynb) and this server both use the same
    backend modules (extractor, store, watcher), so they are interchangeable.

HOW IT WORKS:
    FastAPI is a modern Python web framework that auto-generates API docs.
    After starting the server, visit http://localhost:8000/docs for an
    interactive interface where you can test every endpoint.

    The server:
    1. Scans the email inbox for new emails
    2. Runs the LLM extractor on each email
    3. Presents the extraction to you via the web UI
    4. You review, edit, and accept or skip
    5. Accepted data is saved to the JSON stores (data/*.json)

API ENDPOINTS (summary):
    GET  /api/queue        — List emails waiting to be processed
    GET  /api/next         — Get the next email + its LLM extraction
    POST /api/accept       — Accept (possibly edited) extraction, saves data
    POST /api/skip         — Skip the current email
    GET  /api/deals        — View all saved deals
    GET  /api/managers     — View all saved managers
    POST /api/managers     — Create a new manager
    GET  /api/transactions — View all saved transactions
    GET  /api/deal-orders  — View all saved deal orders
    GET  /api/export       — Export ALL data as one JSON object
    POST /api/reset        — Clear all data stores (careful!)
    GET  /                 — Serve the frontend web page

HOW TO ADAPT:
    - To add a new endpoint: define a function with @app.get or @app.post decorator
    - To change the data model: update AcceptRequest and the handler logic
    - To add authentication: add FastAPI middleware (see FastAPI docs for OAuth2)
    - To use a different database: swap store.* calls for your DB queries
    - To change the port: set HOST / PORT env vars (see config.py)
"""

import logging
import os
from datetime import datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend import config
from backend.extractor import get_extractor, detect_email_type, format_tranche_pricing, parse_deal_fields
from backend.models import ExtractionResult, new_id, now_iso
from backend.watcher import EmailQueue
from backend import store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create the FastAPI application instance
app = FastAPI(title="CLO Email Extraction")

# Initialize the email file queue (scans data/inbox/ for .txt files)
queue = EmailQueue()

# Try to load the LLM extractor at startup.
# If it fails (e.g., no API key, model not downloaded), we fall back
# to basic email-type detection only.
try:
    extractor = get_extractor()
    logger.info(f"Loaded extractor: {type(extractor).__name__}")
except Exception as e:
    logger.warning(f"Extractor init failed: {e}")
    extractor = None


# ─── Request/Response Models ─────────────────────────────────────────
# These define the shape of data the API expects to receive.

class AcceptRequest(BaseModel):
    """
    Data sent by the frontend when the user accepts an extraction.

    Fields:
        filename   — Which email file to mark as processed
        email_type — "announced", "updated", or "priced" (determines what gets saved)
        deal       — Deal info (only for announced emails)
        transaction — Transaction info (for all email types)
        deal_order  — Deal order info (if you're engaged in the deal)
        new_manager — New CLO manager info (if the manager wasn't in our database)
    """
    filename: str
    email_type: str  # announced, updated, priced
    deal: dict[str, Any] | None = None
    transaction: dict[str, Any] | None = None
    deal_order: dict[str, Any] | None = None
    new_manager: dict[str, Any] | None = None


class ManagerCreate(BaseModel):
    """Data for creating a new CLO manager record."""
    short_name: str
    legal_entity: str = ""
    aum_bn: float | None = None
    hq: str = ""
    notes: str = ""


# ─── Queue / Extraction Endpoints ────────────────────────────────────

@app.get("/api/queue")
def get_queue():
    """Return the list of unprocessed email files and total count."""
    queue.scan()  # re-scan for new files that may have been added
    return {"files": queue.remaining(), "count": queue.size()}


@app.get("/api/next")
def get_next():
    """
    Get the next email to review, along with its LLM extraction.

    Returns:
        filename            — The email file name
        html_content        — Raw email text/HTML
        extraction          — LLM-extracted fields (dict)
        existing_transaction — If this is an update/priced email, the existing transaction
        existing_deal_order  — If this is an update/priced email, the existing deal order
    """
    queue.scan()
    filename = queue.peek()
    if not filename:
        return {"filename": None, "html_content": None, "extraction": None}

    html_content = queue.read_file(filename)

    # Run LLM extraction (or fall back to type detection only)
    extraction = None
    try:
        if extractor:
            result = extractor.extract(html_content)
            extraction = result.model_dump()
        else:
            extraction = ExtractionResult(email_type=detect_email_type(html_content)).model_dump()
    except Exception as e:
        logger.error(f"Extraction failed for {filename}: {e}")
        extraction = ExtractionResult(email_type=detect_email_type(html_content)).model_dump()

    # Parse deal fields + tranche pricing directly from HTML tables
    # (more reliable than the LLM for structured emails)
    if extraction and html_content:
        deal_fields = parse_deal_fields(html_content)
        for key, val in deal_fields.items():
            if val and not extraction.get(key):
                extraction[key] = val
        pricing = format_tranche_pricing(html_content)
        for key in ("ipt", "updated_guidance", "final_pricing"):
            if pricing.get(key):
                extraction[key] = pricing[key]

    # For "updated" or "priced" emails, look up existing records so the
    # user can see what was previously saved and compare
    existing_transaction = None
    existing_deal_order = None
    if extraction and extraction.get("email_type") in ("updated", "priced"):
        deal_name = extraction.get("deal_name")
        if deal_name:
            existing_transaction = store.find_record(
                store.TRANSACTIONS,
                lambda r: r.get("deal_name") == deal_name and r.get("status") != "Priced"
            )
            if existing_transaction and existing_transaction.get("engaged"):
                existing_deal_order = store.find_record(
                    store.DEAL_ORDERS,
                    lambda r: r.get("deal_name") == deal_name
                )

    return {
        "filename": filename,
        "html_content": html_content,
        "extraction": extraction,
        "existing_transaction": existing_transaction,
        "existing_deal_order": existing_deal_order,
    }


# ─── Accept / Skip Endpoints ─────────────────────────────────────────

@app.post("/api/accept")
def accept(req: AcceptRequest):
    """
    Accept the user's (possibly edited) extraction and save to data stores.

    Behavior depends on email_type:
    - "announced": Creates deal + transaction + optional manager + optional deal order
    - "updated":   Updates the existing transaction record with new spreads/terms
    - "priced":    Marks the transaction as priced; marks deal order as executed if applicable
    """
    now = datetime.utcnow().isoformat()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    # Save training example for fine-tuning
    html_content = queue.read_file(req.filename) if req.filename else None
    if html_content:
        training_data = {}
        if req.deal:
            training_data.update(req.deal)
        if req.transaction:
            training_data.update(req.transaction)
        training_data["email_type"] = req.email_type
        store.append_training_example(html_content, training_data)

    # ── Handle "announced" emails ──
    if req.email_type == "announced":
        # Create or update the deal record
        if req.deal:
            deal_data = req.deal.copy()
            deal_data.setdefault("created_at", now)
            deal_data["updated_at"] = now
            # Check if deal already exists
            existing = store.find_record(
                store.DEALS, lambda r: r.get("deal_name") == deal_data.get("deal_name")
            )
            if existing:
                store.update_record(
                    store.DEALS,
                    lambda r: r.get("deal_name") == deal_data.get("deal_name"),
                    deal_data,
                )
            else:
                deal_data.setdefault("id", new_id())
                store.append_record(store.DEALS, deal_data)

        # If extraction included a new CLO manager we haven't seen before
        if req.new_manager:
            mgr = req.new_manager.copy()
            mgr.setdefault("id", new_id())
            mgr.setdefault("created_at", now)
            store.append_record(store.MANAGERS, mgr)

        # Create a new transaction record (status = "Announced")
        if req.transaction:
            txn = req.transaction.copy()
            txn.setdefault("id", new_id())
            txn.setdefault("status", "Announced")
            txn.setdefault("created_at", now)
            txn["updated_at"] = now
            store.append_record(store.TRANSACTIONS, txn)

        # Create a deal order if we're engaged in this deal
        if req.deal_order:
            order = req.deal_order.copy()
            order.setdefault("id", new_id())
            order.setdefault("created_at", now)
            order["updated_at"] = now
            store.append_record(store.DEAL_ORDERS, order)

    # ── Handle "updated" emails ──
    elif req.email_type == "updated":
        # Find the existing transaction and update it with new spreads/terms
        if req.transaction:
            txn_update = req.transaction.copy()
            txn_update["status"] = "Updated"
            txn_update["updated_at"] = now
            deal_name = txn_update.get("deal_name")
            if deal_name:
                updated = store.update_record(
                    store.TRANSACTIONS,
                    lambda r: r.get("deal_name") == deal_name and r.get("status") != "Priced",
                    txn_update,
                )
                if not updated:
                    logger.warning(f"No matching transaction found to update for deal '{deal_name}'")

    # ── Handle "priced" emails ──
    elif req.email_type == "priced":
        # Mark the transaction as priced
        if req.transaction:
            txn_update = req.transaction.copy()
            txn_update["status"] = "Priced"
            txn_update["updated_at"] = now
            deal_name = txn_update.get("deal_name")
            if deal_name:
                updated = store.update_record(
                    store.TRANSACTIONS,
                    lambda r: r.get("deal_name") == deal_name and r.get("status") != "Priced",
                    txn_update,
                )
                if not updated:
                    logger.warning(f"No matching transaction found to price for deal '{deal_name}'")

        # If we were engaged, mark the deal order as executed
        if req.deal_order:
            deal_name = req.deal_order.get("deal_name")
            if deal_name and req.deal_order.get("executed"):
                updated = store.update_record(
                    store.DEAL_ORDERS,
                    lambda r: r.get("deal_name") == deal_name,
                    {
                        "executed": True,
                        "executed_date": req.deal_order.get("executed_date", today),
                        "updated_at": now,
                    },
                )
                if not updated:
                    logger.warning(f"No matching deal order found for deal '{deal_name}'")

    # Move the email file to the "processed" folder so it won't appear again
    queue.move_to_processed(req.filename)
    queue.pop()

    return {"status": "ok", "message": f"Accepted {req.filename}"}


@app.post("/api/skip")
def skip():
    """Skip the current email (don't process it, move to back of queue)."""
    skipped = queue.skip()
    if skipped:
        return {"status": "ok", "skipped": skipped}
    return {"status": "empty", "message": "No emails in queue"}


# ─── Data Viewing Endpoints ──────────────────────────────────────────
# These are simple read-only endpoints that return the current data.

@app.get("/api/deals")
def get_deals():
    """Return all deal records."""
    return store.read_store(store.DEALS)


@app.get("/api/managers")
def get_managers():
    """Return all CLO manager records."""
    return store.read_store(store.MANAGERS)


@app.get("/api/transactions")
def get_transactions():
    """Return all transaction records."""
    return store.read_store(store.TRANSACTIONS)


@app.get("/api/deal-orders")
def get_deal_orders():
    """Return all deal order records."""
    return store.read_store(store.DEAL_ORDERS)


# ─── Manager Creation ────────────────────────────────────────────────

@app.post("/api/managers")
def create_manager(mgr: ManagerCreate):
    """Create a new CLO manager. Rejects duplicates based on short_name."""
    # Check uniqueness
    existing = store.find_record(
        store.MANAGERS, lambda r: r.get("short_name") == mgr.short_name
    )
    if existing:
        raise HTTPException(400, f"Manager '{mgr.short_name}' already exists")

    record = {
        "id": new_id(),
        "short_name": mgr.short_name,
        "legal_entity": mgr.legal_entity,
        "aum_bn": mgr.aum_bn,
        "hq": mgr.hq,
        "notes": mgr.notes,
        "created_at": now_iso().isoformat(),
    }
    store.append_record(store.MANAGERS, record)
    return record


# ─── Export / Reset ───────────────────────────────────────────────────

@app.get("/api/export")
def export_all():
    """Export ALL data stores in one JSON response (useful for backup)."""
    return {
        "deals": store.read_store(store.DEALS),
        "managers": store.read_store(store.MANAGERS),
        "transactions": store.read_store(store.TRANSACTIONS),
        "deal_orders": store.read_store(store.DEAL_ORDERS),
    }


@app.post("/api/reset")
def reset():
    """Clear all data stores. Use with caution — this deletes ALL saved data!"""
    for s in store.ALL_STORES:
        store.clear_store(s)
    return {"status": "ok", "message": "All data stores cleared"}


@app.post("/api/shutdown")
def shutdown():
    """Gracefully shut down the server. Called when the queue is empty and user confirms."""
    import threading
    # _server_handle is set by the notebook cell that starts the server
    handle = getattr(app.state, "_server_handle", None)
    if handle and hasattr(handle, "should_exit"):
        # Signal uvicorn to exit after this response completes
        threading.Timer(0.5, setattr, args=(handle, "should_exit", True)).start()
        return {"status": "ok", "message": "Server shutting down..."}
    return {"status": "ok", "message": "Shutdown signal sent (server may need manual stop)"}


# ─── Frontend Static Files ───────────────────────────────────────────
# Serves the web UI from frontend/index.html.
# If you don't need the web UI, you can remove this section.

frontend_dir = os.path.join(config.BASE_DIR, "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def serve_index():
    """Serve the frontend HTML page at the root URL."""
    index_path = os.path.join(config.BASE_DIR, "frontend", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Frontend not found. Place index.html in frontend/"}


# ─── Direct Execution ────────────────────────────────────────────────
# You can run this file directly: python backend/app.py
# Or use: python -m backend  (which calls __main__.py → uvicorn)
if __name__ == "__main__":
    uvicorn.run("backend.app:app", host=config.HOST, port=config.PORT, reload=True)
