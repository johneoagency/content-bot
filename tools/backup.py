#!/usr/bin/env python3
"""Consistent SQLite backup using the online backup API, with retention for
completed media only. Run from cron or a sidecar.

Usage: python tools/backup.py [--keep 14]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATA_DIR  # noqa: E402
from app.storage import Storage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=14, help="backup files to retain")
    args = ap.parse_args()

    backups = DATA_DIR / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = backups / f"content_bot-{stamp}.db"

    db = Storage(DATA_DIR / "content_bot.db")
    try:
        db.backup(dest)
    finally:
        db.close()
    print(f"backup written: {dest}")

    # Verify the backup opens (restore smoke test).
    check = Storage(dest)
    try:
        counts = check.counts_by_state()
        print(f"restore check OK: {sum(counts.values())} jobs")
    finally:
        check.close()

    # Retention.
    files = sorted(backups.glob("content_bot-*.db"))
    for old in files[: max(0, len(files) - args.keep)]:
        old.unlink(missing_ok=True)
        print(f"pruned old backup: {old.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
