"""Entrypoint: `python -m app`.

Loads config + secrets, prints the activation checklist if anything blocks real
operation, and (unless --check) starts the single long-polling consumer and the
worker loops.
"""
from __future__ import annotations

import argparse
import signal
import sys

from .config import DATA_DIR, load_config
from .queue import Orchestrator
from .storage import Storage


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="content-bot")
    ap.add_argument("--config", default=None, help="Path to config.json")
    ap.add_argument("--check", action="store_true",
                    help="Validate config/secrets and exit without connecting.")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    missing = cfg.missing_activation_items()
    if missing:
        print("content-bot: ready code, activation pending. Missing:")
        for item in missing:
            print(f"  - {item}")
        print("Fill config.json + secrets/.env, then re-run. See README.md.")
        if args.check or any(
            m in missing for m in ("TELEGRAM_BOT_TOKEN", "WAVESPEED_API_KEY", "group_chat_id")
        ):
            return 1 if args.check else 2

    if args.check:
        print("content-bot: configuration OK.")
        return 0

    db = Storage(DATA_DIR / "content_bot.db")
    orch = Orchestrator(cfg, db)

    def _shutdown(signum, frame):  # graceful shutdown
        print("content-bot: shutting down…", flush=True)
        orch.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        orch.run()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
