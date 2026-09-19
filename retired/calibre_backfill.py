#!/usr/bin/env python3
"""Retired: backfill AO3 statistics into an existing Calibre library. Run with --help; see docs/BACKFILL.md."""

from pathlib import Path
import sys

# A script in a subfolder does not have the repository root on its import path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ao3archiver.backfill import run  # noqa: E402

if __name__ == "__main__":
    run()
