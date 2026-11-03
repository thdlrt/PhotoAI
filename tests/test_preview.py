from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from pathlib import Path

from PIL import Image

from landscape_culler.preview import _load_raw_thumbnail


def test_corrupted_cached_jpeg_is_repaired_from_source(tmp_path):
    from landscape_culler.preview import load_preview, preview_cache_path
    source = tmp_path / "original.png"
    Image.new("RGB", (120, 80), "green").save(source)
    before = source.read_bytes()
    cached = preview_cache_path(source, tmp_path / "cache")
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"truncated jpeg")
    with load_preview(source, tmp_path / "cache") as image:
        assert image.size == (120, 80)
    with Image.open(cached) as image:
        image.load()
    assert source.read_bytes() == before


def test_failed_preview_publish_preserves_previous_cache(tmp_path, monkeypatch):
    import pytest
    from landscape_culler import preview
    source = tmp_path / "original.png"
    Image.new("RGB", (120, 80)).save(source)
    cached = preview.preview_cache_path(source, tmp_path / "cache")
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"old broken cache")
    def full_disk(*args): raise OSError("disk full")
    monkeypatch.setattr(preview, "atomic_write_bytes", full_disk)
    with pytest.raises(OSError, match="disk full"):
        preview.load_preview(source, tmp_path / "cache")
    assert cached.read_bytes() == b"old broken cache"


def test_raw_embedded_jpeg_respects_exif_orientation(monkeypatch) -> None:
    source = Image.new("RGB", (80, 40), "green")
    exif = Image.Exif()
    exif[274] = 8  # Rotate 270 degrees clockwise for display.
    encoded = io.BytesIO()
    source.save(encoded, "JPEG", exif=exif)

    jpeg_marker = object()

    class FakeRaw:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def extract_thumb(self):
            return SimpleNamespace(format=jpeg_marker, data=encoded.getvalue())

    fake_rawpy = SimpleNamespace(
        imread=lambda _path: FakeRaw(),
        ThumbFormat=SimpleNamespace(JPEG=jpeg_marker),
        LibRawNoThumbnailError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "rawpy", fake_rawpy)

    preview = _load_raw_thumbnail(Path("portrait.ARW"))

    assert preview.size == (40, 80)
