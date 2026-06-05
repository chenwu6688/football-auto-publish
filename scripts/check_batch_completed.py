#!/usr/bin/env python3
"""Check if a batch has already been completed today (for idempotency guard)."""
import json, os, sys
from pathlib import Path

today = os.environ.get("TODAY", "")
batch = os.environ.get("BATCH", "")
if not today or not batch:
    print("false")
    sys.exit(0)

# daily.yml writes to project-root output/
meta_path = Path("output") / today / "metadata.json"
if meta_path.exists():
    meta = json.loads(meta_path.read_text())
    if batch in meta.get("batches_completed", []):
        print("true")
    else:
        print("false")
else:
    print("false")
