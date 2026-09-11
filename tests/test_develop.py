from __future__ import annotations

import copy
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from landscape_culler.develop import (
    confirm_recommended_styles,
    create_develop_plan,
    develop_summary,
    load_develop_plan,
    merge_confirmed_develop,
    skip_all_crops,
    update_develop_item,
    update_develop_options,
    update_style_global,
    update_style_group,
    xmp_settings_for_recipe,
)
from landscape_culler.util import write_json


def _landscape_preview(path: Path) -> None:
    image = Image.new("RGB", (640, 400), "#6f91ad")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 210, 640, 400), fill="#526247")
    draw.line((0, 210, 640, 214), fill="#e1c58d", width=3)
    draw.ellipse((390, 110, 490, 210), fill="#d29a55")
    image.save(path, "JPEG", quality=92)


def test_develop_plan_is_bounded_editable_and_revisioned(tmp_path: Path) -> None:
    preview = tmp_path / "preview.jpg"
    _landscape_preview(preview)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = {
        "run_id": "test-run",
        "results": [
            {
                "path": str(tmp_path / "selected.ARW"),
                "preview": str(preview),
                "rating": 4,
                "group_id": 3,
                "technical": {"brightness": 0.42, "contrast": 0.48, "saturation": 0.30},
            },
            {
                "path": str(tmp_path / "rejected.ARW"),
                "preview": str(preview),
                "rating": 5,
                "group_id": 4,
                "excluded": True,
            },
        ],
    }

    progress_events: list[dict] = []
    plan = create_develop_plan(payload, run_dir, review_revision=7, progress=progress_events.append)
    summary = develop_summary(plan, 7)
    assert summary["eligible_count"] == 1
    assert summary["plan_id"] == plan["plan_id"]
    assert summary["crop_skipped"] is False
    assert summary["color_enabled"] is True
    assert develop_summary(plan, 8)["stale"] is True
    item = plan["items"][0]
    assert Path(item["preview_path"]).is_file()
    assert item["confirmed"] is False
    assert item["base"]["AutoTone"] == "True"
    assert item["base"]["WhiteBalance"] == "Auto"
    assert item["base"]["PerspectiveUpright"] == 1
    assert item["style_id"] == "lightroom"
    assert {candidate["id"] for candidate in item["crop_candidates"]} == {"original", "balanced", "tight", "wide"}
    for candidate in item["crop_candidates"]:
        bounds = candidate["bounds"]
        assert 0 <= bounds["left"] < bounds["right"] <= 1
        assert 0 <= bounds["top"] < bounds["bottom"] <= 1
    assert {candidate["engine"] for candidate in item["crop_candidates"] if candidate["id"] != "original"} == {
        "semantic-rules"
    }
    assert item["crop_engine"] == "semantic-crop-v2"
    assert item["smart_crop"]["dense_candidate_count"] > 20
    assert item["smart_crop"]["valid_candidate_count"] > 0
    assert progress_events
    assert progress_events[-1]["overall_percent"] == 100.0
    assert progress_events[-1]["completed"] == 1
    assert {event["phase"] for event in progress_events} >= {"candidates", "rank", "preview"}

    item["_source_preview"] = str(preview)
    update_develop_item(
        plan,
        run_dir,
        0,
        crop_id="tight",
        style_id="lightroom",
        style_strength=100,
        confirmed=True,
    )
    assert plan["revision"] == 1
    assert item["confirmed"] is True
    assert Path(item["preview_path"]).is_file()
    settings = xmp_settings_for_recipe(item)
    assert settings["HasCrop"] == "True"
    assert (settings["CropRight"] - settings["CropLeft"]) * (settings["CropBottom"] - settings["CropTop"]) < 0.8
    assert settings["AutoTone"] == "True"
    assert settings["WhiteBalance"] == "Auto"

    merged = merge_confirmed_develop(copy.deepcopy(payload), plan, review_revision=7)
    assert merged["develop_confirmed_count"] == 1
    assert merged["results"][0]["develop"]["confirmed"] is True
    assert "develop" not in merged["results"][1]
    stale = merge_confirmed_develop(copy.deepcopy(payload), plan, review_revision=8)
    assert stale["develop_confirmed_count"] == 0
    assert "develop" not in stale["results"][0]


def test_develop_plan_identity_options_and_legacy_defaults(tmp_path: Path) -> None:
    preview = tmp_path / "preview.jpg"
    _landscape_preview(preview)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    payload = {
        "run_id": "identity-run",
        "results": [{
            "path": str(tmp_path / "selected.ARW"),
            "preview": str(preview),
            "rating": 4,
            "group_id": 1,
        }],
    }

    first = create_develop_plan(payload, run_dir, review_revision=3)
    second = create_develop_plan(payload, run_dir, review_revision=3)
    assert first["plan_id"].startswith("develop-")
    assert second["plan_id"].startswith("develop-")
    assert first["plan_id"] != second["plan_id"]
    assert first["creative_style"]["scope"] == "global"

    skip_all_crops(second, run_dir, {0: preview})
    assert second["revision"] == 1
    assert second["crop_skipped"] is True
    assert second["items"][0]["crop_id"] == "original"
    assert second["items"][0]["confirmed"] is True

    update_develop_options(second, run_dir, color_enabled=False)
    assert second["revision"] == 2
    assert second["color_enabled"] is False

    second["items"][0]["_source_preview"] = str(preview)
    update_develop_item(second, run_dir, 0, crop_id="balanced")
    assert second["revision"] == 3
    assert second["crop_skipped"] is False

    legacy = copy.deepcopy(second)
    legacy.pop("plan_id")
    legacy.pop("crop_skipped")
    legacy.pop("color_enabled")
    write_json(run_dir / "develop.json", legacy)
    loaded = load_develop_plan(run_dir)
    assert loaded is not None
    assert loaded["plan_id"].startswith("legacy-")
    assert loaded["crop_skipped"] is False
    assert loaded["color_enabled"] is True
    assert load_develop_plan(run_dir)["plan_id"] == loaded["plan_id"]


def _style_plan(*, candidate: dict | None = None) -> dict:
    ready = {
        "preset_id": "uuid:style-1",
        "preset_hash": "hash-style-1",
        "preset_uuid": "runtime-style-1",
        "preset_scope": "catalog",
        "amount": 65,
        "amount_supported": True,
        "render_status": "ready",
        "preview_key": "a" * 64,
        "preview_path": r"E:\previews\style-1.jpg",
    }
    if candidate:
        ready.update(candidate)
    return {
        "schema_version": 5,
        "plan_id": "develop-style-test",
        "run_id": "style-test",
        "source_review_revision": 0,
        "revision": 3,
        "crop_skipped": False,
        "color_enabled": True,
        "crop": {"status": "confirmed"},
        "basic_color": {"status": "enabled"},
        "creative_style": {
            "status": "pending",
            "groups": {
                "1": {
                    "group_id": 1,
                    "status": "pending",
                    "recommendation_status": "complete",
                    "recommended_kind": "preset",
                    "recommended_preset_id": "uuid:style-1",
                    "recommended_preset_hash": "hash-style-1",
                    "recommended_amount": 65,
                    "manual_override": False,
                    "top3": [ready],
                }
            },
        },
        "color_mode": "style",
        "items": [{"index": 0, "group_id": 1, "crop_confirmed": True, "confirmed": True}],
    }


def test_update_style_group_binds_selection_to_exact_ready_preview(tmp_path: Path) -> None:
    plan = _style_plan()

    selected = update_style_group(
        plan,
        tmp_path,
        1,
        preset_id="uuid:style-1",
        preset_hash="hash-style-1",
        amount=65,
    )

    assert plan["revision"] == 4
    assert plan["creative_style"]["status"] == "confirmed"
    assert selected["preset_hash"] == "hash-style-1"
    assert selected["preset_uuid"] == "runtime-style-1"
    assert selected["preset_scope"] == "catalog"
    assert selected["amount"] == 65
    assert selected["amount_supported"] is True
    assert selected["selected_preview_key"] == "a" * 64
    assert selected["selected_preview_path"] == r"E:\previews\style-1.jpg"


@pytest.mark.parametrize(
    ("candidate_patch", "preset_hash", "amount", "message"),
    [
        ({}, None, 65, "版本信息"),
        ({}, "different-hash", 65, "预设版本"),
        ({}, "hash-style-1", 70, "所选强度"),
        ({"render_status": "pending"}, "hash-style-1", 65, "真实预览"),
        ({"preview_key": None}, "hash-style-1", 65, "真实预览"),
        ({"amount": 80, "amount_supported": False}, "hash-style-1", 80, "不支持强度"),
        ({"amount": 205}, "hash-style-1", 205, "0 到 200"),
    ],
)
def test_update_style_group_rejects_selection_that_does_not_match_preview(
    tmp_path: Path,
    candidate_patch: dict,
    preset_hash: str | None,
    amount: int,
    message: str,
) -> None:
    plan = _style_plan(candidate=candidate_patch)

    with pytest.raises(ValueError, match=message):
        update_style_group(
            plan,
            tmp_path,
            1,
            preset_id="uuid:style-1",
            preset_hash=preset_hash,
            amount=amount,
        )

    assert plan["revision"] == 3


def test_confirm_recommended_styles_copies_exact_candidate_identity(tmp_path: Path) -> None:
    plan = _style_plan()

    confirm_recommended_styles(plan, tmp_path)

    selected = plan["creative_style"]["groups"]["1"]
    assert plan["revision"] == 4
    assert selected["status"] == "confirmed"
    assert selected["preset_id"] == "uuid:style-1"
    assert selected["preset_hash"] == "hash-style-1"
    assert selected["preset_uuid"] == "runtime-style-1"
    assert selected["preset_scope"] == "catalog"
    assert selected["amount"] == 65
    assert selected["selected_preview_key"] == "a" * 64


def test_global_selection_is_saved_once_then_atomically_frozen_for_all_groups(
    tmp_path: Path,
) -> None:
    plan = _style_plan()
    global_selection = dict(plan["creative_style"]["groups"]["1"])
    global_selection.pop("group_id")
    plan["creative_style"] = {
        "scope": "global",
        "status": "pending",
        "groups": {},
        "global_selection": global_selection,
    }
    plan["items"].append(
        {"index": 1, "group_id": 2, "crop_confirmed": True, "confirmed": True}
    )

    selected = update_style_global(
        plan,
        tmp_path,
        preset_id="uuid:style-1",
        preset_hash="hash-style-1",
        amount=65,
    )

    assert selected["scope"] == "global"
    assert selected["status"] == "confirmed"
    assert plan["creative_style"]["groups"] == {}
    assert plan["revision"] == 4

    confirm_recommended_styles(plan, tmp_path)

    assert plan["revision"] == 5
    assert plan["creative_style"]["scope"] == "global"
    assert plan["creative_style"]["status"] == "confirmed"
    assert set(plan["creative_style"]["groups"]) == {"1", "2"}
    for group_id, frozen in plan["creative_style"]["groups"].items():
        assert frozen["group_id"] == int(group_id)
        assert frozen["preset_id"] == "uuid:style-1"
        assert frozen["preset_hash"] == "hash-style-1"
        assert frozen["amount"] == 65
        assert frozen["selected_preview_key"] == "a" * 64


def test_confirm_recommended_styles_rejects_amount_without_matching_preview(tmp_path: Path) -> None:
    plan = _style_plan()
    plan["creative_style"]["groups"]["1"]["recommended_amount"] = 70

    with pytest.raises(ValueError, match="推荐强度"):
        confirm_recommended_styles(plan, tmp_path)

    assert plan["revision"] == 3
    assert plan["creative_style"]["groups"]["1"]["status"] == "pending"


def test_update_style_group_accepts_ready_cube_lut_at_one_hundred_fifty_percent(
    tmp_path: Path,
) -> None:
    plan = _style_plan(
        candidate={
            "preset_id": None,
            "preset_hash": None,
            "lut_id": "cube-lut-1",
            "lut_hash": "lut-hash-1",
            "look_kind": "rendered_lut",
            "xmp_compatible": False,
            "amount": None,
            "strength": 150,
            "amount_supported": True,
        }
    )

    selected = update_style_group(
        plan,
        tmp_path,
        1,
        preset_id=None,
        lut_id="cube-lut-1",
        lut_hash="lut-hash-1",
        amount=150,
    )

    assert selected["preset_id"] is None
    assert selected["lut_id"] == "cube-lut-1"
    assert selected["lut_hash"] == "lut-hash-1"
    assert selected["amount"] == 150
    assert selected["strength"] == 150
    assert selected["xmp_compatible"] is False
    assert selected["look_kind"] == "rendered_lut"


def test_confirm_recommended_cube_lut_preserves_rendered_only_boundary(
    tmp_path: Path,
) -> None:
    plan = _style_plan(
        candidate={
            "preset_id": None,
            "preset_hash": None,
            "lut_id": "cube-lut-1",
            "lut_hash": "lut-hash-1",
            "look_kind": "rendered_lut",
            "xmp_compatible": False,
            "amount": None,
            "strength": 150,
            "amount_supported": True,
        }
    )
    group = plan["creative_style"]["groups"]["1"]
    group.update(
        recommended_kind="lut",
        recommended_preset_id=None,
        recommended_preset_hash=None,
        recommended_lut_id="cube-lut-1",
        recommended_lut_hash="lut-hash-1",
        recommended_strength=150,
    )

    confirm_recommended_styles(plan, tmp_path)

    selected = plan["creative_style"]["groups"]["1"]
    assert selected["status"] == "confirmed"
    assert selected["lut_id"] == "cube-lut-1"
    assert selected["strength"] == 150
    assert selected["xmp_compatible"] is False
