#!/usr/bin/env python3
"""Validate credentials WITHOUT any paid generation.

Checks: Telegram getMe + group/admin/topics presence, WaveSpeed auth, and Apify
actor reachability (if a token is set). Prints a readiness summary. No image or
video job is submitted, so nothing is charged.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.providers.apify import ApifyClient  # noqa: E402
from app.providers.wavespeed import WaveSpeedClient  # noqa: E402
from app.telegram import TelegramClient, TelegramError  # noqa: E402


def main() -> int:
    cfg = load_config()
    ok = True

    # Telegram
    try:
        tg = TelegramClient(cfg.secrets.telegram_bot_token,
                            base_url=cfg.get("runtime", "local_bot_api_base_url"))
        me = tg.get_me()
        print(f"[ok] Telegram getMe: @{me.get('username')} (id {me.get('id')})")
        if cfg.group_chat_id:
            chat = tg.get_chat(cfg.group_chat_id)
            print(f"[ok] Group: {chat.get('title')!r} type={chat.get('type')}")
            for admin in cfg.admin_user_ids:
                m = tg.get_chat_member(cfg.group_chat_id, admin)
                print(f"     admin {admin}: status={m.get('status')}")
        else:
            print("[--] group_chat_id not set")
    except TelegramError as e:
        ok = False
        print(f"[FAIL] Telegram: {e}")

    # Topics
    missing_topics = [k for k, v in cfg.topics.items() if v in (None, 0)]
    if missing_topics:
        print(f"[--] topics not set: {', '.join(missing_topics)}")
    else:
        print("[ok] all six topic ids set")

    # WaveSpeed (auth only, no paid call)
    if cfg.secrets.wavespeed_api_key:
        try:
            ws = WaveSpeedClient(cfg.secrets.wavespeed_api_key,
                                 base_url=cfg.get("providers", "wavespeed_base_url",
                                                  default="https://api.wavespeed.ai"))
            print("[ok] WaveSpeed key accepted" if ws.validate() else "[FAIL] WaveSpeed key rejected")
        except Exception as e:
            print(f"[FAIL] WaveSpeed: {e}")
    else:
        ok = False
        print("[FAIL] WAVESPEED_API_KEY not set")

    # Apify (optional)
    if cfg.secrets.has_apify:
        try:
            ap = ApifyClient(cfg.secrets.apify_token,
                             cfg.get("providers", "apify_actor",
                                     default="intropix/instagram-posts-reels-scraper"))
            print("[ok] Apify actor reachable" if ap.validate() else "[warn] Apify actor not reachable")
        except Exception as e:
            print(f"[warn] Apify: {e}")
    else:
        print("[--] Apify not configured (Instagram URL intake disabled; uploads still work)")

    # Reference
    ref = cfg.get("reference", "image_path")
    if ref and Path(ref).exists() and cfg.get("reference", "usage_rights_confirmed"):
        print("[ok] reference image set and rights confirmed")
    else:
        print("[--] reference image not set / rights not confirmed")

    print("\nSummary:", "core credentials OK" if ok else "missing core credentials")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
