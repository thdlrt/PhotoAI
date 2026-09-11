from __future__ import annotations

from pathlib import Path

import pytest

from landscape_culler.export_state import (
    create_export_attempt,
    create_export_spec,
    export_summary,
    load_export_spec,
    pending_export_work,
    record_export_result,
)


def test_export_spec_keeps_xmp_and_jpeg_independent(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    input_root = tmp_path / "shoot"
    input_root.mkdir()
    spec = create_export_spec(
        data_dir,
        run_id="run",
        input_root=input_root,
        items=[
            {"item_id": "a", "path": str(input_root / "same.ARW"), "rating": 4},
            {"item_id": "b", "path": str(input_root / "same.CR3"), "rating": 5},
        ],
        xmp=True,
        jpeg=True,
        develop_revision=3,
    )
    assert spec["output_dir"] == str((input_root / "成片").resolve())
    assert spec["jpeg_options"] == {
        "color_space": "sRGB",
        "resize": "original",
        "quality": 90,
        "output_sharpening": "screen-standard",
        "collision": "suffix",
    }
    assert spec["items"][0]["targets"]["jpeg"]["output"].endswith("same.jpg")
    assert spec["items"][1]["targets"]["jpeg"]["output"].endswith("same-2.jpg")
    assert len(pending_export_work(spec)) == 4
    assert load_export_spec(data_dir, spec["export_spec_id"])["develop_revision"] == 3


def test_failed_jpeg_does_not_roll_back_successful_xmp_and_retry_is_targeted(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    spec = create_export_spec(
        data_dir,
        run_id="run",
        input_root=tmp_path,
        items=[{"item_id": "a", "path": str(tmp_path / "a.ARW")}],
        xmp=True,
        jpeg=True,
    )
    record_export_result(
        data_dir, spec, item_id="a", target="xmp", succeeded=True, output="a.xmp"
    )
    record_export_result(
        data_dir,
        spec,
        item_id="a",
        target="jpeg",
        succeeded=False,
        error="Lightroom 断连",
    )
    summary = export_summary(spec)
    assert summary["status"] == "partial_failure"
    assert summary["targets"]["xmp"]["succeeded"] == 1
    assert summary["targets"]["jpeg"]["failed"] == 1
    retry = pending_export_work(spec, retry_failed=True)
    assert [(item["item_id"], item["target"]) for item in retry] == [("a", "jpeg")]

    record_export_result(
        data_dir, spec, item_id="a", target="jpeg", succeeded=True, output="a.jpg"
    )
    assert spec["status"] == "complete"
    assert spec["items"][0]["targets"]["xmp"]["status"] == "succeeded"


def test_export_requires_at_least_one_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="至少"):
        create_export_spec(
            tmp_path,
            run_id="run",
            input_root=tmp_path,
            items=[],
            xmp=False,
            jpeg=False,
        )


def test_creative_profile_xmp_attempt_is_forced_through_lightroom(tmp_path: Path) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    spec = create_export_spec(
        tmp_path / "data",
        run_id="run",
        input_root=tmp_path,
        items=[
            {
                "item_id": "a",
                "path": str(raw),
                "rating": 4,
                "develop": {
                    "creative_style": {
                        "status": "confirmed",
                        "preset_id": "profile-1",
                        "look_kind": "lightroom_profile",
                        "profile_name": "Modern 10",
                        "amount": 135,
                        "xmp_compatible": True,
                    }
                },
            }
        ],
        xmp=True,
        jpeg=False,
    )

    attempt = create_export_attempt(tmp_path / "data", spec)

    assert attempt is not None
    assert attempt["engine"] == "lightroom"
    assert attempt["output_mode"] == "xmp"
