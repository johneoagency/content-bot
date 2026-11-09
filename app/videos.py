"""Video intake and processing via ffmpeg/ffprobe.

Rules:
  * Stream downloads to disk, never whole videos into RAM (download handled by
    the Telegram/Apify layer; here we only probe and transform files on disk).
  * Probe with ffprobe; if longer than the trim limit, keep the first N seconds
    and tell the user. If shorter than the model's accepted minimum, explain the
    error before charging.
  * Extract the first frame for the Qwen preview; keep the video's aspect ratio.
  * Verify the finished file's duration, streams and decodability before sending.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class VideoError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    path: str
    duration: float
    width: int
    height: int
    has_audio: bool
    vcodec: str
    acodec: str | None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as e:
        raise VideoError(f"Required tool not found: {cmd[0]}") from e
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip().splitlines()
        tail = stderr[-1] if stderr else "unknown error"
        raise VideoError(f"{cmd[0]} failed: {tail[:200]}") from e


def probe(path: str | Path) -> VideoInfo:
    path = Path(path)
    cp = _run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    data = json.loads(cp.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        raise VideoError("No video stream found in file")
    duration = float(data.get("format", {}).get("duration", 0) or v.get("duration", 0) or 0)
    return VideoInfo(
        path=str(path),
        duration=duration,
        width=int(v.get("width", 0)),
        height=int(v.get("height", 0)),
        has_audio=a is not None,
        vcodec=v.get("codec_name", ""),
        acodec=a.get("codec_name") if a else None,
    )


def verify_decodable(path: str | Path) -> bool:
    """Fully decode to null to confirm the file is not truncated/corrupt."""
    try:
        _run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"])
        return True
    except VideoError:
        return False


def trim(src: str | Path, dest: str | Path, seconds: float) -> VideoInfo:
    """Keep the first `seconds`. Re-encodes for a clean, seekable cut."""
    _run([
        "ffmpeg", "-y", "-i", str(src), "-t", f"{seconds:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-movflags", "+faststart", str(dest),
    ])
    return probe(dest)


def ensure_trimmed(src: str | Path, dest: str | Path, limit_seconds: float) -> tuple[VideoInfo, bool]:
    """If longer than the limit, trim and return (info, trimmed=True). Otherwise
    copy through unchanged and return (info, False)."""
    info = probe(src)
    if info.duration > limit_seconds + 0.05:
        return trim(src, dest, limit_seconds), True
    # normalize container without cutting
    _run([
        "ffmpeg", "-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dest),
    ])
    return probe(dest), False


def first_frame(src: str | Path, dest_png: str | Path) -> str:
    _run([
        "ffmpeg", "-y", "-i", str(src), "-vf", "select=eq(n\\,0)",
        "-vframes", "1", str(dest_png),
    ])
    return str(dest_png)


def sample_frames(src: str | Path, out_dir: str | Path, count: int = 3) -> list[str]:
    """Grab evenly spaced frames (start/middle/end) for caption vision input."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    duration = max(info.duration, 0.1)
    timestamps = [duration * f for f in (0.05, 0.5, 0.95)][:count]
    paths: list[str] = []
    for i, ts in enumerate(timestamps):
        out = out_dir / f"frame_{i}.png"
        _run([
            "ffmpeg", "-y", "-ss", f"{ts:.3f}", "-i", str(src),
            "-vframes", "1", str(out),
        ])
        paths.append(str(out))
    return paths
