"""
Fine-tune NuExtract on your corrected extractions using LoRA.
===============================================================

WHAT THIS FILE DOES:
    Takes the training data you've accumulated (email + corrected extraction
    pairs saved in data/training_examples.jsonl) and trains a small set of
    adapter weights (LoRA) that adjust the NuExtract model's behavior.

    After fine-tuning, the model gets better at extracting the specific
    fields you care about, in the format you prefer.

WHAT IS LoRA?  (Low-Rank Adaptation)
    Instead of retraining the ENTIRE model (billions of parameters, would
    take days and tons of GPU memory), LoRA only trains a SMALL adapter
    layer (~0.1% of parameters) that sits on top of the frozen base model.

    Benefits:
    - Fast: minutes instead of hours/days
    - Lightweight: adapter is ~50-100MB vs 7.5GB for the full model
    - Reversible: delete the adapter to go back to the base model
    - Stackable: you can keep fine-tuning on more data over time

    The adapter is saved to models/nuextract-lora/ and automatically
    loaded by the extractor on next startup (see extractor.py).

HOW TRAINING WORKS (step by step):
    1. Load training examples from data/training_examples.jsonl
    2. Format each example as: prompt (email + template) + completion (JSON)
    3. Load the base NuExtract model and apply LoRA adapters to it
    4. Train ONLY the adapter weights, keeping the base model frozen
    5. The loss function only considers the COMPLETION tokens (not the prompt)
       — this teaches the model to generate better extractions
    6. Save just the adapter weights to models/nuextract-lora/

HOW TO ADAPT:
    - target_modules: These are the specific layers inside the model that
      get LoRA adapters. The names below are for Phi-3 architecture
      (which NuExtract-1.5 is based on). If you switch to a different
      model, you'll need to find the right layer names.
    - lora_r: "Rank" of the adapter. Higher = more capacity but slower.
      16 is a good default. Try 8 for faster training, 32 for more accuracy.
    - num_epochs: How many times to loop through all training data.
      3 is usually enough. More isn't always better (can overfit).
    - learning_rate: How fast the model learns. 2e-4 is standard for LoRA.
      If the loss isn't going down, try 1e-4 (slower but more stable).

MINIMUM DATA REQUIRED:
    At least 2 training examples. More is better — 10+ gives good results.
    The model won't memorize your data; it learns patterns across examples.
"""

import json
import os
import sys

# Ensure project root is importable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend import config
from backend.extractor import NUEXTRACT_TEMPLATE


def load_training_data(path: str) -> list[dict]:
    """
    Load training examples from a JSONL file (one JSON per line).

    Each line has: {"email_text": "...", "extraction": {...}, "timestamp": "..."}
    """
    examples = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    return examples


def format_prompt(email_text: str) -> str:
    """
    Format an email into the NuExtract prompt format.

    This must match exactly how NuExtractExtractor.extract() builds the
    prompt, so the model learns to generate the right output format.
    """
    template = json.dumps(NUEXTRACT_TEMPLATE, indent=2)
    return f"<|input|>\n{email_text}\n<|template|>\n{template}\n<|output|>\n"


def format_completion(extraction: dict) -> str:
    """Format the corrected extraction as the target JSON output."""
    return json.dumps(extraction, indent=2)


def run_finetune(
    num_epochs: int = 3,
    batch_size: int = 1,
    learning_rate: float = 2e-4,
    lora_r: int = 16,
    lora_alpha: int = 32,
    max_seq_length: int = 2048,
):
    """
    Run LoRA fine-tuning on accumulated training examples.

    Parameters:
        num_epochs     — Number of full passes through the training data (default: 3)
        batch_size     — Examples per batch (keep at 1 for limited GPU memory)
        learning_rate  — Step size for weight updates (2e-4 is standard for LoRA)
        lora_r         — LoRA rank, controls adapter capacity (16 is a good default)
        lora_alpha     — LoRA scaling factor (usually 2x lora_r)
        max_seq_length — Max input length in tokens (2048 covers most emails)

    Returns:
        Path to the saved adapter directory, or None if training couldn't proceed.
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, Trainer
    from peft import LoraConfig, get_peft_model, TaskType

    # --- Step 1: Load and validate training data ---
    if not os.path.exists(config.TRAINING_DATA):
        print("No training data found. Process some emails with corrections first.")
        return

    examples = load_training_data(config.TRAINING_DATA)
    if len(examples) < 2:
        print(f"Only {len(examples)} training example(s). Need at least 2 to train. Keep processing emails!")
        return

    print(f"[Fine-tune] {len(examples)} training examples loaded.")

    # --- Step 2: Determine device and precision ---
    device = config.NUEXTRACT_DEVICE
    if device == "cuda" and not torch.cuda.is_available():
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    elif device == "mps" and not torch.backends.mps.is_available():
        device = "cpu"

    # MPS and CPU need float32 for training stability; CUDA can use bfloat16
    dtype = torch.float32 if device in ("cpu", "mps") else torch.bfloat16

    # --- Step 3: Load the base model and tokenizer ---
    print(f"[Fine-tune] Loading base model {config.NUEXTRACT_MODEL} on {device} ({dtype})...")
    tokenizer = AutoTokenizer.from_pretrained(config.NUEXTRACT_MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token  # Required for batched training

    model = AutoModelForCausalLM.from_pretrained(
        config.NUEXTRACT_MODEL,
        torch_dtype=dtype,
    )

    # --- Step 4: Configure LoRA adapters ---
    # target_modules: These are the layer names inside Phi-3 (NuExtract's base).
    # qkv_proj  = query/key/value attention projections (how the model "looks at" the input)
    # o_proj    = output projection (how attention results are combined)
    # gate_up_proj / down_proj = feed-forward layers (how the model "thinks")
    #
    # If you use a DIFFERENT base model, you'll need to find its layer names.
    # Run: print([n for n, _ in model.named_modules()]) to see all layer names.
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,       # We're training a text generation model
        r=lora_r,                            # Rank: higher = more capacity, more memory
        lora_alpha=lora_alpha,               # Scaling factor: usually 2x the rank
        lora_dropout=0.05,                   # Small dropout to prevent overfitting
        target_modules=["qkv_proj", "o_proj", "gate_up_proj", "down_proj"],
        bias="none",                         # Don't train bias terms (saves memory)
    )

    print("[Fine-tune] Applying LoRA adapters...")
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()  # Shows something like "trainable: 0.1% of parameters"

    # --- Step 5: Prepare training data ---
    # For each example, we create:
    #   - input_ids: the full sequence (prompt + correct answer)
    #   - attention_mask: which tokens are real (1) vs padding (0)
    #   - labels: same as input_ids, BUT with -100 for prompt tokens and padding
    #             (-100 tells PyTorch "don't compute loss for this token")
    #
    # This way, the model only learns to produce the ANSWER, not to repeat the prompt.
    print("[Fine-tune] Tokenizing training data...")

    train_inputs = []
    for ex in examples:
        prompt = format_prompt(ex["email_text"])
        completion = format_completion(ex["extraction"])
        full_text = prompt + completion + tokenizer.eos_token

        encoded = tokenizer(
            full_text,
            truncation=True,
            max_length=max_seq_length,
            padding="max_length",
            return_tensors="pt",
        )

        input_ids = encoded["input_ids"].squeeze()
        attention_mask = encoded["attention_mask"].squeeze()

        # Mask the prompt tokens in labels — only train on the completion (answer)
        prompt_tokens = tokenizer(prompt, return_tensors="pt")["input_ids"].shape[1]
        labels = input_ids.clone()
        labels[:prompt_tokens] = -100   # Don't compute loss on the prompt
        labels[attention_mask == 0] = -100  # Don't compute loss on padding

        train_inputs.append({
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "labels": labels,
        })

    # Simple PyTorch dataset wrapper
    class SimpleDataset(torch.utils.data.Dataset):
        def __init__(self, data):
            self.data = data
        def __len__(self):
            return len(self.data)
        def __getitem__(self, idx):
            return self.data[idx]

    dataset = SimpleDataset(train_inputs)

    # --- Step 6: Configure training ---
    output_dir = config.LORA_ADAPTER_DIR
    os.makedirs(output_dir, exist_ok=True)

    use_mps = device == "mps"
    training_args = TrainingArguments(
        output_dir=output_dir,
        num_train_epochs=num_epochs,
        per_device_train_batch_size=batch_size,
        gradient_accumulation_steps=4,      # Simulate larger batch size (4 x batch_size)
        learning_rate=learning_rate,
        weight_decay=0.01,               # Slight L2 regularization (prevents overfitting)
        warmup_steps=2,                   # Gradually increase LR for first 2 steps (stability)
        logging_steps=1,                  # Print loss every step so you can watch progress
        save_strategy="epoch",            # Save a checkpoint after each epoch
        fp16=False,                       # Don't use float16 (not well-supported on MPS)
        bf16=False,                       # Don't use bfloat16 (same reason)
        use_mps_device=use_mps,           # Tell HuggingFace to use Apple GPU if available
        optim="adamw_torch",              # Standard optimizer (works on all devices)
        report_to="none",                 # Don't send metrics to wandb/tensorboard
        dataloader_pin_memory=False,      # Required for MPS compatibility
    )

    # --- Step 7: Train! ---
    print(f"[Fine-tune] Training for {num_epochs} epochs...")
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
    )

    trainer.train()
    # You'll see the loss printed every step. It should generally decrease.
    # If loss stays high or increases, try lowering learning_rate to 1e-4.

    # --- Step 8: Save the adapter ---
    # We save ONLY the adapter weights (not the full model). This is just
    # ~50-100MB. The extractor will load the base model + this adapter.
    print(f"[Fine-tune] Saving LoRA adapter to {output_dir}...")
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)  # Needed so the adapter can reload properly

    print(f"[Fine-tune] Done! Adapter saved. Restart the extractor to use the fine-tuned model.")
    return output_dir


# --- Can be run directly from command line: python -m backend.finetune ---
if __name__ == "__main__":
    run_finetune()
