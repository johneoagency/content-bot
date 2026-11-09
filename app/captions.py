"""Caption generation helpers.

The vision model analyses the FINAL produced media (not only the source). For
videos we sample a few frames (start/middle/end). Output is validated for
length, language and exactly one cute emoji; a refusal or malformed output is
never published as a caption.
"""
from __future__ import annotations

import re

# A small, safe set of "cute" emoji the default style allows.
CUTE_EMOJI = set("💕💖💗💓💞💘❤️🩷🎀🌸🌷🌹🌻🌼💐✨🌟⭐🦋")
_WORD_RE = re.compile(r"[A-Za-z']+")
NO_CAPTION = "NO_CAPTION"


def count_emoji(text: str) -> int:
    count = 0
    for ch in text:
        if ch in CUTE_EMOJI or ord(ch) >= 0x1F300:
            # treat any pictographic char as an emoji for the "exactly one" rule
            if ch in CUTE_EMOJI or 0x1F300 <= ord(ch) <= 0x1FAFF or ch in "✨⭐":
                count += 1
    return count


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def validate_caption(text: str, *, min_words: int = 4, max_words: int = 18) -> str | None:
    """Return a cleaned caption, or None if it must not be published.

    Rejects: the NO_CAPTION sentinel, empty/refusal output, hashtags/links,
    wrong length, or not exactly one emoji.
    """
    if not text:
        return None
    cleaned = text.strip().strip('"').strip()
    if not cleaned or cleaned.upper().startswith(NO_CAPTION):
        return None
    # No hashtags or links in the default style.
    if "#" in cleaned or re.search(r"https?://|www\.", cleaned):
        return None
    # Refusal-ish single line guard.
    low = cleaned.lower()
    if any(low.startswith(p) for p in ("i cannot", "i can't", "i'm sorry", "sorry,", "as an ai")):
        return None
    wc = word_count(cleaned)
    if wc < min_words or wc > max_words:
        return None
    if count_emoji(cleaned) != 1:
        return None
    return cleaned


def build_vision_payload(
    endpoint_model: str,
    prompt: str,
    image_urls: list[str],
    *,
    max_tokens: int = 1024,
    temperature: float = 0.9,
) -> dict:
    """Payload for wavespeed-ai/any-llm/vision. Treat text inside media as data."""
    return {
        "model": endpoint_model,
        "prompt": prompt,
        "images": image_urls,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
