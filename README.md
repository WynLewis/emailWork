# CLO Email Extraction Workstation

A system for processing CLO (Collateralized Loan Obligation) deal data from **two sources**:
1. **Emails** — Announcements and updated guidance extracted via LLM or rules-based parsing
2. **Bloomberg** — Pricing data pulled via BQL formulas with tranche-level detail

Both sources feed into the same JSON stores and share the same web-style GUI.

---

## Table of Contents
- [Overview](#overview)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Email Extraction (Announcements)](#email-extraction-announcements)
- [Bloomberg Pricing Ingestion](#bloomberg-pricing-ingestion)
- [Extraction Methods](#extraction-methods)
- [Field Mapping & Customization](#field-mapping--customization)
- [Sharepoint Integration](#sharepoint-integration)
- [How Fine-Tuning Works](#how-fine-tuning-works)
- [Configuration](#configuration)
- [API Endpoints](#api-endpoints)
- [Data Files](#data-files)
- [Adapting to Your Environment](#adapting-to-your-environment)
- [Troubleshooting](#troubleshooting)

---

## Overview

### Two ingestion paths, one data store:

| Source | What it handles | How it works |
|--------|----------------|--------------|
| **Emails** | Announced, Updated guidance | LLM or rule-based extraction from HTML emails |
| **Bloomberg** | Priced deals + tranche detail | CSV parsing + BQL formulas via xlwings |

For email extraction, the system:
1. Reads email text from `data/inbox/`
2. Extracts structured fields (LLM, rules-based, or HTML parsing)
3. Presents the extraction in a web-style GUI for review
4. Saves corrected data to JSON stores + training data for fine-tuning

For Bloomberg pricing, the system:
1. Parses a `pricings_YYYYMMDD.csv` export
2. Loops through priced deals, running BQL formulas via xlwings to pull tranche data
3. Assigns ratings using credit subordination analysis
4. Presents each deal in a matching GUI for review
5. Updates deals, transactions, and managers

**Key principle: the model learns from YOUR corrections and gets better at YOUR specific emails.**

---

## Quick Start

### First-time setup (automated):
```bash
python3 setup_env.py
```
This creates the venv, installs packages, detects your hardware, and configures everything.

### Work machine (no HuggingFace/GPU):
```bash
# Just set this in your .env file:
CLO_EXTRACTOR=rules
pip install pydantic python-dotenv fastapi uvicorn ipywidgets filelock
```

### Processing emails:
```
Open extractor_gui.ipynb in VS Code
  Cell 1 → Setup (loads extractor, queue, field mapping)
  Cell 2 → Interactive GUI (full web interface embedded in notebook)
```

### Processing Bloomberg pricings:
```
Drop pricings_YYYYMMDD.csv in project root
Open bbg_pricing.ipynb in VS Code
  Cell 1 → Setup (loads CSV, connects to stores)
  Cell 2 → Interactive GUI (deal list + tranche detail + accept/skip)
```

### Using the web server (alternative):
```bash
source .venv/bin/activate
python -m backend               # Starts on http://localhost:8000
```

---

## Project Structure

```
clo-email-extractor/
├── setup_env.py                # One-time setup script
├── extractor_gui.ipynb         # Email extraction GUI (notebook)
├── bbg_pricing.ipynb           # Bloomberg pricing GUI (notebook)
├── configure_fields.ipynb      # Field mapping configuration GUI
├── custom_rules_template.py    # Template for custom extraction rules
├── requirements.txt            # Python dependencies
│
├── backend/                    # Core Python modules
│   ├── __init__.py
│   ├── __main__.py             # Entry point: python -m backend
│   ├── config.py               # Configuration (env vars, paths, defaults)
│   ├── models.py               # Pydantic data models (Deal, Transaction, etc.)
│   ├── store.py                # JSON file persistence with file locking
│   ├── watcher.py              # Email file queue manager
│   ├── extractor.py            # LLM/rules extraction (NuExtract, Anthropic, rules)
│   ├── field_mapping.py        # Configurable field → store mapping + JSON scanner
│   ├── bbg_pricing.py          # Bloomberg CSV + BQL pricing pipeline
│   ├── sharepoint_sync.py      # Sharepoint list export converter
│   ├── finetune.py             # LoRA fine-tuning on user corrections
│   └── app.py                  # FastAPI web server
│
├── data/                       # All persisted data
│   ├── inbox/                  # Drop email .txt files here
│   ├── processed/              # Processed emails moved here
│   ├── deals.json              # Deal records
│   ├── managers.json           # CLO manager records
│   ├── transactions.json       # Transaction lifecycle records
│   ├── deal_orders.json        # Your deal orders
│   ├── field_mapping.json      # Custom field mapping config (auto-generated)
│   └── training_examples.jsonl # Training data for fine-tuning
│
├── models/                     # Model artifacts
│   └── nuextract-lora/         # LoRA adapter after fine-tuning
│
└── frontend/
    └── index.html              # Web UI (also embedded in notebooks)
```

---

## Email Extraction (Announcements)

Open `extractor_gui.ipynb`:

| Cell | What |
|------|------|
| **1** | Setup — loads extractor, email queue, field mapping |
| **2** | Interactive GUI — full web interface embedded in notebook (no server needed) |
| **3** | *(Optional)* Launch web UI in a separate browser tab |
| **4+** | View saved data, training status, fine-tune model |

The GUI shows the email on the left and extracted fields on the right. Review, edit, Accept or Skip. All accepted extractions save to both data stores and training data.

### Email types:
- **Announced** — Creates Deal + Transaction records
- **Updated** — Updates existing Transaction with revised guidance
- **Priced** — Marks Transaction as Priced (note: pricings now primarily come from Bloomberg)

---

## Bloomberg Pricing Ingestion

Open `bbg_pricing.ipynb`:

| Cell | What |
|------|------|
| **1** | Setup — finds most recent `pricings_*.csv`, loads stores |
| **2** | Interactive GUI — deal list with tranche detail, ratings, and formatted pricing |
| **3** | *(Optional)* Batch run all deals without GUI review |

### How it works:
1. **CSV parsing** — Reads Bloomberg pricing export, filters deals where Pricing != TBA
2. **BQL formulas** — For each deal, xlwings pastes a BQL formula into Excel to pull tranche-level data (size, spread, class, dates, collateral type, tranche type)
3. **Rating assignment** — Filters X/equity tranches, sorts by spread, calculates credit subordination (debt par + 9% assumed equity), assigns ratings using class letter grouping + sub thresholds
4. **Final pricing format** — AAA tranches kept as Sr/Jr separate; all other ratings blended via size-weighted average spread
5. **Store updates** — Creates/updates deals, transactions (marked Priced), and managers

### Rating logic:
- Tranches sharing the same first letter (A1R2/A2R2 → "A") = same rating bucket
- Multiple tranches in same bucket → Sr/Jr (validated by subordination threshold)
- If Jr tranche's sub drops below the rating threshold → it's actually the next rating down

### Transaction type:
- `RSET` in `mtg_tranche_typ_long` → **Reset/Refi**
- No `RSET` → **New Issue**
- Reinvestment period length makes reset vs refi obvious

### Dedup:
Already-priced deals (matching deal_name + pricing_date) are automatically skipped.

### CSV-only mode:
Set `USE_XLWINGS = False` to process without Bloomberg/Excel. Deals get marked as Priced but without tranche-level detail.

---

## Extraction Methods

| Method | Config | When to use |
|--------|--------|-------------|
| **NuExtract** | `CLO_EXTRACTOR=nuextract` | Local LLM, best accuracy, needs GPU |
| **Anthropic** | `CLO_EXTRACTOR=anthropic` | Cloud API, needs API key |
| **Bedrock** | `CLO_EXTRACTOR=bedrock` | AWS Bedrock, needs AWS credentials |
| **Rules** | `CLO_EXTRACTOR=rules` | No LLM needed, works offline, great for work machines |

### Rules-based extraction:
Uses built-in HTML table parsing + optional custom rules module. Set `CLO_RULES_MODULE=my_rules.py` to point at your custom extraction logic.

Copy `custom_rules_template.py` to get started:
```python
def extract_from_email(email_text: str) -> dict:
    result = {}
    # Your regex/keyword rules here
    return result
```

All accepted extractions still save as training data regardless of extraction method.

---

## Field Mapping & Customization

Open `configure_fields.ipynb` to configure how extraction fields map to your JSON stores.

### Auto-discovery:
Cell 1 scans your actual JSON files, discovers every field, and auto-matches them to known extractable email fields (~45 recognized CLO field patterns). Unknown fields are flagged for your review.

### What you can do:
- **Remap fields** to different stores than the defaults
- **Add new fields** that get extracted from emails and saved to your JSONs
- **Mark fields as SKIP** (always blank, won't be extracted)
- **Remove fields** you don't need

Config is saved to `data/field_mapping.json` and picked up automatically by both the email and pricing GUIs.

---

## Sharepoint Integration

If your deal data lives in Sharepoint lists, use `backend/sharepoint_sync.py` to convert between Sharepoint's XML schema format and the flat JSON records used by the extractor.

```bash
# Flatten Sharepoint exports → flat JSONs for processing
python -m backend.sharepoint_sync flatten

# Export flat records → Sharepoint-compatible format
python -m backend.sharepoint_sync export

# Show what's new/changed vs Sharepoint
python -m backend.sharepoint_sync diff
```

Supports the deals, managers, and transactions Sharepoint list schemas.

---

## How Fine-Tuning Works

After collecting corrected extractions, you can fine-tune the NuExtract model:

1. Training examples are loaded from `data/training_examples.jsonl`
2. Each example = (email prompt, corrected JSON output)
3. A **LoRA adapter** is trained (~50-100MB) that adjusts the model's behavior
4. The adapter is saved to `models/nuextract-lora/`
5. On next startup, the extractor automatically loads the adapter
6. The model produces better extractions for your specific email format

**Works across extraction methods**: corrections made in rules-based mode still build training data. When you enable the AI model later, it can fine-tune on everything you've processed.

---

## Configuration

All settings in `backend/config.py`, overridden via `.env` file or environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `CLO_EXTRACTOR` | `nuextract` | Extractor: `nuextract`, `anthropic`, `bedrock`, `rules` |
| `CLO_RULES_MODULE` | — | Path to custom rules module (for `rules` extractor) |
| `NUEXTRACT_MODEL` | `numind/NuExtract-1.5` | HuggingFace model name |
| `NUEXTRACT_DEVICE` | `mps` | Device: `mps` (Mac GPU), `cuda` (NVIDIA), `cpu` |
| `ANTHROPIC_API_KEY` | — | Required if using Anthropic extractor |
| `CLO_INBOX_DIR` | `data/inbox` | Where to scan for email files |
| `CLO_DATA_DIR` | `data` | Where JSON stores are saved |
| `CLO_LORA_DIR` | `models/nuextract-lora` | Where fine-tuned adapter is saved |

---

## API Endpoints

Available when running `python -m backend` (web server mode):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/queue` | List remaining emails |
| GET | `/api/next` | Get next email with extraction |
| POST | `/api/accept` | Accept and commit reviewed data |
| POST | `/api/skip` | Skip current email |
| GET | `/api/deals` | Get all deals |
| GET | `/api/managers` | Get all managers |
| GET | `/api/transactions` | Get all transactions |
| GET | `/api/deal-orders` | Get all deal orders |
| POST | `/api/managers` | Create a new manager |
| GET | `/api/export` | Export all data as JSON |

---

## Data Files

| File | Contents |
|------|----------|
| `data/deals.json` | Deal records (name, manager, arranger, target par, etc.) |
| `data/managers.json` | CLO manager records (name, legal entity) |
| `data/transactions.json` | Transaction lifecycle (announced → updated → priced) + tranche_detail |
| `data/deal_orders.json` | Your deal orders (tranche, allocation, execution) |
| `data/field_mapping.json` | Custom field mapping config |
| `data/training_examples.jsonl` | Training data for fine-tuning |

---

## Adapting to Your Environment

### Work machine (no GPU, no HuggingFace):
Set `CLO_EXTRACTOR=rules` in `.env`. No torch, no transformers needed. Everything still saves as training data.

### Adding new extraction fields:
Run `configure_fields.ipynb` → Cell 4 to add fields. They automatically appear in the GUI and save to the correct JSON store.

### Using your existing JSON structure:
Drop your JSONs into `data/`, run `configure_fields.ipynb` → Cell 1 scans them and auto-generates the field mapping.

### Sharepoint data:
Use `python -m backend.sharepoint_sync flatten` to convert Sharepoint exports to flat JSON, then `export` to push changes back.

### Corporate network / no internet:
Download the model on another machine, copy `~/.cache/huggingface/hub/models--numind--NuExtract-1.5/` to the work machine. Or just use `CLO_EXTRACTOR=rules`.

### Switching to a real database:
Replace the functions in `backend/store.py` with database queries. The rest of the code only calls `store.append_record()`, `store.update_record()`, etc.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Firewall blocks web server | Use the notebook GUI (Cell 2) — no server needed |
| "No valid JSON found in LLM response" | Model output truncated. Increase `max_new_tokens` in extractor.py |
| FallbackExtractor used instead of NuExtract | Restart Jupyter kernel and re-run Cell 1 |
| Model download is slow | Model is ~7.5GB. Use `setup_env.py` to pre-download |
| MPS out of memory | Close other apps. Model needs ~10GB unified memory |
| xlwings can't connect to Excel | Make sure Bloomberg Terminal + Excel are running |
| BQL returns no data | Check deal name matches exactly. Try the formula manually in Excel |
| Ratings look wrong | Adjust `SUB_THRESHOLDS` in `backend/bbg_pricing.py` |
| Fields not showing in GUI | Run `configure_fields.ipynb` to update field mapping |
