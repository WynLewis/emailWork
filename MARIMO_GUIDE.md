# CLO Deal Extraction & Review — Marimo App Guide

## Quick Start

```bash
# 1. Install dependencies (first time only)
pip install marimo pydantic pyyaml filelock

# 2. Migrate existing JSON data to SQLite (first time only)
python -m backend.store_db migrate

# 3. Run the app
marimo run app.py
```

Open the URL printed in the terminal (usually `http://localhost:2718`).

To edit the app layout/code: `marimo edit app.py`

---

## What the App Does

The marimo app is a **single interactive dashboard** for processing CLO deal emails and Bloomberg pricing data. It has 5 tabs:

### Tab 1: Email Review

**Purpose:** Extract deal information from emails, review, edit, and save.

**Data flow:**
```
data/inbox/*.txt          ← Drop email files here
       ↓
my_rules.py               ← Rules-based extraction (regex patterns)
       ↓
backend/extractor.py       ← Tranche pricing parser (HTML tables)
       ↓
extraction_log (SQLite)    ← Pending extractions queued for review
       ↓
YOU review & edit          ← Marimo app shows email + form side-by-side
       ↓
deals / transactions /     ← Accepted data saved to SQLite
managers (SQLite)
       ↓
training_data (SQLite)     ← Email + corrected extraction saved as training pair
```

**What you see:**
- **Left panel:** Email HTML rendered in an iframe (scrollable)
- **Right panel:**
  - **Diff view** (yellow callout) — if this deal already exists in the DB, shows what's changing
  - **Table breakdown** — 3 sections showing exactly which SQLite table each field writes to, with confidence badges:
    - 🟢 **High** — strong regex match on structured text (`DEAL NAME:`, `MANAGER:`)
    - 🟡 **Medium** — matched from legal boilerplate or derived from other fields
    - 🔴 **Low** — fallback/default value (e.g., BSL when no collateral type found)
    - ⚫ **Missing** — field not found in email
  - **Editable form** — text fields, dropdowns, text areas for all extracted values
  - **Accept & Save** — writes to all 3 tables + saves training pair
  - **Skip** — marks as skipped, moves to next

### Tab 2: Bloomberg

**Purpose:** Import priced deals from Bloomberg pricing CSV.

**Data flow:**
```
pricings_YYYYMMDD.csv     ← Drop Bloomberg CSV in project root
       ↓
bbg_pricing.py             ← Parses CSV, filters already-priced deals
       ↓
Selectable table           ← Multi-select which deals to import
       ↓
deals / transactions       ← Selected deals saved to SQLite as "Priced"
(SQLite)
```

**Notes:**
- The CSV comes from Bloomberg's CLO pricing screen
- Deals already in the DB with matching pricing date are skipped
- When xlwings + Bloomberg Terminal are available, BQL formulas pull tranche-level detail
- Without Bloomberg, only deal-level data (name, type, arranger, date) is imported

### Tab 3: Database

**Purpose:** Browse the SQLite tables directly.

**Sub-tabs:**
- **deals** — all deals with title, collateral type, manager, Bloomberg name
- **transactions** — all transactions with status, type, agent, term, pricing preview
- **managers** — all managers with name, short name, ultimate parent
- **Schema & Relationships** — ASCII diagram showing table structure and foreign keys

### Tab 4: SharePoint Sync

**Purpose:** Compare SQLite data vs SharePoint exports, generate CSVs for import.

**Data flow:**
```
data/csv/clo-deals.csv         ← SharePoint export (you drop this here)
data/csv/clo-transactions.csv  ← SharePoint export
       ↓
Compare with SQLite             ← App finds records in SQLite but not in SharePoint
       ↓
data/export/new_deals.csv       ← CSV of new deals to import into SharePoint
data/export/new_priced_transactions.csv  ← CSV of new priced transactions
```

**Stat cards show:**
- SP Deals vs DB Deals vs New Deals
- SP Transactions vs DB Priced vs New Priced

### Tab 5: Rules Audit

**Purpose:** Analyze extraction quality to guide rules improvement.

Shows which fields are populated across your training data (accepted extractions), helping you identify which regex patterns in `my_rules.py` need work.

---

## Database Schema

### SQLite file: `data/clo.db`

```
┌──────────────────────────┐
│        managers          │
├──────────────────────────┤
│ id          INTEGER (PK) │
│ name        TEXT (UNIQUE) │ ◄─── deals.collateral_manager
│ short_name  TEXT          │      transactions.collateral_manager
│ ultimate_parent TEXT      │
│ crd_number  TEXT          │
│ sec_number  TEXT          │
│ website     TEXT          │
│ created_at  TEXT          │
└──────────────────────────┘

┌──────────────────────────────┐
│           deals              │
├──────────────────────────────┤
│ id                INTEGER PK │
│ title             TEXT UNIQUE│ ◄─── transactions.deal
│ collateral_type   TEXT       │
│ collateral_manager TEXT      │ ───► managers.name
│ bloomberg_deal_name TEXT     │
│ intex_deal        TEXT       │
│ intex_preprice    TEXT       │
│ deal_documents    TEXT       │
│ created_at        TEXT       │
│ updated_at        TEXT       │
└──────────────┬───────────────┘
               │
               │ deals.title = transactions.deal
               ▼
┌──────────────────────────────────┐
│         transactions             │
├──────────────────────────────────┤
│ id                    INTEGER PK │
│ title                 TEXT       │
│ deal                  TEXT       │ ───► deals.title
│ collateral_type       TEXT       │
│ collateral_manager    TEXT       │ ───► managers.name
│ transaction_type      TEXT       │      (New Issue|Reset|Refinancing|Re-Issue)
│ status                TEXT       │      (Announced|Priced|Upcoming|Cancelled)
│ placement_agent       TEXT       │      (24 allowed banks)
│ term                  TEXT       │      (e.g., 5nc2, 3nc1, 0.5nc0.5)
│ ipt                   TEXT       │      (multi-line tranche pricing)
│ final_pricing_details TEXT       │      (multi-line tranche pricing)
│ announcement_date     TEXT       │
│ priced_date           TEXT       │
│ engaged               INTEGER    │      (0 or 1)
│ executed              INTEGER    │      (0 or 1)
│ created_at            TEXT       │
│ updated_at            TEXT       │
└──────────────────────────────────┘

┌──────────────────────────────┐
│       training_data          │
├──────────────────────────────┤
│ id              INTEGER PK   │
│ email_html      TEXT         │    Raw email HTML
│ extraction_json TEXT         │    Corrected extraction (JSON)
│ source          TEXT         │    "email" or "bbg"
│ filename        TEXT         │    Source filename
│ accepted_at     TEXT         │
└──────────────────────────────┘

┌──────────────────────────────┐
│       extraction_log         │
├──────────────────────────────┤
│ id              INTEGER PK   │
│ source          TEXT         │    "email" or "bbg"
│ filename        TEXT         │
│ deal_name       TEXT         │
│ extraction_json TEXT         │    Full extraction (JSON)
│ status          TEXT         │    "pending" | "accepted" | "skipped"
│ reviewed_at     TEXT         │
│ created_at      TEXT         │
└──────────────────────────────┘
```

### Key Relationships

| From | → To | Meaning |
|------|------|---------|
| `transactions.deal` | → `deals.title` | Every transaction belongs to a deal |
| `deals.collateral_manager` | → `managers.name` | Every deal has a manager |
| `transactions.collateral_manager` | → `managers.name` | Denormalized for convenience |

### Allowed Values

**Transaction Type:** New Issue, Reset, Refinancing, Re-Issue

**Status:** Announced, Priced, Upcoming, Cancelled

**Placement Agent:** JPMorgan, Jefferies, Bank of America, Citigroup, Morgan Stanley, CIBC, Scotia, GreensLedge, Wells Fargo, Goldman Sachs, BNP Paribas, Mitsubishi, Natixis, RBC, SMBC, Nomura, Santander, Barclays, Mizuho, Capital One, Deutsche Bank, BMO, Atlas, Societe Generale

**Collateral Type:** BSL, MM, PC, Infra, EM, MM Rated Feeder, Infra Rated Feeder

---

## File Layout

```
emailWork/
├── app.py                      ← Marimo app (run with: marimo run app.py)
├── batch_extract.py            ← CLI batch extraction (headless)
├── my_rules.py                 ← Rules-based extraction engine
├── extraction_rules.yaml       ← YAML extraction patterns
├── custom_rules_template.py    ← Blank template (reference only)
├── pricings_YYYYMMDD.csv       ← Bloomberg pricing CSV (drop here)
│
├── backend/
│   ├── store_db.py             ← SQLite CRUD operations
│   ├── store.py                ← Legacy JSON store (used by old notebook)
│   ├── extractor.py            ← Extraction backends (Rules, LLM, NuExtract)
│   ├── bbg_pricing.py          ← Bloomberg CSV parser + BQL integration
│   ├── field_mapping.py        ← GUI field name mapping
│   ├── models.py               ← Pydantic data models
│   ├── config.py               ← Environment configuration
│   ├── watcher.py              ← Email inbox queue
│   ├── app.py                  ← FastAPI web server (alternative to marimo)
│   ├── finetune.py             ← LoRA fine-tuning for NuExtract
│   └── sharepoint_sync.py      ← SharePoint format conversion
│
├── data/
│   ├── clo.db                  ← SQLite database (created by migrate)
│   ├── field_mapping.json      ← GUI field configuration
│   ├── inbox/                  ← Drop .txt email files here
│   ├── processed/              ← Emails moved here after acceptance
│   ├── csv/                    ← SharePoint CSV/JSON exports
│   │   ├── clo-deals.csv
│   │   ├── clo-managers.csv
│   │   ├── clo-transactions.csv
│   │   └── *.json              ← JSON versions of same data
│   └── export/                 ← Generated CSVs for SharePoint import
│       ├── new_deals.csv
│       └── new_priced_transactions.csv
│
├── extractor_gui.ipynb         ← DEPRECATED (use app.py instead)
├── bbg_pricing.ipynb           ← Bloomberg pricing notebook
└── configure_fields.ipynb      ← Field mapping configuration
```

---

## Extraction Pipeline

### Email Extraction (`my_rules.py`)

Supports 5 bank email formats:

| Bank | Deal Name Format | Manager Format | Arranger Detection |
|------|-----------------|----------------|-------------------|
| **BNP Paribas** | Bold: `Refinancing of <Name>` | Legal boilerplate: `engaged by <Entity> (the "Manager")` | Sender domain: `@us.bnpparibas.com` |
| **Citigroup** | Structured: `DEAL NAME: <Name>` | Structured: `MANAGER: <Entity>` | Legal: `Citigroup Global Markets` |
| **Goldman Sachs** | Subject: `CLO Refi: <Name>` | Labeled: `Collateral Manager: <Entity>` | Sender domain: `@gs.com` |
| **Barclays** | Announcement: `ELMWOOD CLO 28 PARTIAL REFI` | Table cell: `Collateral Manager: <Entity>` | Sender domain: `@barclays.com` |
| **SMBC** | Structured: `DEAL NAME: <Name>` | Structured: `MANAGER: <Entity>` | Sender domain: `@smbcnikko-si.com` |

### Term Extraction

Handles multiple formats:
- Direct: `5/2 transaction` → `5nc2`
- Explicit: `3nc1`, `5NC2`
- Period descriptions: `Reinvestment Period: ~3Y. Non-Call Period: ~1Y.` → `3nc1`
- Exact dates: `04/17/2029` → derive years from closing date
- Bracket notation: `Approx. [3] years` → `3`
- Static deals: `Reinvestment Period: N/A` → `0`

### Pricing Extraction

Parses tranche pricing from HTML tables and plain text:
- **Sr/Jr AAA:** Multiple AAA tranches get `(Sr AAA)` / `(Jr AAA)` labels
- **Non-AAA:** Aggregated by rating bucket with size-weighted average spread
- **Formats handled:** `SOFR + 123`, `S + 145a`, `SUBJECT (115)`, `Call Desk (127)`, `RETAINED`
- **Output:** `A1R (Sr AAA) @ 124-125 dm\nA2R (Jr AAA) @ 145a dm\nBR (AA) @ 160a dm`

---

## Rules Improvement Workflow

1. **Process emails** — run `marimo run app.py`, extract emails, review in Email Review tab
2. **Accept/edit** — every acceptance saves `(email_html, corrected_fields)` to `training_data` table
3. **Check Rules Audit tab** — shows which fields you correct most often
4. **Fix patterns** in `my_rules.py` — the fields with lowest auto-fill rates need better regex
5. **Re-test** — run `python batch_extract.py --dry-run` to see extraction results without saving
6. **When ready for LLM** — training pairs become fine-tuning data:
   - NuExtract: `python -m backend.finetune` creates LoRA adapter from training data
   - Claude API: training pairs can be used as few-shot examples in the system prompt

---

## CLI Commands

```bash
# Run the interactive app
marimo run app.py

# Edit the app (development mode)
marimo edit app.py

# Batch extract (headless, no browser)
python batch_extract.py                    # process all, show review
python batch_extract.py --emails-only      # only emails
python batch_extract.py --bbg-only         # only Bloomberg CSV
python batch_extract.py --dry-run          # show what would happen
python batch_extract.py --save             # save to SQLite
python batch_extract.py --export out.json  # export for editing

# SQLite management
python -m backend.store_db migrate         # import JSON → SQLite
python -m backend.store_db stats           # show table counts
python -m backend.store_db export          # export SQLite → CSV

# Test extraction on a single email
python -c "
from my_rules import extract_from_email
with open('data/inbox/your_email.txt') as f:
    result = extract_from_email(f.read())
for k, v in sorted(result.items()):
    if v and k != '_confidence': print(f'{k}: {v}')
"
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CLO_EXTRACTOR` | `nuextract` | Extraction backend: `rules`, `anthropic`, `bedrock`, `nuextract` |
| `CLO_RULES_MODULE` | — | Path to custom rules module (e.g., `my_rules.py`) |
| `CLO_INBOX_DIR` | `data/inbox` | Where to find email .txt files |
| `CLO_DATA_DIR` | `data` | Root data directory |
| `ANTHROPIC_API_KEY` | — | For Claude API extraction |
| `NUEXTRACT_DEVICE` | `mps` | GPU device: `cuda`, `mps`, `cpu` |
