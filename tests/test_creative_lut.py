from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageCms

from landscape_culler.creative_lut import (
    ORDINARY_XMP_LUT_COMPATIBLE,
    ORDINARY_XMP_LUT_LIMITATION,
    CreativeLutEngine,
    CreativeLutError,
    InvalidCubeLutError,
    inspect_cube_lut,
)

INVERT_1D = """# Display-referred sRGB creative look
TITLE "Invert test"
LUT_1D_SIZE 2
DOMAIN_MIN 0.0 0.0 0.0
DOMAIN_MAX 1.0 1.0 1.0
1.0 1.0 1.0
0.0 0.0 0.0
"""

IDENTITY_3D = """TITLE "Identity 3D"
LUT_3D_SIZE 2
DOMAIN_MIN 0.0 0.0 0.0
DOMAIN_MAX 1.0 1.0 1.0
0.0 0.0 0.0
1.0 0.0 0.0
0.0 1.0 0.0
1.0 1.0 0.0
0.0 0.0 1.0
1.0 0.0 1.0
0.0 1.0 1.0
1.0 1.0 1.0
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _engine(tmp_path: Path) -> CreativeLutEngine:
    return CreativeLutEngine(tmp_path / "runtime", enforce_e_drive=False)


def _jpeg(path: Path) -> Path:
    y, x = np.mgrid[0:32, 0:48]
    pixels = (
        np.stack(
            (
                32 + x * 4,
                48 + y * 5,
                210 - x * 2,
            ),
            axis=-1,
        )
        .clip(0, 255)
        .astype(np.uint8)
    )
    image = Image.fromarray(pixels, mode="RGB")
    exif = Image.Exif()
    exif[315] = "Photo AI test"
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    image.save(path, "JPEG", quality=96, exif=exif, icc_profile=icc, dpi=(240, 240))
    return path


def _pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.int16)


def test_inspect_cube_supports_common_1d_and_3d(tmp_path: Path) -> None:
    one_d = inspect_cube_lut(_write(tmp_path / "one.cube", INVERT_1D))
    three_d = inspect_cube_lut(_write(tmp_path / "three.CUBE", IDENTITY_3D))

    assert one_d.kind == "1d"
    assert one_d.size_1d == 2
    assert one_d.data_rows == 2
    assert three_d.kind == "3d"
    assert three_d.size_3d == 2
    assert three_d.data_rows == 8


def test_import_indexes_immutable_hash_resource_and_xmp_boundary(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    source = _write(tmp_path / "look.cube", INVERT_1D)

    descriptor = engine.import_lut(
        source,
        source_label="test library",
        source_license="MIT",
        source_url="https://example.invalid/lut",
    )
    duplicate = engine.import_lut(source)

    assert duplicate == descriptor
    assert descriptor.lut_id.startswith("cube-")
    assert descriptor.lut_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert descriptor.xmp_compatible is False
    assert ORDINARY_XMP_LUT_COMPATIBLE is False
    assert "不能" in ORDINARY_XMP_LUT_LIMITATION
    resource = engine.library_root / descriptor.resource_file
    assert resource.is_relative_to(engine.runtime_root)
    assert engine.cache_root.is_relative_to(engine.runtime_root)
    assert resource.read_bytes() == source.read_bytes()
    assert engine.get_lut(descriptor.lut_id) == descriptor


def test_directory_import_reports_bad_lut_without_losing_good_entries(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "incoming"
    source_root.mkdir()
    _write(source_root / "valid.cube", IDENTITY_3D)
    _write(source_root / "broken.cube", "LUT_3D_SIZE 2\n0 0 0\n")

    report = _engine(tmp_path).import_directory(source_root)

    assert len(report.imported) == 1
    assert report.imported[0].kind == "3d"
    assert len(report.failures) == 1
    assert report.failures[0].source_path.endswith("broken.cube")
    assert "数据行数" in report.failures[0].error


def test_render_real_pixels_strength_cache_and_metadata(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    descriptor = engine.import_lut(_write(tmp_path / "invert.cube", INVERT_1D))
    source = _jpeg(tmp_path / "base-srgb.jpg")

    zero = engine.render_jpeg(source, lut_id=descriptor.lut_id, strength=0)
    full = engine.render_jpeg(source, lut_id=descriptor.lut_id, strength=100)
    extra = engine.render_jpeg(source, lut_id=descriptor.lut_id, strength=150)
    cached = engine.render_jpeg(source, lut_id=descriptor.lut_id, strength=100)

    assert zero.cache_path.read_bytes() == source.read_bytes()
    assert zero.cache_key != full.cache_key != extra.cache_key
    assert cached.cache_hit is True
    assert cached.cache_key == full.cache_key
    assert full.lut_id == descriptor.lut_id
    assert full.lut_hash == descriptor.lut_hash
    assert full.strength == 100
    assert full.xmp_compatible is False
    assert {"EXIF", "ICC", "DPI"}.issubset(full.metadata_preserved)

    natural_pixels = _pixels(zero.output_path)
    full_pixels = _pixels(full.output_path)
    extra_pixels = _pixels(extra.output_path)
    assert np.mean(np.abs(full_pixels - natural_pixels)) > 30
    assert np.mean(np.abs(extra_pixels - full_pixels)) > 10
    assert extra_pixels.min() >= 0
    assert extra_pixels.max() <= 255

    with Image.open(full.output_path) as rendered:
        assert rendered.getexif()[315] == "Photo AI test"
        assert rendered.info.get("icc_profile")
        assert rendered.info.get("dpi") == pytest.approx((240, 240), abs=1)


def test_output_copy_does_not_overwrite_by_default(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    descriptor = engine.import_lut(_write(tmp_path / "invert.cube", INVERT_1D))
    source = _jpeg(tmp_path / "base.jpg")
    destination = tmp_path / "finished.jpg"

    first = engine.render_jpeg(
        source,
        lut_id=descriptor.lut_id,
        strength=100,
        output_path=destination,
    )
    assert first.output_path == destination.resolve()
    destination.write_bytes(b"user file")
    with pytest.raises(FileExistsError):
        engine.render_jpeg(
            source,
            lut_id=descriptor.lut_id,
            strength=150,
            output_path=destination,
        )


@pytest.mark.parametrize("strength", [-1, 201, float("nan"), float("inf")])
def test_strength_outside_zero_to_two_hundred_is_rejected(
    tmp_path: Path, strength: float
) -> None:
    engine = _engine(tmp_path)
    descriptor = engine.import_lut(_write(tmp_path / "invert.cube", INVERT_1D))
    with pytest.raises(ValueError, match="0–200"):
        engine.render_jpeg(
            _jpeg(tmp_path / "base.jpg"),
            lut_id=descriptor.lut_id,
            strength=strength,
        )


def test_delete_lut_requires_exact_hash_and_archives_resource(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    descriptor = engine.import_lut(_write(tmp_path / "invert.cube", INVERT_1D))

    with pytest.raises(CreativeLutError, match="已变化"):
        engine.delete_lut(
            descriptor.lut_id,
            expected_hash="0" * 64,
            archive_root=tmp_path / "archive",
        )
    assert engine.get_lut(descriptor.lut_id).lut_hash == descriptor.lut_hash

    removed = engine.delete_lut(
        descriptor.lut_id,
        expected_hash=descriptor.lut_hash,
        archive_root=tmp_path / "archive",
    )

    assert removed == descriptor
    assert engine.list_luts() == []
    archives = list((tmp_path / "archive").glob("*/removed.json"))
    assert len(archives) == 1
    assert (archives[0].parent / f"{descriptor.lut_hash}.cube").is_file()


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "TITLE bad\n0 0 0\n",
        "LUT_1D_SIZE 3\n0 0 0\n1 1 1\n",
        "LUT_3D_SIZE 999\n",
        "LUT_1D_SIZE 2\n0 0 NaN\n1 1 1\n",
    ],
)
def test_bad_lut_is_rejected(tmp_path: Path, contents: str) -> None:
    source = _write(tmp_path / "bad.cube", contents)
    with pytest.raises(InvalidCubeLutError):
        inspect_cube_lut(source)


def test_default_policy_accepts_runtime_on_the_selected_drive(tmp_path: Path) -> None:
    engine = CreativeLutEngine(tmp_path / "runtime")

    assert engine.runtime_root == (tmp_path / "runtime").resolve()
