"""Image intake and geometry.

"Size is a code rule": the desired aspect must not be enforced through the
prompt alone. The final file is checked in pixels before delivery. We store the
true width/height AFTER applying EXIF orientation and BEFORE any provider resize,
and the delivered file must match the received file's dimensions unless the owner
explicitly chose a per-job override.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

# Guard against decompression bombs; tightened again from config at runtime.
Image.MAX_IMAGE_PIXELS = 50_000_000

ACCEPTED_FORMATS = {"JPEG", "PNG", "WEBP"}


class ImageError(RuntimeError):
    pass


@dataclass
class ImageInfo:
    path: str
    fmt: str
    width: int           # after EXIF orientation
    height: int
    sha256: str
    bytes: int


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect(path: str | Path, *, max_pixels: int | None = None) -> ImageInfo:
    """Open, verify the real format, apply EXIF orientation, record true size."""
    path = Path(path)
    if max_pixels:
        Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
            if fmt not in ACCEPTED_FORMATS:
                raise ImageError(f"Unsupported image format: {fmt or 'unknown'}")
            im = ImageOps.exif_transpose(im)  # bake orientation into pixels
            width, height = im.size
    except Image.DecompressionBombError as e:
        raise ImageError("Image exceeds the maximum allowed pixel size") from e
    except ImageError:
        raise
    except Exception as e:  # pragma: no cover - PIL raises many types
        raise ImageError(f"Cannot read image: {e}") from e
    return ImageInfo(
        path=str(path),
        fmt=fmt,
        width=width,
        height=height,
        sha256=file_sha256(path),
        bytes=path.stat().st_size,
    )


def normalize_to_png(src: str | Path, dest: str | Path) -> str:
    """Write an EXIF-baked PNG copy (what we send to the provider as the base)."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        im.save(dest, format="PNG")
    return str(dest)


def pad_to_size(
    src: str | Path,
    dest: str | Path,
    target_w: int,
    target_h: int,
    fill=(0, 0, 0, 0),
) -> dict[str, int]:
    """Proportional fit + padding to reach an exact canvas without distortion or
    silent cropping. Returns the padding box so the artificial margin can be
    removed again after generation."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGBA")
        sw, sh = im.size
        scale = min(target_w / sw, target_h / sh)
        new_w, new_h = max(1, round(sw * scale)), max(1, round(sh * scale))
        resized = im.resize((new_w, new_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (target_w, target_h), fill)
        off_x = (target_w - new_w) // 2
        off_y = (target_h - new_h) // 2
        canvas.paste(resized, (off_x, off_y))
        canvas.save(dest, format="PNG")
    return {"off_x": off_x, "off_y": off_y, "inner_w": new_w, "inner_h": new_h}


def crop_padding(src: str | Path, dest: str | Path, box: dict[str, int]) -> str:
    """Undo pad_to_size: keep only the real content region."""
    with Image.open(src) as im:
        left, top = box["off_x"], box["off_y"]
        right, bottom = left + box["inner_w"], top + box["inner_h"]
        im.crop((left, top, right, bottom)).save(dest, format="PNG")
    return str(dest)


def resize_to_exact(src: str | Path, dest: str | Path, target_w: int, target_h: int) -> str:
    """Final geometry enforcement: make the output exactly the target size. Only
    use when aspect already matches (same aspect => proportional); otherwise use
    pad/crop so nothing is distorted."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.size != (target_w, target_h):
            im = im.resize((target_w, target_h), Image.LANCZOS)
        im.save(dest, format="PNG")
    return str(dest)


def same_aspect(w1: int, h1: int, w2: int, h2: int, tol: float = 0.01) -> bool:
    if min(h1, h2) == 0:
        return False
    return abs((w1 / h1) - (w2 / h2)) <= tol


def enforce_output_geometry(
    produced_path: str | Path,
    dest_path: str | Path,
    target_w: int,
    target_h: int,
    pad_box: dict[str, int] | None = None,
) -> ImageInfo:
    """After generation: remove any artificial padding, then make the final file
    match the target dimensions exactly. Returns info on the final PNG."""
    tmp = Path(dest_path).with_suffix(".stage.png")
    if pad_box:
        crop_padding(produced_path, tmp, pad_box)
        resize_to_exact(tmp, dest_path, target_w, target_h)
        tmp.unlink(missing_ok=True)
    else:
        resize_to_exact(produced_path, dest_path, target_w, target_h)
    return inspect(dest_path)
