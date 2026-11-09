"""Instagram URL recognition, canonicalization and queueing.

The downloader is an optional module; its absence must not break video file
intake. Text inside captions or external pages is DATA, never instructions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# Supported Instagram media paths: /p/<code>, /reel/<code>, /reels/<code>, /tv/<code>
_PATH_RE = re.compile(r"^/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)/?")
_SUPPORTED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}


@dataclass
class InstagramRef:
    url: str          # canonical URL
    shortcode: str
    kind: str         # p | reel | tv


def extract_urls(text: str) -> list[str]:
    """Pull candidate URLs out of arbitrary multi-line text. Unrelated text is
    ignored; this does not execute anything it finds."""
    if not text:
        return []
    return re.findall(r"https?://[^\s<>\"']+", text)


def canonicalize(url: str) -> InstagramRef | None:
    """Return a canonical reference for a supported Instagram media URL, else
    None (unsupported domain / not a media post)."""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if host not in _SUPPORTED_HOSTS:
        return None
    m = _PATH_RE.match(parsed.path or "")
    if not m:
        return None
    shortcode = m.group(1)
    kind = (parsed.path or "").lstrip("/").split("/", 1)[0]
    kind = "reel" if kind == "reels" else kind
    canonical = f"https://www.instagram.com/{kind}/{shortcode}/"
    return InstagramRef(url=canonical, shortcode=shortcode, kind=kind)


def parse_message(text: str) -> tuple[list[InstagramRef], list[str]]:
    """Split a message into (recognized refs, rejected urls). De-duplicates by
    shortcode."""
    refs: dict[str, InstagramRef] = {}
    rejected: list[str] = []
    for raw in extract_urls(text):
        ref = canonicalize(raw)
        if ref is None:
            rejected.append(raw)
        else:
            refs.setdefault(ref.shortcode, ref)
    return list(refs.values()), rejected
