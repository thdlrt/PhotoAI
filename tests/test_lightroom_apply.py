from __future__ import annotations

from pathlib import Path

import pytest

import landscape_culler.lightroom_apply as apply
import landscape_culler.lightroom_export as export
from landscape_culler.lightroom_apply import (
    LightroomApplyError,
    apply_results_in_lightroom,
    lightroom_tasks_from_payload,
    wait_for_lightroom_batch,
)
from landscape_culler.lightroom_export import (
    execute_lightroom_export,
    export_tasks_from_spec,
)
from landscape_culler.progress import parse_progress_line
from landscape_culler.util import write_json


def _confirmed_recipe() -> dict:
    return {
        "confirmed": True,
        "base": {
            "ProcessVersion": "15.4",
            "WhiteBalance": "Auto",
            "AutoTone": "True",
            "AutoLateralCA": 1,
            "LensProfileEnable": 1,
            "EnableTransform": 1,
            "PerspectiveUpright": 1,
            "ConstrainToWarp": 1,
            "Exposure2012": 0.25,
            "CropLeft": 0.99,
            "HasCrop": "True",
            "UnsafeSetting": "ignored",
        },
        "crop_candidates": [
            {"id": "original", "bounds": {"left": 0, "top": 0, "right": 1, "bottom": 1}},
            {"id": "tight", "bounds": {"left": 0.1, "top": 0.2, "right": 0.9, "bottom": 0.8}},
        ],
        "crop_id": "tight",
        "angle": -0.5,
    }


def test_tasks_include_only_confirmed_selected_photos_and_separate_controls(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen.ARW"
    excluded = tmp_path / "excluded.ARW"
    low = tmp_path / "low.ARW"
    unconfirmed = tmp_path / "unconfirmed.ARW"
    for path in (chosen, excluded, low, unconfirmed):
        path.write_bytes(b"raw bytes are never read by this module")

    not_confirmed = _confirmed_recipe()
    not_confirmed["confirmed"] = False
    tasks = lightroom_tasks_from_payload(
        {
            "results": [
                {"path": str(chosen), "rating": 4, "develop": _confirmed_recipe()},
                {"path": str(excluded), "rating": 5, "excluded": True, "develop": _confirmed_recipe()},
                {"path": str(low), "rating": 2, "develop": _confirmed_recipe()},
                {"path": str(unconfirmed), "rating": 5, "develop": not_confirmed},
            ]
        }
    )

    assert len(tasks) == 1
    task = tasks[0]
    assert task.photo_path == chosen.resolve()
    assert task.rating == 4
    assert task.auto_tone is True
    assert task.auto_white_balance is True
    assert task.lens_profile is True
    assert task.remove_chromatic_aberration is True
    assert task.crop == {"left": 0.1, "top": 0.2, "right": 0.9, "bottom": 0.8, "angle": -0.5}
    assert task.style == {
        "EnableTransform": 1,
        "PerspectiveUpright": 1,
        "ConstrainToWarp": 1,
        "Exposure2012": 0.25,
    }
    assert "ProcessVersion" not in task.style
    assert "WhiteBalance" not in task.style
    assert "AutoTone" not in task.style
    assert "CropLeft" not in task.style
    assert "UnsafeSetting" not in task.style


def test_tasks_reject_missing_selection_and_empty_eligibility(tmp_path: Path) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    broken = _confirmed_recipe()
    broken["crop_id"] = "missing"
    with pytest.raises(LightroomApplyError, match="找不到已确认"):
        lightroom_tasks_from_payload({"results": [{"path": str(raw), "rating": 4, "develop": broken}]})
    with pytest.raises(LightroomApplyError, match="没有可交给 Lightroom"):
        lightroom_tasks_from_payload({"results": [{"path": str(raw), "rating": 2, "develop": broken}]})


def test_tasks_reject_embedded_metadata_raw_to_preserve_source_bytes(tmp_path: Path) -> None:
    dng = tmp_path / "photo.DNG"
    dng.write_bytes(b"dng")
    with pytest.raises(LightroomApplyError, match="RAW 文件本身不被修改"):
        lightroom_tasks_from_payload(
            {"results": [{"path": str(dng), "rating": 4, "develop": _confirmed_recipe()}]}
        )


def test_lightroom_integrity_check_hashes_entire_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"start" + (b"a" * 32) + b"end")
    results = tmp_path / "results.reviewed.json"
    write_json(results, {"results": [{"path": str(raw), "rating": 4, "develop": _confirmed_recipe()}]})
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"exe")

    monkeypatch.setattr(
        apply,
        "write_lightroom_plugin_config",
        lambda data_dir: {"root": str(Path(data_dir) / "lightroom-bridge")},
    )
    monkeypatch.setattr(apply, "read_lightroom_bridge_status", lambda _data_dir: {"heartbeat": {"state": "online"}})
    monkeypatch.setattr(apply, "create_lightroom_batch", lambda *_args, **_kwargs: {"task_count": 1})

    def mutate_middle(_data_dir, batch_id, _timeout):
        payload = bytearray(raw.read_bytes())
        payload[len(payload) // 2] ^= 1
        raw.write_bytes(payload)
        return {"batch_id": batch_id, "status": "complete", "tasks": [], "counts": {"done": 1}}

    monkeypatch.setattr(apply, "wait_for_lightroom_batch", mutate_middle)
    with pytest.raises(LightroomApplyError, match="RAW 文件在处理期间发生变化"):
        apply_results_in_lightroom(results, tmp_path / "data", "full-hash", lightroom_exe=executable)


def test_apply_configures_launches_publishes_and_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    results = tmp_path / "results.reviewed.json"
    write_json(results, {"results": [{"path": str(raw), "rating": 4, "develop": _confirmed_recipe()}]})
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"exe")
    calls: list[tuple] = []

    monkeypatch.setattr(
        apply,
        "write_lightroom_plugin_config",
        lambda data_dir: {"root": str(Path(data_dir) / "lightroom-bridge")},
    )
    monkeypatch.setattr(apply, "read_lightroom_bridge_status", lambda _data_dir: {"heartbeat": {"state": "offline"}})
    monkeypatch.setattr(apply, "start_lightroom", lambda path: calls.append(("launch", Path(path))))
    monkeypatch.setattr(apply, "wait_for_lightroom_online", lambda data_dir, timeout: calls.append(("online", data_dir, timeout)))

    def create(data_dir, tasks, *, batch_id):
        calls.append(("create", data_dir, tasks, batch_id))
        return {"task_count": len(tasks)}

    monkeypatch.setattr(apply, "create_lightroom_batch", create)
    monkeypatch.setattr(
        apply,
        "wait_for_lightroom_batch",
        lambda data_dir, batch_id, timeout: {
            "batch_id": batch_id,
            "status": "complete",
            "counts": {"done": 1, "failed": 0},
            "tasks": [],
        },
    )

    outcome = apply_results_in_lightroom(
        results,
        tmp_path / "data",
        "web-run-123",
        lightroom_exe=executable,
        startup_timeout=5,
        batch_timeout=10,
    )

    assert calls[0] == ("launch", executable.resolve())
    assert calls[1][0] == "online"
    assert calls[2][0] == "create"
    assert calls[2][3] == "web-run-123"
    assert outcome["batch_id"] == "web-run-123"
    assert outcome["lightroom_launched"] is True
    assert outcome["published_count"] == 1


def test_batch_failure_names_failed_photo(tmp_path: Path) -> None:
    status = {
        "task_count": 1,
        "completed_count": 1,
        "status": "failed",
        "tasks": [
            {
                "photo_path": str(tmp_path / "bad.ARW"),
                "status": "failed",
                "result": {"message": "catalog locked"},
            }
        ],
    }
    with pytest.raises(LightroomApplyError, match=r"bad\.ARW: catalog locked"):
        wait_for_lightroom_batch(tmp_path, "batch", 1, status_reader=lambda _data, _batch: status)


def test_batch_progress_uses_fixed_total_and_cached_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    statuses = iter(
        [
            {
                "task_count": 93,
                "completed_count": 9,
                "status": "running",
                "tasks": [],
            },
            {
                "task_count": 93,
                "completed_count": 93,
                "status": "complete",
                "tasks": [],
            },
        ]
    )

    result = wait_for_lightroom_batch(
        tmp_path,
        "style-previews",
        1,
        poll_interval=0.001,
        status_reader=lambda _data, _batch: next(statuses),
        progress_phase="process",
        progress_label="Lightroom 真实预览",
        progress_unit="项预览",
        progress_offset=7,
        progress_total=100,
    )

    events = [
        event
        for line in capsys.readouterr().out.splitlines()
        if (event := parse_progress_line(line)) is not None
    ]
    assert result["status"] == "complete"
    assert {event["phase"] for event in events} == {"process"}
    assert {event["label"] for event in events} == {"Lightroom 真实预览"}
    assert {event["total"] for event in events} == {100}
    assert {event["unit"] for event in events} == {"项预览"}
    assert events[0]["event"] == "phase_start"
    assert events[0]["current"] == 7
    assert events[0]["cached"] == 7
    assert any(event["event"] == "progress" and event["current"] == 16 for event in events)
    assert events[-1]["event"] == "phase_end"
    assert events[-1]["current"] == 100


def test_failed_bridge_still_runs_complete_raw_integrity_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw-before")
    results = tmp_path / "results.reviewed.json"
    write_json(results, {"results": [{"path": str(raw), "rating": 4, "develop": _confirmed_recipe()}]})
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"exe")
    monkeypatch.setattr(
        apply,
        "write_lightroom_plugin_config",
        lambda data_dir: {"root": str(Path(data_dir) / "lightroom-bridge")},
    )
    monkeypatch.setattr(apply, "read_lightroom_bridge_status", lambda _data_dir: {"heartbeat": {"state": "online"}})
    monkeypatch.setattr(apply, "create_lightroom_batch", lambda *_args, **_kwargs: {"task_count": 1})

    def fail_after_mutation(_data_dir, _batch_id, _timeout):
        raw.write_bytes(b"raw-after!")
        raise LightroomApplyError("bridge failed")

    monkeypatch.setattr(apply, "wait_for_lightroom_batch", fail_after_mutation)
    with pytest.raises(LightroomApplyError, match="RAW 文件在处理期间发生变化"):
        apply_results_in_lightroom(results, tmp_path / "data", "failed-hash", lightroom_exe=executable)


@pytest.mark.parametrize("state", ["cancelling", "cancelled"])
def test_cancelled_batch_stops_wait_without_reporting_success(tmp_path: Path, state: str) -> None:
    status = {
        "task_count": 2,
        "completed_count": 1,
        "status": state,
        "counts": {"pending": 1, "running": 0, "done": 0, "failed": 0, "cancelled": 1},
        "tasks": [],
    }
    with pytest.raises(LightroomApplyError, match="已取消"):
        wait_for_lightroom_batch(tmp_path, "batch", 30, status_reader=lambda _data, _batch: status)


def test_cli_parser_accepts_lightroom_apply_contract() -> None:
    from landscape_culler.cli import build_parser

    args = build_parser().parse_args(
        [
            "lightroom-apply",
            "--results",
            __file__,
            "--data-dir",
            r"E:\code\data",
            "--batch-id",
            "batch-1",
            "--lightroom-exe",
            r"E:\软件\Adobe Lightroom Classic\Lightroom.exe",
        ]
    )
    assert args.command == "lightroom-apply"
    assert args.batch_id == "batch-1"
    assert args.startup_timeout == 120.0
    assert args.batch_timeout == 7200.0


def test_export_spec_builds_both_target_tasks_with_exact_preset(tmp_path: Path) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    output_dir = (tmp_path / "成片").resolve()
    tasks = export_tasks_from_spec(
        {
            "schema_version": 1,
            "targets": {"xmp": True, "jpeg": True},
            "jpeg_settings": {
                "quality": 90,
                "color_space": "sRGB",
                "size": "original",
                "sharpening": "screen_standard",
                "collision": "suffix",
            },
            "output_dir": str(output_dir),
            "items": [
                {
                    "photo_path": str(raw),
                    "rating": 4,
                    "crop": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9},
                    "style": {"Vibrance": 8},
                    "preset_uuid": "550e8400-e29b-41d4-a716-446655440000",
                    "preset_scope": "plugin",
                    "preset_amount": 72,
                }
            ],
        }
    )

    assert len(tasks) == 1
    assert tasks[0].output_mode == "both"
    assert tasks[0].jpeg_output_dir == output_dir
    assert tasks[0].preset_scope == "plugin"
    assert tasks[0].preset_amount == 72


def test_export_spec_can_load_frozen_results_and_creative_style(tmp_path: Path) -> None:
    raw = tmp_path / "photo.NEF"
    raw.write_bytes(b"raw")
    recipe = _confirmed_recipe()
    recipe["creative_style"] = {
        "status": "confirmed",
        "preset_uuid": "550e8400-e29b-41d4-a716-446655440000",
        "preset_scope": "catalog",
        "amount": 85,
    }
    results = tmp_path / "frozen-results.json"
    write_json(results, {"results": [{"path": str(raw), "rating": 5, "develop": recipe}]})

    tasks = export_tasks_from_spec(
        {"targets": {"xmp": False, "jpeg": True}, "results_path": str(results)}
    )

    assert tasks[0].output_mode == "jpeg"
    assert tasks[0].preset_uuid == "550e8400-e29b-41d4-a716-446655440000"
    assert tasks[0].preset_amount == 85
    assert tasks[0].jpeg_output_dir is None


def test_creative_look_uses_frozen_descriptor_after_base(tmp_path: Path) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    recipe = _confirmed_recipe()
    recipe["creative_style"] = {
        "status": "confirmed",
        "preset_id": "uuid:creative-profile",
        "look_kind": "lightroom_profile",
        "profile_name": "Modern 10",
        "profile_hash": "abc123",
        "look_descriptor_path": "E:/runtime/lightroom-bridge/presets/looks/"
        + ("a" * 64)
        + ".look",
        "look_descriptor_hash": "a" * 64,
        "look_uuid": "LOOK-UUID",
        "look_amount": 135,
        "amount": 135,
        "xmp_compatible": True,
    }

    tasks = export_tasks_from_spec(
        {
            "targets": {"xmp": True, "jpeg": True},
            "items": [{"path": str(raw), "rating": 4, "develop": recipe}],
        }
    )

    assert tasks[0].preset_uuid is None
    assert tasks[0].style["Exposure2012"] == 0.25
    assert "CameraProfile" not in tasks[0].style
    assert tasks[0].look_uuid == "LOOK-UUID"
    assert tasks[0].look_descriptor_hash == "a" * 64
    assert tasks[0].look_amount == 135


def test_creative_look_rejects_ordinary_preset_uuid(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    recipe = _confirmed_recipe()
    recipe["creative_style"] = {
        "status": "confirmed",
        "preset_id": "uuid:creative-look",
        "preset_uuid": "9901408EBF8D496E99EFC526805F2F7C",
        "preset_scope": "catalog",
        "look_kind": "lightroom_profile",
        "profile_name": "Film-Inspired 12",
        "amount": 135,
    }

    with pytest.raises(LightroomApplyError, match="不能作为普通 Lightroom 预设"):
        export_tasks_from_spec(
            {
                "targets": {"xmp": True, "jpeg": True},
                "items": [{"path": str(raw), "rating": 4, "develop": recipe}],
            }
        )


def test_export_spec_rejects_empty_targets_and_nonstandard_jpeg_settings(tmp_path: Path) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    item = {"photo_path": str(raw), "rating": 4}
    with pytest.raises(LightroomApplyError, match="至少要选择"):
        export_tasks_from_spec({"targets": {"xmp": False, "jpeg": False}, "items": [item]})
    with pytest.raises(LightroomApplyError, match="quality"):
        export_tasks_from_spec(
            {
                "targets": {"xmp": False, "jpeg": True},
                "jpeg_settings": {"quality": 80},
                "items": [item],
            }
        )


def test_lightroom_export_runs_queue_and_preserves_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    spec = tmp_path / "export.json"
    write_json(
        spec,
        {
            "targets": {"xmp": True, "jpeg": False},
            "items": [{"photo_path": str(raw), "rating": 4}],
        },
    )
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"exe")
    captured: dict = {}
    monkeypatch.setattr(export, "write_lightroom_plugin_config", lambda data: {"root": str(data)})
    monkeypatch.setattr(export, "read_lightroom_bridge_status", lambda _data: {"heartbeat": {"state": "online"}})
    monkeypatch.setattr(export, "resolve_lightroom_executable", lambda _path: executable)

    def create(_data, tasks, *, batch_id):
        captured["tasks"] = tasks
        captured["batch_id"] = batch_id
        return {"task_count": len(tasks)}

    monkeypatch.setattr(export, "create_lightroom_batch", create)
    monkeypatch.setattr(
        export,
        "wait_for_lightroom_batch",
        lambda _data, batch_id, _timeout: {
            "batch_id": batch_id,
            "status": "complete",
            "task_count": 1,
            "tasks": [{"result": {"xmp_status": "done", "jpeg_status": "not_requested"}}],
        },
    )

    result = execute_lightroom_export(spec, tmp_path / "data", "export-1", lightroom_exe=executable)
    assert captured["batch_id"] == "export-1"
    assert captured["tasks"][0].output_mode == "xmp"
    assert result["targets"] == {"xmp": True, "jpeg": False}
    assert result["tasks"][0]["result"]["xmp_status"] == "done"


def test_cli_parser_accepts_lightroom_export_contract(tmp_path: Path) -> None:
    from landscape_culler.cli import build_parser

    spec = tmp_path / "spec.json"
    spec.write_text("{}", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "lightroom-export",
            "--spec",
            str(spec),
            "--data-dir",
            str(tmp_path / "data"),
            "--batch-id",
            "export-1",
            "--lightroom-exe",
            str(tmp_path / "Lightroom.exe"),
        ]
    )
    assert args.command == "lightroom-export"
    assert args.spec == spec
    assert args.startup_timeout == 120.0
    assert args.batch_timeout == 7200.0
