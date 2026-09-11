from __future__ import annotations

import io
import hashlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .constants import PREVIEW_VERSION, READABLE_RAW_EXTENSIONS
from .util import cache_key


def _load_raw_thumbnail(path: Path) -> Image.Image:
    import rawpy

    with rawpy.imread(str(path)) as raw:
        try:
            thumb = raw.extract_thumb()
        except rawpy.LibRawNoThumbnailError:
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False, output_bps=8)
            return Image.fromarray(rgb).convert("RGB")
    if thumb.format == rawpy.ThumbFormat.JPEG:
        # Sony and other cameras commonly store a landscape-shaped embedded
        # JPEG plus EXIF Orientation.  Converting to RGB first discards that
        # metadata and makes portrait photographs enter DINO sideways.
        with Image.open(io.BytesIO(thumb.data)) as source:
            return ImageOps.exif_transpose(source).convert("RGB")
    return Image.fromarray(np.asarray(thumb.data)).convert("RGB")


def _preview_key(path: Path) -> str:
    payload = f"{PREVIEW_VERSION}|{cache_key(path)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_preview(path: Path, cache_dir: Path, max_size: int = 1280) -> Image.Image:
    key = _preview_key(path)
    cached = cache_dir / key[:2] / f"{key}.jpg"
    if cached.exists():
        with Image.open(cached) as image:
            return image.convert("RGB")

    if path.suffix.lower() in READABLE_RAW_EXTENSIONS:
        image = _load_raw_thumbnail(path)
    else:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    cached.parent.mkdir(parents=True, exist_ok=True)
    image.save(cached, "JPEG", quality=88, optimize=True)
    return image


def preview_cache_path(path: Path, cache_dir: Path) -> Path:
    key = _preview_key(path)
    return cache_dir / key[:2] / f"{key}.jpg"
