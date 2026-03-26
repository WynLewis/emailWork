#!/usr/bin/env python3
"""
NWInvestments — First-time Setup Script
========================================

PURPOSE:
    Run this ONCE when you deploy the project to a new machine. It creates
    everything needed to run the CLO email extraction system.

USAGE:
    python3 setup_env.py

    The script is interactive — it will ask before overwriting existing files.
    Safe to run multiple times (won't destroy existing data).

WHAT IT DOES (7 steps):
    1. Creates a Python virtual environment (.venv/)
       — Keeps this project's packages isolated from your system Python
    2. Installs all dependencies from requirements.txt
       — PyTorch, transformers, FastAPI, peft, etc.
    3. Detects your hardware (NVIDIA CUDA / Apple MPS / CPU-only)
       — Determines the fastest available device for model inference
    4. Creates a .env config file with the right settings
       — Sets extractor type, model name, device, API keys
    5. Creates required directories (data/inbox, data/processed, models/)
       — These are where emails and model adapters are stored
    6. Initializes empty JSON data stores (deals.json, etc.)
       — The JSON files that store your extracted data
    7. Registers a Jupyter kernel so the notebook can find the venv
       — Makes "NWInvestments (.venv)" appear in VS Code's kernel picker
    8. (Optional) Pre-downloads the NuExtract model (~7.5GB)
       — So the first notebook run doesn't have to wait for download

HOW TO ADAPT FOR YOUR WORK ENVIRONMENT:
    - If you can't use pip to install packages, you may need to work with
      your IT department to approve the packages in requirements.txt
    - If you're behind a corporate proxy, set HTTP_PROXY/HTTPS_PROXY env
      vars before running this script
    - If you don't have internet access, download the model on another machine
      and copy ~/.cache/huggingface/hub/models--numind--NuExtract-1.5/ to
      the same path on your work machine
    - If your work machine has an NVIDIA GPU, the script will auto-detect it
      and configure CUDA; otherwise it falls back to CPU
"""

import os
import subprocess
import sys
import json
import platform
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))  # Project root directory

# Paths to the venv's Python and pip executables (handles Windows vs macOS/Linux)
VENV_DIR = os.path.join(ROOT, ".venv")
VENV_PYTHON = os.path.join(VENV_DIR, "bin", "python") if os.name != "nt" else os.path.join(VENV_DIR, "Scripts", "python.exe")
VENV_PIP = os.path.join(VENV_DIR, "bin", "pip") if os.name != "nt" else os.path.join(VENV_DIR, "Scripts", "pip.exe")
ENV_FILE = os.path.join(ROOT, ".env")       # Config file for environment variables
DATA_DIR = os.path.join(ROOT, "data")       # Where all JSON stores and emails live


def banner(msg):
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}")


def run(cmd, **kwargs):
    """Run a command and exit on failure."""
    print(f"  $ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"  ERROR: Command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


# -------------------------------------------------------------------
# 1. Create virtual environment
# -------------------------------------------------------------------
def setup_venv():
    banner("1/7  Creating virtual environment")
    if os.path.exists(VENV_DIR):
        print(f"  .venv already exists at {VENV_DIR}")
        resp = input("  Recreate it? [y/N]: ").strip().lower()
        if resp == "y":
            shutil.rmtree(VENV_DIR)
        else:
            print("  Keeping existing venv.")
            return

    run([sys.executable, "-m", "venv", VENV_DIR])
    print("  Created .venv/")


# -------------------------------------------------------------------
# 2. Install dependencies
# -------------------------------------------------------------------
def install_deps():
    banner("2/7  Installing dependencies")
    #run([VENV_PIP, "install", "--upgrade", "pip"])
    run([VENV_PIP, "install", "-r", os.path.join(ROOT, "requirements.txt")])
    print("  All packages installed.")


# -------------------------------------------------------------------
# 3. Detect hardware
# -------------------------------------------------------------------
def detect_device():
    banner("3/7  Detecting hardware")

    proc = platform.processor()
    machine = platform.machine()
    system = platform.system()
    print(f"  System: {system} | Machine: {machine} | Processor: {proc}")

    # Try importing torch to check for CUDA / MPS
    result = subprocess.run(
        [VENV_PYTHON, "-c", """
import torch
print(f"cuda:{torch.cuda.is_available()}")
print(f"mps:{torch.backends.mps.is_available()}")
if torch.cuda.is_available():
    print(f"gpu_name:{torch.cuda.get_device_name(0)}")
"""],
        capture_output=True, text=True
    )

    output = result.stdout.strip()
    has_cuda = "cuda:True" in output
    has_mps = "mps:True" in output

    if has_cuda:
        gpu_name = ""
        for line in output.split("\n"):
            if line.startswith("gpu_name:"):
                gpu_name = line.split(":", 1)[1]
        print(f"  ✅ CUDA GPU detected: {gpu_name}")
        return "cuda"
    elif has_mps:
        print(f"  ✅ Apple MPS (Metal) detected")
        return "mps"
    else:
        print(f"  ⚠️  No GPU detected, using CPU (extraction will be slower)")
        return "cpu"


# -------------------------------------------------------------------
# 4. Create .env file
# -------------------------------------------------------------------
def create_env_file(device):
    banner("4/7  Creating .env configuration")

    if os.path.exists(ENV_FILE):
        print(f"  .env already exists.")
        resp = input("  Overwrite it? [y/N]: ").strip().lower()
        if resp != "y":
            print("  Keeping existing .env")
            return

    env_content = f"""# NWInvestments — Environment Configuration
# Generated by setup_env.py

# Extractor: "nuextract" (local), "anthropic" (API), "bedrock" (AWS), or "rules" (no LLM)
# Use "rules" on machines where HuggingFace/torch can't be installed.
# All accepted extractions still save as training data for future AI fine-tuning.
CLO_EXTRACTOR=nuextract

# NuExtract model settings (only used when CLO_EXTRACTOR=nuextract)
NUEXTRACT_MODEL=numind/NuExtract-1.5
NUEXTRACT_DEVICE={device}

# Rule-based extractor settings (only used when CLO_EXTRACTOR=rules)
# Point this at your custom rules file. If blank, uses built-in HTML parser.
# CLO_RULES_MODULE=my_rules.py

# Anthropic API (if using CLO_EXTRACTOR=anthropic)
# ANTHROPIC_API_KEY=sk-ant-...
# ANTHROPIC_MODEL=claude-sonnet-4-20250514

# AWS Bedrock (if using CLO_EXTRACTOR=bedrock)
# BEDROCK_REGION=us-east-1
# BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-20250514-v1:0

# Paths (defaults are fine for most setups)
# CLO_INBOX_DIR=data/inbox
# CLO_PROCESSED_DIR=data/processed
# CLO_DATA_DIR=data
# CLO_LORA_DIR=models/nuextract-lora
"""
    with open(ENV_FILE, "w") as f:
        f.write(env_content)

    print(f"  Created .env with NUEXTRACT_DEVICE={device}")
    print(f"  Edit .env to change settings (e.g., add ANTHROPIC_API_KEY)")


# -------------------------------------------------------------------
# 5. Create directories
# -------------------------------------------------------------------
def create_directories():
    banner("5/7  Creating directories")

    dirs = [
        os.path.join(DATA_DIR, "inbox"),
        os.path.join(DATA_DIR, "processed"),
        os.path.join(ROOT, "models", "nuextract-lora"),
    ]

    for d in dirs:
        os.makedirs(d, exist_ok=True)
        print(f"  ✅ {os.path.relpath(d, ROOT)}/")


# -------------------------------------------------------------------
# 6. Initialize data stores
# -------------------------------------------------------------------
def init_data_stores():
    banner("6/7  Initializing data stores")

    stores = ["deals.json", "managers.json", "transactions.json", "deal_orders.json"]

    for store_file in stores:
        path = os.path.join(DATA_DIR, store_file)
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump([], f)
            print(f"  Created {store_file}")
        else:
            with open(path, "r") as f:
                data = json.load(f)
            print(f"  {store_file} exists ({len(data)} records)")


# -------------------------------------------------------------------
# 7. Register Jupyter kernel
# -------------------------------------------------------------------
def register_kernel():
    banner("7/7  Registering Jupyter kernel")
    run([
        VENV_PYTHON, "-m", "ipykernel", "install",
        "--user",
        "--name=NWInvestments",
        "--display-name=NWInvestments (.venv)"
    ])
    print("  Kernel registered. Select 'NWInvestments (.venv)' in VS Code.")


# -------------------------------------------------------------------
# Optional: Pre-download model
# -------------------------------------------------------------------
def predownload_model():
    banner("Optional: Pre-download NuExtract model")
    print("  The NuExtract-1.5 model is ~7.5GB.")
    resp = input("  Download it now? [Y/n]: ").strip().lower()
    if resp in ("", "y", "yes"):
        print("  Downloading model (this may take a few minutes)...")
        run([
            VENV_PYTHON, "-c",
            "from transformers import AutoModelForCausalLM, AutoTokenizer; "
            "name = 'numind/NuExtract-1.5'; "
            "print('Downloading tokenizer...'); AutoTokenizer.from_pretrained(name); "
            "print('Downloading model weights...'); AutoModelForCausalLM.from_pretrained(name); "
            "print('Done! Model cached locally.')"
        ])
    else:
        print("  Skipped. Model will download on first notebook run.")


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    print("""
    ╔══════════════════════════════════════╗
    ║   NWInvestments — Project Setup      ║
    ╚══════════════════════════════════════╝
    """)

    setup_venv()
    install_deps()
    device = detect_device()
    create_env_file(device)
    create_directories()
    init_data_stores()
    register_kernel()
    predownload_model()

    banner("Setup Complete!")
    print("""
  Next steps:
    1. Open extractor_gui.ipynb in VS Code
    2. Select the 'NWInvestments (.venv)' kernel
    3. Run Cell 1 to load the extractor
    4. Run Cell 2 to preview and extract the first email

  To change settings, edit .env and restart the kernel.
    """)


if __name__ == "__main__":
    main()
