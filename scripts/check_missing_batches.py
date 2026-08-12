#!/usr/bin/env python3
"""Check which batches are missing today for the heartbeat workflow.

The set of batches that are *expected* to be done depends on when the check
runs:
  - 午检 (midday, before ~15:00 CST): only `morning` must be done.
    `noon` may still be running and `evening` has not started yet, so they
    must NOT be treated as missing.
  - 终检 (final, ~22:17 CST): all three batches should be done.

Pass the expected set via the EXPECTED env var (space-separated). Defaults to
all three for backwards compatibility.
"""
import json, os, sys
from pathlib import Path

today = os.environ.get("TODAY", "")
expected = os.environ.get("EXPECTED", "morning noon evening")
expected = set(expected.split()) if expected.strip() else set()

meta_path = Path("output") / today / "metadata.json"

if not meta_path.exists():
    # All expected batches are missing
    print(" ".join(sorted(expected)))
    sys.exit(0)

meta = json.loads(meta_path.read_text())
completed = set(meta.get("batches_completed", []))

# 'full' (auto mode) counts as all three
if "full" in completed:
    print("")
    sys.exit(0)

missing = expected - completed
print(" ".join(sorted(missing)))
