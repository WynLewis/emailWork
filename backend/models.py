"""
Pydantic data models for CLO Email Extraction application.
============================================================

WHAT THIS FILE DOES:
    Defines the shape of every data record used in the application.
    Think of these as "table schemas" — they enforce the types and
    defaults for each field so you get clean, validated data.

HOW PYDANTIC WORKS (quick primer):
    - Each class below inherits from `BaseModel`.
    - Fields are declared as class-level type annotations (e.g. `deal_name: str`).
    - `Optional[X]` means the field can be `None` (i.e. missing / not yet known).
    - `Field(default_factory=...)` generates a default value each time a new
      record is created (e.g. a random UUID for `id`, or the current timestamp).
    - Pydantic auto-validates inputs:  if you pass a string where a float is
      expected, it will raise an error immediately rather than silently storing
      garbage.

HOW TO ADD A NEW FIELD:
    1. Add it to the relevant class below (e.g. `rating: Optional[str] = None`).
    2. If it should come from the LLM extraction, also add it to:
       - EXTRACTION_SCHEMA in extractor.py (type description for cloud LLMs)
       - NUEXTRACT_TEMPLATE in extractor.py (empty string for NuExtract)
       - ExtractionResult class at the bottom of this file.
    3. If it should be stored in the JSON files, the store module handles
       that automatically (it stores dicts, not Pydantic objects).

DATA FLOW:
    Email text → LLM → ExtractionResult → displayed in notebook →
    user corrects → dict saved to JSON stores (deals.json, transactions.json, etc.)
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field
import uuid


def new_id() -> str:
    """Generate a unique ID for a new record (UUID v4 string)."""
    return str(uuid.uuid4())


def now_iso() -> datetime:
    """Return the current UTC timestamp (used as default for created_at / updated_at)."""
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# DEAL — The CLO deal itself (e.g. "Dryden 120 CLO, Ltd.")
# ---------------------------------------------------------------------------
# Stored in: data/deals.json
# Created when: an "announced" email is processed
# Updated when: a subsequent email for the same deal_name arrives
class Deal(BaseModel):
    id: str = Field(default_factory=new_id)
    deal_name: str = ""                          # Full legal name of the CLO deal
    legal_entity: str = ""                       # Legal entity of the collateral manager
    collateral_manager_short: str = ""           # Short/common name (e.g. "PGIM")
    arranger: str = ""                           # Investment bank arranging the deal
    deal_type: str = ""                          # BSL, MM, Euro BSL, Euro MM, Other
    target_par: Optional[float] = None           # Target par amount in $MM (millions)
    reinvestment_period: str = ""                # e.g. "5 years"
    non_call_period: str = ""                    # e.g. "2 years"
    stated_maturity: str = ""                    # e.g. "July 2037"
    warehouse_provider: str = ""                 # Bank providing warehouse funding
    trustee: str = ""                            # e.g. "U.S. Bank"
    notes: str = ""                              # Free-text notes
    created_at: datetime = Field(default_factory=now_iso)
    updated_at: datetime = Field(default_factory=now_iso)


# ---------------------------------------------------------------------------
# MANAGER — Collateral manager / asset manager
# ---------------------------------------------------------------------------
# Stored in: data/managers.json
# A reference table of CLO managers you track.
class Manager(BaseModel):
    id: str = Field(default_factory=new_id)
    short_name: str = ""                         # Short name (e.g. "PGIM", "Ares")
    legal_entity: str = ""                       # Full legal entity name
    aum_bn: Optional[float] = None               # AUM in $billions
    hq: str = ""                                 # Headquarters location
    notes: str = ""
    created_at: datetime = Field(default_factory=now_iso)


# ---------------------------------------------------------------------------
# TRANSACTION — Lifecycle record for a deal (Announced → Updated → Priced)
# ---------------------------------------------------------------------------
# Stored in: data/transactions.json
# One transaction per deal, updated as the deal progresses through its lifecycle:
#   Announced  →  Updated (optional, guidance changes)  →  Priced
class Transaction(BaseModel):
    id: str = Field(default_factory=new_id)
    deal_name: str = ""                          # Links to Deal.deal_name
    collateral_manager: str = ""                 # Short name of the manager
    status: str = ""                             # Announced | Updated | Priced | Closed
    transaction_type: str = ""                   # New Issue | Reset | Refi
    announced_date: Optional[str] = None         # ISO date string (YYYY-MM-DD)
    ipt: str = ""                                # Initial Price Talk (tranche-level spread guidance)
    updated_guidance: str = ""                   # Revised spread guidance (if updated)
    priced_date: Optional[str] = None            # Date the deal priced
    final_pricing: str = ""                      # Final tranche-level pricing
    engaged: bool = False                        # Whether you placed an order
    notes: str = ""
    created_at: datetime = Field(default_factory=now_iso)
    updated_at: datetime = Field(default_factory=now_iso)


# ---------------------------------------------------------------------------
# DEAL ORDER — Your firm's order/allocation for a specific deal
# ---------------------------------------------------------------------------
# Stored in: data/deal_orders.json
# Created when you "engage" (place an order) on a transaction.
class DealOrder(BaseModel):
    id: str = Field(default_factory=new_id)
    deal_name: str = ""                          # Links to Deal.deal_name
    collateral_manager: str = ""
    transaction_type: str = ""
    engaged_date: Optional[str] = None           # Date the order was placed
    executed: bool = False                       # Whether the allocation was confirmed
    executed_date: Optional[str] = None
    tranche: str = ""                            # e.g. "Class A-1", "Class B"
    allocation: Optional[float] = None           # Allocation amount in $MM
    spread: Optional[float] = None               # Spread in basis points
    notes: str = ""
    created_at: datetime = Field(default_factory=now_iso)
    updated_at: datetime = Field(default_factory=now_iso)


# ---------------------------------------------------------------------------
# EXTRACTION RESULT — What the LLM returns after parsing an email
# ---------------------------------------------------------------------------
# This is NOT stored directly — it's the intermediate output of the LLM.
# The user reviews these fields, optionally corrects them, and then the
# corrected values are written into the Deal/Transaction/etc. stores above.
#
# All fields are Optional because any given email may only contain a subset.
# For example, an "announced" email won't have final_pricing or priced_date.
#
# extra="allow" lets this model carry custom fields added via field_mapping.json
# without requiring code changes. Any field you add in configure_fields.ipynb
# will flow through extraction → GUI → store automatically.
class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    email_type: Optional[str] = None  # announced, updated, priced
    deal_name: Optional[str] = None
    title: Optional[str] = None
    collateral_manager_legal_entity: Optional[str] = None
    collateral_manager_short: Optional[str] = None
    arranger: Optional[str] = None
    deal_type: Optional[str] = None
    collateral_type: Optional[str] = None
    target_par_mm: Optional[float] = None
    reinvestment_period: Optional[str] = None
    non_call_period: Optional[str] = None
    stated_maturity: Optional[str] = None
    warehouse_provider: Optional[str] = None
    trustee: Optional[str] = None
    announced_date: Optional[str] = None
    priced_date: Optional[str] = None
    ipt: Optional[str] = None
    updated_guidance: Optional[str] = None
    final_pricing: Optional[str] = None
    transaction_type: Optional[str] = None
    term: Optional[str] = None
    status: Optional[str] = None
