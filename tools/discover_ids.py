#!/usr/bin/env python3
"""Discover and verify Telegram IDs instead of guessing them.

Usage:
  python tools/discover_ids.py            # long-poll and print every chat/topic/
                                          # user id it sees, so you can copy the
                                          # real values into config.json.

Add the bot to your group as admin, open each forum topic and send one message
in it, then watch the output here. Press Ctrl-C to stop. Nothing is written to
config.json automatically; you copy the verified ids yourself.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_secrets  # noqa: E402
from app.telegram import TelegramClient  # noqa: E402


def main() -> int:
    secrets = load_secrets()
    if not secrets.telegram_bot_token:
        print("Set TELEGRAM_BOT_TOKEN in secrets/.env first.")
        return 1
    tg = TelegramClient(secrets.telegram_bot_token)
    me = tg.get_me()
    print(f"Bot: @{me.get('username')} (id {me.get('id')})")
    print("Listening. Post a message in each topic of your group. Ctrl-C to stop.\n")
    offset = 0
    seen_topics: dict[int, str] = {}
    try:
        while True:
            for upd in tg.get_updates(offset, timeout=30):
                offset = max(offset, upd["update_id"] + 1)
                msg = upd.get("message") or upd.get("channel_post")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                user = msg.get("from", {})
                thread = msg.get("message_thread_id")
                topic_name = (msg.get("forum_topic_created") or {}).get("name")
                if thread and topic_name:
                    seen_topics[thread] = topic_name
                print(
                    f"chat_id={chat.get('id')} type={chat.get('type')} "
                    f"title={chat.get('title')!r} thread_id={thread} "
                    f"topic={seen_topics.get(thread, '?')} "
                    f"from_user_id={user.get('id')} ({user.get('username')})"
                )
    except KeyboardInterrupt:
        print("\n--- Verified topics seen ---")
        for tid, name in seen_topics.items():
            print(f"  {name}: thread_id={tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
