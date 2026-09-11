from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from pathlib import Path

from PIL import Image

from landscape_culler.preview import _load_raw_thumbnail


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
