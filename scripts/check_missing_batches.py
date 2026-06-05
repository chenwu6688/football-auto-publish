#!/usr/bin/env python3
"""Check which batches are missing today for the heartbeat workflow."""
import json, os, sys
from pathlib import Path

today = os.environ.get("TODAY", "")
meta_path = Path("output") / today / "metadata.json"

if not meta_path.exists():
    # All missing
    print("morning noon evening")
    sys.exit(0)

meta = json.loads(meta_path.read_text())
expected = {"morning", "noon", "evening"}
completed = set(meta.get("batches_completed", []))

# 'full' (auto mode) counts as all three
if "full" in completed:
    print("")
    sys.exit(0)

missing = expected - completed
print(" ".join(sorted(missing)))
