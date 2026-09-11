import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from landscape_culler.xmp import build_xmp, rollback_manifest, validate_xmp, write_results_xmp
from landscape_culler.util import write_json


def _xml_body(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("<?xpacket"))


def test_build_xmp_is_valid_and_contains_rating() -> None:
    text = build_xmp(4, ["AI|候选|强推荐", "AI|天气|雾"])
    root = ET.fromstring(_xml_body(text))
    assert root.tag.endswith("xmpmeta")
    assert 'xmp:Rating="4"' in text
    assert "AI|候选|强推荐" in text
    validate_xmp(text)


def test_build_xmp_contains_confirmed_lightroom_develop_recipe() -> None:
    recipe = {
        "confirmed": True,
        "base": {"ProcessVersion": "11.0", "Exposure2012": 0.35, "Highlights2012": -22},
        "crop_candidates": [{
            "id": "tight",
            "bounds": {"left": 0.1, "top": 0.08, "right": 0.9, "bottom": 0.92},
        }],
        "crop_id": "tight",
        "angle": -0.4,
        "style_id": "natural",
        "style_strength": 60,
    }
    text = build_xmp(4, ["AI|候选"], recipe)
    validate_xmp(text)
    assert 'xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"' in text
    assert 'crs:Exposure2012="0.35"' in text
    assert 'crs:CropLeft="0.1"' in text
    assert 'crs:CropAngle="-0.4"' in text


def _disable_external_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("landscape_culler.xmp.assert_lightroom_not_running", lambda: None)
    monkeypatch.setattr("landscape_culler.xmp.assert_exiftool_available", lambda: Path("exiftool"))
    monkeypatch.setattr("landscape_culler.xmp.validate_xmp_with_exiftool", lambda *_args: None)


def test_write_and_rollback_does_not_change_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_external_checks(monkeypatch)
    raw = tmp_path / "DSC0001.ARW"
    raw.write_bytes(b"raw-data")
    results = tmp_path / "results.json"
    write_json(
        results,
        {
            "results": [
                {
                    "path": str(raw),
                    "rating": 4,
                    "score": 1.0,
                    "keywords": ["AI|候选|强推荐"],
                }
            ]
        },
    )
    before = raw.read_bytes()
    outcome = write_results_xmp(results, commit=True)
    assert outcome["created_count"] == 1
    assert raw.read_bytes() == before
    sidecar = raw.with_suffix(".xmp")
    assert sidecar.exists()
    rollback_manifest(Path(outcome["manifest_path"]))
    assert not sidecar.exists()
    assert raw.read_bytes() == before


def test_excluded_high_rating_never_writes_xmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_external_checks(monkeypatch)
    raw = tmp_path / "REMOVED.ARW"
    raw.write_bytes(b"raw-data")
    results = tmp_path / "results.json"
    write_json(results, {"results": [{
        "path": str(raw),
        "rating": 5,
        "score": 1.0,
        "keywords": ["人工|终选"],
        "excluded": True,
    }]})

    outcome = write_results_xmp(results, commit=True)
    assert outcome["created_count"] == 0
    assert not raw.with_suffix(".xmp").exists()


def test_write_results_counts_and_writes_confirmed_develop_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_external_checks(monkeypatch)
    raw = tmp_path / "DSC0005.ARW"
    raw.write_bytes(b"raw-data")
    results = tmp_path / "results.json"
    recipe = {
        "confirmed": True,
        "base": {"ProcessVersion": "11.0", "Exposure2012": -0.2},
        "crop_candidates": [{
            "id": "original",
            "bounds": {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0},
        }],
        "crop_id": "original",
        "angle": 0.0,
        "style_id": "clear",
        "style_strength": 50,
    }
    write_json(results, {"results": [{
        "path": str(raw), "rating": 5, "score": 1.0, "keywords": [], "develop": recipe,
    }]})
    before = raw.read_bytes()
    outcome = write_results_xmp(results, commit=True)
    assert outcome["created_count"] == 1
    assert outcome["develop_count"] == 1
    text = raw.with_suffix(".xmp").read_text(encoding="utf-8")
    assert 'crs:Exposure2012="-0.2"' in text
    assert 'crs:Dehaze="3"' in text
    sidecar = raw.with_suffix(".xmp")
    assert sidecar.exists()
    rollback_manifest(Path(outcome["manifest_path"]))
    assert not sidecar.exists()
    assert raw.read_bytes() == before


def test_commit_never_overwrites_xmp_created_during_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_external_checks(monkeypatch)
    raw = tmp_path / "DSC0002.ARW"
    raw.write_bytes(b"raw-data")
    results = tmp_path / "results.json"
    write_json(results, {"results": [{"path": str(raw), "rating": 4, "score": 1.0, "keywords": []}]})

    def inject_existing(path: Path, _text: str) -> None:
        path.write_text("user xmp", encoding="utf-8")
        raise FileExistsError(path)

    monkeypatch.setattr("landscape_culler.xmp.atomic_create_text", inject_existing)
    outcome = write_results_xmp(results, commit=True)
    assert outcome["created_count"] == 0
    assert outcome["skipped_count"] == 1
    assert raw.with_suffix(".xmp").read_text(encoding="utf-8") == "user xmp"


def test_validation_failure_preserves_externally_replaced_xmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_external_checks(monkeypatch)
    raw = tmp_path / "DSC0003.ARW"
    raw.write_bytes(b"raw-data")
    results = tmp_path / "results.json"
    write_json(results, {"results": [{"path": str(raw), "rating": 4, "score": 1.0, "keywords": []}]})

    def replace_then_fail(path: Path, _rating: int, _develop: dict | None = None) -> None:
        path.write_text("external replacement", encoding="utf-8")
        raise RuntimeError("readback failed")

    monkeypatch.setattr("landscape_culler.xmp.validate_xmp_with_exiftool", replace_then_fail)
    with pytest.raises(RuntimeError, match="readback failed"):
        write_results_xmp(results, commit=True)
    assert raw.with_suffix(".xmp").read_text(encoding="utf-8") == "external replacement"


def test_rollback_refuses_record_without_creation_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("landscape_culler.xmp.assert_lightroom_not_running", lambda: None)
    raw = tmp_path / "DSC0004.ARW"
    raw.write_bytes(b"raw-data")
    xmp = raw.with_suffix(".xmp")
    xmp.write_text("must stay", encoding="utf-8")
    manifest = tmp_path / "xmp-commit-manifest-fake.json"
    write_json(manifest, {
        "schema_version": 2,
        "commit": True,
        "records": [{"status": "created", "raw_path": str(raw), "xmp_path": str(xmp)}],
    })
    result = rollback_manifest(manifest)
    assert result["removed"] == []
    assert xmp.read_text(encoding="utf-8") == "must stay"
