"""
Configuration for CLO Email Extraction application.
=====================================================

HOW IT WORKS:
    Every setting reads from an ENVIRONMENT VARIABLE first, and falls back
    to a sensible default if the variable is not set.

    You can set these variables in three ways (pick one):
        1. Create a `.env` file in the project root   (recommended — run setup_env.py)
        2. Export them in your shell:  export CLO_EXTRACTOR=nuextract
        3. Set them in the notebook:   os.environ["CLO_EXTRACTOR"] = "nuextract"
           (must be done *before* importing backend.config)

HOW TO ADAPT TO YOUR ENVIRONMENT:
    - Change EXTRACTOR to choose which LLM backend is used for parsing emails.
    - Change NUEXTRACT_DEVICE to match your hardware (see table below).
    - Change paths if your folder layout is different.

EXTRACTOR OPTIONS:
    "nuextract"  — Local open-source model. No API key needed. Requires GPU
                   or a fast CPU. Best for privacy / offline use.
    "anthropic"  — Anthropic's Claude API (cloud). Needs ANTHROPIC_API_KEY env var.
                   More accurate but costs money per request.
    "bedrock"    — AWS Bedrock (cloud). Needs AWS credentials configured.
                   Use this if your company has an AWS account with Bedrock enabled.
    "rules"      — Rule-based extraction. No LLM, no GPU, no API key needed.
                   Uses HTML table parsing + optional custom rules module.
                   Great for work machines where HuggingFace is blocked.
                   Output still saves as training data for future AI fine-tuning.

DEVICE OPTIONS (NUEXTRACT_DEVICE):
    "cuda"  — NVIDIA GPU (Linux/Windows with NVIDIA drivers + CUDA toolkit)
    "mps"   — Apple Silicon GPU (MacBook Pro M1/M2/M3/M4)
    "cpu"   — Any machine. Slowest but always works. ~10x slower than GPU.
"""

import os

# Load .env file if present (created by setup_env.py).
# This makes environment variables available without manually exporting them.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass  # python-dotenv not installed; rely on shell environment variables

# ---------------------------------------------------------------------------
# EXTRACTOR SELECTION
# ---------------------------------------------------------------------------
# Which extraction backend to use for parsing emails.
# Options: "nuextract" (local LLM), "anthropic" (cloud API), "bedrock" (AWS),
#          "rules" (rule-based, no LLM — works offline without HuggingFace)
EXTRACTOR = os.environ.get("CLO_EXTRACTOR", "nuextract")

# ---------------------------------------------------------------------------
# RULE-BASED EXTRACTOR settings (only used when EXTRACTOR = "rules")
# ---------------------------------------------------------------------------
# Path to a Python module containing your custom rule-based extraction logic.
# The module must define a function: extract_from_email(email_text: str) -> dict
# that returns a dict with ExtractionResult field names as keys.
#
# If not set, the built-in HTML table parser (parse_deal_fields + format_tranche_pricing)
# is used — this already handles most structured CLO emails well.
#
# Example:  CLO_RULES_MODULE=my_rules.clo_extractor
#           CLO_RULES_MODULE=/path/to/my_rules.py
RULES_MODULE = os.environ.get("CLO_RULES_MODULE", "")

# ---------------------------------------------------------------------------
# NuExtract (local model) settings
# ---------------------------------------------------------------------------
# The HuggingFace model ID. "numind/NuExtract-1.5" is a ~3.8B parameter
# model based on Microsoft's Phi-3 architecture, fine-tuned for structured
# extraction. It runs locally — no data leaves your machine.
NUEXTRACT_MODEL = os.environ.get("NUEXTRACT_MODEL", "numind/NuExtract-1.5")

# Which hardware to run the model on. See DEVICE OPTIONS above.
# The code auto-falls-back: cuda → mps → cpu if the chosen device is unavailable.
NUEXTRACT_DEVICE = os.environ.get("NUEXTRACT_DEVICE", "mps")

# ---------------------------------------------------------------------------
# Bedrock (AWS) settings
# ---------------------------------------------------------------------------
# Only used when EXTRACTOR = "bedrock". Requires AWS credentials
# (via ~/.aws/credentials, IAM role, or env vars AWS_ACCESS_KEY_ID etc.)
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"
)

# ---------------------------------------------------------------------------
# Anthropic direct API settings
# ---------------------------------------------------------------------------
# Only used when EXTRACTOR = "anthropic". Requires ANTHROPIC_API_KEY env var.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# ---------------------------------------------------------------------------
# FILE PATHS
# ---------------------------------------------------------------------------
# BASE_DIR = the project root (one level above this backend/ folder).
# All other paths are relative to BASE_DIR by default.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Where to look for new .txt email files to process.
INBOX_DIR = os.environ.get("CLO_INBOX_DIR", os.path.join(BASE_DIR, "data", "inbox"))

# Where processed emails are moved after acceptance.
PROCESSED_DIR = os.environ.get(
    "CLO_PROCESSED_DIR", os.path.join(BASE_DIR, "data", "processed")
)

# Root of the data directory (JSON stores live here).
DATA_DIR = os.environ.get("CLO_DATA_DIR", os.path.join(BASE_DIR, "data"))

# ---------------------------------------------------------------------------
# FINE-TUNING PATHS
# ---------------------------------------------------------------------------
# JSONL file where corrected extractions are saved as training data.
# Each line is one JSON object: {email_text, extraction, timestamp}.
# This file grows every time you accept an extraction in Cell 2 of the notebook.
TRAINING_DATA = os.path.join(DATA_DIR, "training_examples.jsonl")

# Directory where the LoRA adapter weights are saved after fine-tuning.
# The extractor automatically loads this adapter (if it exists) on startup.
LORA_ADAPTER_DIR = os.environ.get(
    "CLO_LORA_DIR", os.path.join(BASE_DIR, "models", "nuextract-lora")
)

# ---------------------------------------------------------------------------
# WEB SERVER (optional — only used if you run `python -m backend`)
# ---------------------------------------------------------------------------
HOST = os.environ.get("CLO_HOST", "0.0.0.0")
PORT = int(os.environ.get("CLO_PORT", "8000"))
