from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import landscape_culler.lightroom_bridge as bridge
from landscape_culler.lightroom_bridge import (
    HEARTBEAT_PROTOCOL,
    PRESET_PROTOCOL,
    RESULT_PROTOCOL,
    LightroomTask,
    bridge_paths,
    cancel_lightroom_batch,
    create_lightroom_batch,
    create_lightroom_preset_enumeration,
    create_lightroom_preview_metadata_repair,
    create_lightroom_transient_snapshot_cleanup,
    detect_lightroom_classic_15_3,
    install_lightroom_plugin,
    look_descriptor_sha256,
    parse_look_descriptor,
    parse_task_line,
    read_lightroom_batch_status,
    read_lightroom_bridge_status,
    read_lightroom_preset_listing,
    remove_owned_lightroom_plugin,
    serialize_look_descriptor,
    serialize_task_line,
    write_lightroom_look_descriptor,
    write_lightroom_plugin_config,
)
from landscape_culler.style_library import _lua_literal


def test_task_line_round_trips_unicode_crop_and_style(tmp_path: Path) -> None:
    photo = (tmp_path / "南京" / "风景 01.ARW").resolve()
    task = LightroomTask(
        photo_path=photo,
        rating=4,
        crop={"left": 0.1, "top": 0.05, "right": 0.9, "bottom": 0.95, "angle": -0.4},
        style={"Vibrance": 8, "WhiteBalance": "As Shot", "EnableTransform": True},
        task_id="photo-1",
    )

    line = serialize_task_line(task, batch_id="batch-1")
    assert "南京" not in line
    parsed = parse_task_line(line)

    assert parsed["photo_path"] == str(photo)
    assert parsed["rating"] == 4
    assert parsed["crop"]["CropLeft"] == 0.1
    assert parsed["crop"]["CropAngle"] == -0.4
    assert parsed["style"] == {
        "EnableTransform": True,
        "Vibrance": 8,
        "WhiteBalance": "As Shot",
    }


def test_legacy_task_shape_is_unchanged_and_new_defaults_are_inferred(
    tmp_path: Path,
) -> None:
    photo = (tmp_path / "legacy.ARW").resolve()
    line = serialize_task_line(LightroomTask(photo, task_id="legacy"), batch_id="batch")

    assert "task_type=" not in line
    assert "output_mode=" not in line
    assert "preset_uuid=" not in line
    parsed = parse_task_line(line)
    assert parsed["task_type"] == "apply"
    assert parsed["output_mode"] == "xmp"
    assert parsed["preset_uuid"] is None
    assert parsed["preset_amount"] == 100


def test_preview_task_round_trips_exact_plugin_preset_amount_and_output(
    tmp_path: Path,
) -> None:
    photo = (tmp_path / "preview.NEF").resolve()
    destination = (tmp_path / "preview-cache").resolve()
    line = serialize_task_line(
        LightroomTask(
            photo,
            task_type="preview",
            preset_uuid="550e8400-e29b-41d4-a716-446655440000",
            preset_scope="plugin",
            preset_amount=73,
            jpeg_output_dir=destination,
        ),
        batch_id="preview-batch",
        task_id="candidate-1",
    )
    parsed = parse_task_line(line)

    assert parsed["task_type"] == "preview"
    assert parsed["output_mode"] == "jpeg"
    assert parsed["preset_scope"] == "plugin"
    assert parsed["preset_amount"] == 73
    assert parsed["jpeg_output_dir"] == str(destination)


def _compact_look_descriptor() -> dict:
    payload = {
        "SchemaVersion": 1,
        "UUID": "9901408EBF8D496E99EFC526805F2F7C",
        "Name": "Film-Inspired 12",
        "Group": "Film-Inspired",
        "Cluster": "Adobe",
        "SupportsAmount": True,
        "Parameters": {
            "CameraProfile": "Adobe Standard",
            "LookTable": "E1095149FDB39D7A057BAB208837E2E1",
            "RGBTable": "42B6AB7E41D340B68A5AEB7D1DAFEA94",
            "Saturation": -27,
            "ToneCurvePV2012": ["0, 0", "128, 132", "255, 255"],
        },
        "TableDigests": {
            "Table_E1095149FDB39D7A057BAB208837E2E1": {
                "sha256": "a" * 64,
                "size": 12345,
            }
        },
        "ComplexParameterDigests": {},
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {**payload, "Hash": digest}


def test_creative_look_descriptor_and_task_round_trip_are_data_only() -> None:
    source = _compact_look_descriptor()
    uuid = source["UUID"]
    contents = serialize_look_descriptor(source, look_uuid=uuid)
    digest = look_descriptor_sha256(contents)
    path = rf"E:\photo-ai\lightroom-bridge\presets\looks\{digest}.look"

    look = parse_look_descriptor(contents, expected_uuid=uuid)
    assert look["UUID"] == uuid
    assert look["Amount"] == 1
    assert look["Group"] == {"x-default": "Film-Inspired"}
    assert look["Parameters"]["Saturation"] == -27
    assert look["Parameters"]["ToneCurvePV2012"] == {
        1: "0, 0",
        2: "128, 132",
        3: "255, 255",
    }
    assert "TableDigests" not in look
    assert "ComplexParameterDigests" not in look
    assert "Hash" not in look

    line = serialize_task_line(
        LightroomTask(
            r"E:\photos\one.ARW",
            task_type="preview",
            look_descriptor_path=path,
            look_descriptor_hash=digest,
            look_uuid=uuid,
            look_amount=135,
        ),
        batch_id="look-preview",
        task_id="candidate-1",
    )
    parsed = parse_task_line(line)
    assert "look_descriptor_path=" not in line
    assert parsed["look_descriptor_path"] is None
    assert parsed["look_descriptor_hash"] == digest
    assert parsed["look_uuid"] == uuid
    assert parsed["look_amount"] == 135
    assert parsed["preset_uuid"] is None


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"look_descriptor_path": r"C:\unsafe.look"}, "hash and look_uuid"),
        (
            {
                "look_descriptor_path": "\\\\server\\lightroom-bridge\\presets\\looks\\"
                + "a" * 64
                + ".look",
                "look_descriptor_hash": "a" * 64,
                "look_uuid": "LOOK-1",
            },
            "local Windows path",
        ),
        (
            {
                "look_descriptor_path": "E:\\elsewhere\\" + "a" * 64 + ".look",
                "look_descriptor_hash": "a" * 64,
                "look_uuid": "LOOK-1",
            },
            "lightroom-bridge",
        ),
        (
            {
                "look_descriptor_path": r"E:\data\lightroom-bridge\presets\looks\wrong.look",
                "look_descriptor_hash": "a" * 64,
                "look_uuid": "LOOK-1",
            },
            "filename",
        ),
        (
            {
                "look_descriptor_path": "E:\\data\\lightroom-bridge\\presets\\looks\\"
                + "a" * 64
                + ".look",
                "look_descriptor_hash": "a" * 64,
                "look_uuid": "LOOK-1",
                "preset_uuid": "PRESET-1",
            },
            "cannot be combined",
        ),
        ({"look_amount": 135}, "requires"),
        ({"look_amount": 201}, "look_amount"),
    ],
)
def test_creative_look_task_fields_enforce_path_and_mode_safety(
    updates: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        serialize_task_line(
            LightroomTask(r"E:\photos\one.ARW", **updates),
            batch_id="look-safety",
        )


def test_creative_look_descriptor_writer_uses_configured_data_root(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "portable-content" / "state"
    result = write_lightroom_look_descriptor(
        data_dir,
        _compact_look_descriptor(),
        look_uuid="9901408EBF8D496E99EFC526805F2F7C",
    )

    descriptor = Path(result["look_descriptor_path"])
    expected_root = bridge_paths(data_dir).presets / "looks"
    assert descriptor.parent == expected_root
    assert descriptor.name == f"{result['look_descriptor_hash']}.look"


def test_creative_look_task_protocol_carries_only_portable_identity() -> None:
    digest = "a" * 64
    line = serialize_task_line(
        LightroomTask(
            r"D:\photos\one.ARW",
            look_descriptor_hash=digest,
            look_uuid="LOOK-1",
        ),
        batch_id="portable-look",
    )
    parsed = parse_task_line(line)
    assert "look_descriptor_path=" not in line
    assert parsed["look_descriptor_path"] is None
    assert parsed["look_descriptor_hash"] == digest
    assert parsed["look_uuid"] == "LOOK-1"


def test_legacy_creative_look_task_path_is_validated_then_discarded() -> None:
    digest = "a" * 64
    path = f"C:\\PhotoAI\\state\\lightroom-bridge\\presets\\looks\\{digest}.look"
    line = serialize_task_line(
        LightroomTask(
            r"D:\photos\one.ARW",
            look_descriptor_hash=digest,
            look_uuid="LOOK-1",
        ),
        batch_id="legacy-look",
    )
    fields = bridge._parse_line(line, bridge.TASK_PROTOCOL)
    fields["look_descriptor_path"] = path

    parsed = parse_task_line(bridge._line(bridge.TASK_PROTOCOL, fields))

    assert parsed["look_descriptor_path"] is None
    assert parsed["look_descriptor_hash"] == digest


def test_creative_look_batch_publishes_portable_identity_only(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "portable-content" / "state"
    published = write_lightroom_look_descriptor(
        data_dir,
        _compact_look_descriptor(),
        look_uuid="9901408EBF8D496E99EFC526805F2F7C",
    )
    create_lightroom_batch(
        data_dir,
        [
            LightroomTask(
                (tmp_path / "photo.ARW").resolve(),
                task_id="look-1",
                look_descriptor_path=published["look_descriptor_path"],
                look_descriptor_hash=published["look_descriptor_hash"],
                look_uuid=published["look_uuid"],
                look_amount=135,
            )
        ],
        batch_id="portable-look-batch",
    )

    task_path = (
        bridge_paths(data_dir).pending / "portable-look-batch--look-1.task"
    )
    task_line = task_path.read_text(encoding="utf-8")
    parsed = parse_task_line(task_line)

    assert "look_descriptor_path=" not in task_line
    assert parsed["look_descriptor_path"] is None
    assert parsed["look_descriptor_hash"] == published["look_descriptor_hash"]
    assert parsed["look_uuid"] == published["look_uuid"]


def test_creative_look_batch_rejects_another_bridge_root(tmp_path: Path) -> None:
    published = write_lightroom_look_descriptor(
        tmp_path / "first" / "state",
        _compact_look_descriptor(),
        look_uuid="9901408EBF8D496E99EFC526805F2F7C",
    )
    with pytest.raises(ValueError, match="configured bridge root"):
        create_lightroom_batch(
            tmp_path / "second" / "state",
            [
                LightroomTask(
                    (tmp_path / "photo.ARW").resolve(),
                    look_descriptor_path=published["look_descriptor_path"],
                    look_descriptor_hash=published["look_descriptor_hash"],
                    look_uuid=published["look_uuid"],
                )
            ],
            batch_id="wrong-bridge-root",
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"output_mode": "tiff"}, "output_mode"),
        ({"task_type": "preview", "output_mode": "both"}, "preview"),
        ({"preset_uuid": "bad uuid"}, "preset_uuid"),
        ({"preset_uuid": "good-uuid", "preset_scope": "foreign"}, "preset_scope"),
        ({"preset_uuid": "good-uuid", "preset_amount": 201}, "preset_amount"),
    ],
)
def test_new_task_fields_are_validated(
    tmp_path: Path, updates: dict, message: str
) -> None:
    values = {"photo_path": (tmp_path / "photo.ARW").resolve(), **updates}
    with pytest.raises(ValueError, match=message):
        serialize_task_line(LightroomTask(**values), batch_id="batch")


def test_preset_enumeration_task_and_listing_artifact(tmp_path: Path) -> None:
    task_line = serialize_task_line(
        LightroomTask(None, task_type="enumerate_presets", task_id="presets"),
        batch_id="library",
    )
    task = parse_task_line(task_line)
    assert task["task_type"] == "enumerate_presets"
    assert task["photo_path"] == ""

    listing = tmp_path / "library.presets"
    listing.write_text(
        bridge._line(
            PRESET_PROTOCOL,
            {
                "scope": "catalog",
                "uuid": "550e8400-e29b-41d4-a716-446655440000",
                "name": "南京秋色",
                "folder": "用户预设",
                "file": r"E:\Presets\autumn.xmp",
            },
        ),
        encoding="utf-8",
    )
    records = read_lightroom_preset_listing(listing)
    assert records == [
        {
            "scope": "catalog",
            "uuid": "550e8400-e29b-41d4-a716-446655440000",
            "name": "南京秋色",
            "folder": "用户预设",
            "file": r"E:\Presets\autumn.xmp",
        }
    ]

    batch = create_lightroom_preset_enumeration(
        tmp_path / "data", batch_id="preset-sync", task_id="catalog"
    )
    assert batch["status"] == "pending"
    queued = bridge_paths(tmp_path / "data").pending / "preset-sync--catalog.task"
    assert (
        parse_task_line(queued.read_text(encoding="utf-8"))["task_type"]
        == "enumerate_presets"
    )


def test_preview_metadata_repair_task_round_trips_as_an_inert_xmp_operation(
    tmp_path: Path,
) -> None:
    photo = (tmp_path / "repair.ARW").resolve()
    line = serialize_task_line(
        LightroomTask(
            photo,
            task_type="repair_preview_metadata",
            output_mode="xmp",
            rating=0,
            auto_tone=False,
            auto_white_balance=False,
            lens_profile=False,
            remove_chromatic_aberration=False,
            task_id="repair-1",
        ),
        batch_id="repair-batch",
    )

    parsed = parse_task_line(line)
    assert parsed["task_type"] == "repair_preview_metadata"
    assert parsed["output_mode"] == "xmp"
    assert parsed["rating"] == 0
    assert not any(
        parsed[key]
        for key in ("auto_tone", "auto_white_balance", "lens_profile", "remove_ca")
    )
    assert parsed["crop"] == {}
    assert parsed["style"] == {}
    assert parsed["preset_uuid"] is None
    assert parsed["jpeg_output_dir"] is None


@pytest.mark.parametrize(
    "updates",
    [
        {"output_mode": "jpeg"},
        {"rating": 1},
        {"auto_tone": True},
        {"auto_white_balance": True},
        {"lens_profile": True},
        {"remove_chromatic_aberration": True},
        {"crop": {"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9}},
        {"style": {"Exposure2012": 0.2}},
        {"preset_uuid": "good-uuid"},
        {"preset_scope": "plugin"},
        {"preset_amount": 50},
        {
            "look_descriptor_path": "E:\\data\\lightroom-bridge\\presets\\looks\\"
            + "a" * 64
            + ".look",
            "look_descriptor_hash": "a" * 64,
            "look_uuid": "LOOK-1",
        },
        {"jpeg_output_dir": Path("E:/preview-output")},
    ],
)
def test_preview_metadata_repair_rejects_mutating_settings(
    tmp_path: Path,
    updates: dict,
) -> None:
    values = {
        "photo_path": (tmp_path / "repair.ARW").resolve(),
        "task_type": "repair_preview_metadata",
        "rating": 0,
        "auto_tone": False,
        "auto_white_balance": False,
        "lens_profile": False,
        "remove_chromatic_aberration": False,
        **updates,
    }
    with pytest.raises(ValueError, match="repair_preview_metadata"):
        serialize_task_line(LightroomTask(**values), batch_id="repair-batch")


def test_preview_metadata_repair_batch_records_cleanup_count(tmp_path: Path) -> None:
    data_dir = tmp_path / "on-e-drive"
    photos = [(tmp_path / "one.ARW").resolve(), (tmp_path / "two.NEF").resolve()]
    batch = create_lightroom_preview_metadata_repair(
        data_dir,
        photos,
        batch_id="repair-preview-metadata",
    )
    paths = bridge_paths(data_dir)

    assert batch["status"] == "pending"
    assert batch["task_count"] == 2
    for index, photo in enumerate(photos, start=1):
        task_id = f"repair-{index:06d}"
        queued = paths.pending / f"repair-preview-metadata--{task_id}.task"
        parsed = parse_task_line(queued.read_text(encoding="utf-8"))
        assert parsed["task_type"] == "repair_preview_metadata"
        assert parsed["photo_path"] == str(photo)

    first = paths.pending / "repair-preview-metadata--repair-000001.task"
    first.unlink()
    (paths.done / "repair-preview-metadata--repair-000001.result").write_text(
        bridge._line(
            RESULT_PROTOCOL,
            {
                "batch_id": "repair-preview-metadata",
                "task_id": "repair-000001",
                "photo_path": str(photos[0]),
                "status": "done",
                "finished_at": "2026-09-01T10:00:00Z",
                "message": "",
                "cleanup_count": 3,
            },
        ),
        encoding="utf-8",
    )
    status = read_lightroom_batch_status(data_dir, "repair-preview-metadata")
    assert status["tasks"][0]["result"]["cleanup_count"] == 3


def test_transient_snapshot_cleanup_task_is_exact_inert_and_idempotent(
    tmp_path: Path,
) -> None:
    photo = (tmp_path / "final.ARW").resolve()
    line = serialize_task_line(
        LightroomTask(
            photo,
            task_type="cleanup_transient_snapshot",
            output_mode="xmp",
            rating=0,
            auto_tone=False,
            auto_white_balance=False,
            lens_profile=False,
            remove_chromatic_aberration=False,
            source_batch_id="export-source-1",
            source_task_id="photo-000004",
        ),
        batch_id="snapshot-cleanup-1",
        task_id="cleanup-000001",
    )
    parsed = parse_task_line(line)
    assert parsed["task_type"] == "cleanup_transient_snapshot"
    assert parsed["source_batch_id"] == "export-source-1"
    assert parsed["source_task_id"] == "photo-000004"
    assert parsed["output_mode"] == "xmp"
    assert parsed["crop"] == {}
    assert parsed["style"] == {}
    assert not any(
        parsed[key]
        for key in ("auto_tone", "auto_white_balance", "lens_profile", "remove_ca")
    )

    batch = create_lightroom_transient_snapshot_cleanup(
        tmp_path / "data",
        [
            {
                "photo_path": photo,
                "source_batch_id": "export-source-1",
                "source_task_id": "photo-000004",
            }
        ],
        batch_id="snapshot-cleanup-1",
    )
    queued = (
        bridge_paths(tmp_path / "data").pending
        / "snapshot-cleanup-1--cleanup-000001.task"
    )
    assert batch["task_count"] == 1
    assert (
        parse_task_line(queued.read_text(encoding="utf-8"))["source_task_id"]
        == "photo-000004"
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"source_batch_id": None},
        {"source_task_id": None},
        {"source_batch_id": "style-source"},
        {"source_task_id": "candidate-1"},
        {"rating": 1},
        {"auto_tone": True},
        {"output_mode": "jpeg"},
        {"style": {"Exposure2012": 1}},
    ],
)
def test_transient_snapshot_cleanup_rejects_ambiguous_or_mutating_tasks(
    tmp_path: Path,
    updates: dict,
) -> None:
    values = {
        "photo_path": (tmp_path / "final.ARW").resolve(),
        "task_type": "cleanup_transient_snapshot",
        "output_mode": "xmp",
        "rating": 0,
        "auto_tone": False,
        "auto_white_balance": False,
        "lens_profile": False,
        "remove_chromatic_aberration": False,
        "source_batch_id": "export-source-1",
        "source_task_id": "photo-000004",
        **updates,
    }
    with pytest.raises(ValueError, match="cleanup_transient_snapshot"):
        serialize_task_line(LightroomTask(**values), batch_id="snapshot-cleanup-1")


def test_task_validation_rejects_partial_or_inverted_crop(tmp_path: Path) -> None:
    photo = (tmp_path / "photo.ARW").resolve()
    for crop in (
        {"left": 0.1},
        {"left": 0.9, "top": 0.0, "right": 0.1, "bottom": 1.0},
    ):
        try:
            serialize_task_line(LightroomTask(photo, crop=crop), batch_id="batch")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid crop was accepted")


def test_create_batch_and_aggregate_done_and_failed_results(tmp_path: Path) -> None:
    data_dir = tmp_path / "app-data"
    batch = create_lightroom_batch(
        data_dir,
        [
            LightroomTask((tmp_path / "a.ARW").resolve(), rating=5, task_id="a"),
            LightroomTask((tmp_path / "b.ARW").resolve(), rating=3, task_id="b"),
        ],
        batch_id="run-1",
    )
    paths = bridge_paths(data_dir)
    assert batch["status"] == "pending"
    assert batch["counts"]["pending"] == 2
    assert paths.backups == paths.root / "backups"
    assert paths.backups.is_dir()
    assert paths.previews.is_dir()
    assert paths.exports.is_dir()
    assert paths.presets.is_dir()
    assert all(path.is_relative_to(paths.root) for path in paths.root.rglob("*"))

    first = paths.pending / "run-1--a.task"
    first.unlink()
    (paths.done / "run-1--a.result").write_text(
        bridge._line(
            RESULT_PROTOCOL,
            {
                "batch_id": "run-1",
                "task_id": "a",
                "photo_path": str((tmp_path / "a.ARW").resolve()),
                "status": "done",
                "finished_at": "2026-09-01T10:00:00Z",
                "message": "",
                "isolation_kind": "virtual_copy",
                "isolation_status": "removed",
                "source_uuid": "source-uuid",
                "working_uuid": "working-uuid",
            },
        ),
        encoding="utf-8",
    )
    second = paths.pending / "run-1--b.task"
    second.unlink()
    (paths.failed / "run-1--b.result").write_text(
        bridge._line(
            RESULT_PROTOCOL,
            {
                "batch_id": "run-1",
                "task_id": "b",
                "photo_path": str((tmp_path / "b.ARW").resolve()),
                "status": "failed",
                "finished_at": "2026-09-01T10:00:01Z",
                "message": "catalog locked",
            },
        ),
        encoding="utf-8",
    )

    status = read_lightroom_batch_status(data_dir, "run-1")
    assert status["status"] == "failed"
    assert status["completed_count"] == 2
    assert status["counts"]["done"] == 1
    assert status["counts"]["failed"] == 1
    first_result = status["tasks"][0]["result"]
    assert first_result["isolation_kind"] == "virtual_copy"
    assert first_result["isolation_status"] == "removed"
    assert first_result["source_uuid"] == "source-uuid"
    assert first_result["working_uuid"] == "working-uuid"
    assert status["tasks"][1]["result"]["message"] == "catalog locked"


def test_cancel_marker_is_atomic_and_batch_drains_to_cancelled(tmp_path: Path) -> None:
    data_dir = tmp_path / "app-data"
    create_lightroom_batch(
        data_dir,
        [
            LightroomTask((tmp_path / "a.ARW").resolve(), task_id="a"),
            LightroomTask((tmp_path / "b.ARW").resolve(), task_id="b"),
        ],
        batch_id="cancel-me",
    )
    paths = bridge_paths(data_dir)

    requested = cancel_lightroom_batch(data_dir, "cancel-me", reason="first request")
    marker = paths.cancelled / "cancel-me.cancel"
    first_contents = marker.read_bytes()
    assert requested["status"] == "cancelling"
    assert requested["counts"]["pending"] == 2
    assert requested["cancellation"]["reason"] == "first request"

    # An idempotent second request must not replace the first durable marker.
    cancel_lightroom_batch(data_dir, "cancel-me", reason="later request")
    assert marker.read_bytes() == first_contents

    for task_id in ("a", "b"):
        pending = paths.pending / f"cancel-me--{task_id}.task"
        pending.unlink()
        (paths.cancelled / f"cancel-me--{task_id}.result").write_text(
            bridge._line(
                bridge.RESULT_PROTOCOL,
                {
                    "batch_id": "cancel-me",
                    "task_id": task_id,
                    "photo_path": str((tmp_path / f"{task_id}.ARW").resolve()),
                    "status": "cancelled",
                    "finished_at": "2026-09-01T10:00:00Z",
                    "message": "batch cancelled",
                },
            ),
            encoding="utf-8",
        )

    final = read_lightroom_batch_status(data_dir, "cancel-me")
    assert final["status"] == "cancelled"
    assert final["completed_count"] == 2
    assert final["counts"]["cancelled"] == 2
    assert all(task["status"] == "cancelled" for task in final["tasks"])


def test_pre_cancelled_batch_id_cannot_publish_tasks(tmp_path: Path) -> None:
    data_dir = tmp_path / "app-data"
    cancel_lightroom_batch(
        data_dir, "never-publish", reason="cancelled before worker start"
    )

    with pytest.raises(RuntimeError, match="cancelled before publication"):
        create_lightroom_batch(
            data_dir,
            [LightroomTask((tmp_path / "photo.ARW").resolve(), task_id="photo")],
            batch_id="never-publish",
        )

    paths = bridge_paths(data_dir)
    assert not list(paths.pending.glob("never-publish--*.task"))
    assert not (paths.batches / "never-publish.json").exists()


def test_plugin_config_and_stale_heartbeat_remain_under_data_dir(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "photo-ai-lightroom.lrplugin"
    plugin.mkdir()
    (plugin / "Info.lua").write_text("return {}", encoding="utf-8")
    data_dir = tmp_path / "on-e-drive"

    config = write_lightroom_plugin_config(data_dir, plugin_dir=plugin)
    paths = bridge_paths(data_dir)
    assert Path(config["config_file"]).is_relative_to(paths.root)
    pointer_lines = (
        (plugin / "bridge-path.txt").read_text(encoding="utf-8").splitlines()
    )
    assert pointer_lines == [str(paths.root), str(data_dir.resolve() / "style-library")]

    then = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    paths.heartbeat.write_text(
        bridge._line(
            HEARTBEAT_PROTOCOL,
            {"at": then.isoformat(), "state": "running", "plugin_version": "0.1.0"},
        ),
        encoding="utf-8",
    )
    status = read_lightroom_bridge_status(
        data_dir,
        now=then + timedelta(seconds=20),
        stale_after_seconds=15,
    )
    assert status["configured"] is True
    assert status["heartbeat"]["state"] == "stale"
    assert status["heartbeat"]["age_seconds"] == 20
    assert "batches" in status

    compact = read_lightroom_bridge_status(
        data_dir,
        include_batches=False,
        now=then + timedelta(seconds=20),
        stale_after_seconds=15,
    )
    assert "batches" not in compact


def test_plugin_config_uses_content_root_state_and_styles(
    tmp_path: Path, monkeypatch
) -> None:
    content_root = (tmp_path / "portable").resolve()
    data_dir = content_root / "state"
    styles_dir = content_root / "styles"
    data_dir.mkdir(parents=True)
    styles_dir.mkdir()
    plugin = tmp_path / "installed" / "PhotoAI.lrplugin"
    plugin.mkdir(parents=True)
    (plugin / "Info.lua").write_text("return {}", encoding="utf-8")
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(content_root))
    monkeypatch.setenv("PHOTO_AI_STYLES_DIR", str(styles_dir))

    config = write_lightroom_plugin_config(data_dir, plugin_dir=plugin)
    lines = (plugin / "bridge-path.txt").read_text(encoding="utf-8").splitlines()
    assert lines == [
        str(data_dir / "lightroom-bridge"),
        str(styles_dir / "style-library"),
    ]
    assert config["style_root"] == str(styles_dir / "style-library")


def test_default_plugin_dir_prefers_installed_lightroom_module(
    tmp_path: Path, monkeypatch
) -> None:
    appdata = tmp_path / "appdata"
    installed = appdata / "Adobe" / "Lightroom" / "Modules" / "PhotoAI.lrplugin"
    installed.mkdir(parents=True)
    (installed / "Info.lua").write_text("return {}", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("PHOTO_AI_LIGHTROOM_PLUGIN_DIR", raising=False)

    assert bridge._default_plugin_dir() == installed.resolve()


def test_installed_plugin_is_owned_and_uninstall_preserves_modifications(
    tmp_path: Path, monkeypatch
) -> None:
    appdata = tmp_path / "appdata"
    template = tmp_path / "template.lrplugin"
    template.mkdir()
    (template / "Info.lua").write_text("return { VERSION = 1 }", encoding="utf-8")
    (template / "Bridge.lua").write_text("return true", encoding="utf-8")
    (template / "bridge-path.txt").write_text(r"E:\developer", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("PHOTO_AI_LIGHTROOM_PLUGIN_DIR", raising=False)

    installed = install_lightroom_plugin(template)
    plugin = Path(installed["plugin_dir"])
    assert plugin.is_dir()
    assert not (plugin / "bridge-path.txt").exists()
    assert (plugin / ".photoai-installed.json").is_file()

    (plugin / "Bridge.lua").write_text("-- user modification", encoding="utf-8")
    preserved = remove_owned_lightroom_plugin()
    assert preserved["removed"] is False
    assert preserved["reason"] == "modified"
    assert plugin.is_dir()

    (plugin / "Bridge.lua").write_text("return true", encoding="utf-8")
    removed = remove_owned_lightroom_plugin()
    assert removed["removed"] is True
    assert not plugin.exists()


def test_detects_lightroom_at_plugin_minimum_and_all_newer_versions(
    tmp_path: Path, monkeypatch
) -> None:
    install = tmp_path / "Adobe Lightroom Classic"
    install.mkdir()
    executable = install / "Lightroom.exe"
    executable.write_bytes(b"not a real executable")

    monkeypatch.setattr(bridge, "_file_version", lambda path: (14, 3, 1, 2))
    assert detect_lightroom_classic_15_3([tmp_path]) == executable.resolve()

    monkeypatch.setattr(bridge, "_file_version", lambda path: (14, 2, 0, 0))
    assert detect_lightroom_classic_15_3([tmp_path]) is None

    monkeypatch.setattr(bridge, "_file_version", lambda path: (15, 9, 0, 0))
    assert detect_lightroom_classic_15_3([tmp_path]) == executable.resolve()

    monkeypatch.setattr(bridge, "_file_version", lambda path: (16, 0, 0, 0))
    assert detect_lightroom_classic_15_3([tmp_path]) == executable.resolve()

    status = bridge.lightroom_classic_status([tmp_path])
    assert status["required_version"] == ">=14.3"
    assert status["compatible"] is True
    assert status["support_level"] == "allowed_untested"


def test_plugin_uses_object_addressed_auto_settings_and_unique_snapshot() -> None:
    plugin_source = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
        / "Bridge.lua"
    )
    text = plugin_source.read_text(encoding="utf-8")
    assert 'SNAPSHOT_PREFIX = "照片选片 · 处理前"' in text
    assert "LrDevelopController" not in text
    assert "settings.AutoTone = true" in text
    assert 'settings.WhiteBalance = "Auto"' in text
    assert 'photo:applyDevelopSettings(settings, "照片选片 · 自动调整", true)' in text
    assert 'if task.task_type ~= "preview" then' in text
    rating_write = 'photo:setRawMetadata("rating", task.rating)'
    assert rating_write in text
    preview_guard = text.index('if task.task_type ~= "preview" then')
    assert preview_guard < text.index(rating_write, preview_guard)
    assert "photo:saveMetadata()" in text
    assert "photo:createDevelopSnapshot(snapshot_name, false)" in text
    assert "target=object-addressed" in text
    apply_start = text.index("function Bridge:apply_develop_settings")
    apply_end = text.index("function Bridge:save_xmp", apply_start)
    apply_body = text[apply_start:apply_end]
    assert "LrApplicationView" not in apply_body
    assert "getTargetPhoto" not in apply_body
    assert "setSelectedPhotos" not in apply_body
    assert "os.remove" not in text
    assert "os.rename" not in text
    assert "LrFileUtils.delete(path)" in text
    assert "LrFileUtils.move(temporary, path)" in text


def test_plugin_error_boundaries_allow_lightroom_tasks_to_yield() -> None:
    plugin_source = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
        / "Bridge.lua"
    )
    text = plugin_source.read_text(encoding="utf-8")

    # Lightroom catalog writes, metadata saves and XMP stabilization can yield.
    # Standard Lua pcall cannot cross those coroutine boundaries.
    assert text.count("local ok, err = LrTasks.pcall(function()") >= 2
    assert "local snapshot_ok, snapshot_error = LrTasks.pcall(function()" in text
    assert "local settings_ok, settings_error = LrTasks.pcall(function()" in text
    assert "local saved, save_error = LrTasks.pcall(function()" in text
    assert "local backup_ok, backup_or_error = LrTasks.pcall(function()" in text


def test_plugin_waits_for_lightroom_15_async_xmp_queue() -> None:
    plugin_source = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
        / "Bridge.lua"
    )
    text = plugin_source.read_text(encoding="utf-8")

    assert "local SIDECAR_TIMEOUT_POLLS = 4800" in text
    assert "local SIDECAR_SAVE_RETRY_POLLS = 80" in text
    assert (
        "local retry_ok, retry_error = LrTasks.pcall(function() photo:saveMetadata() end)"
        in text
    )
    assert "within 20 minutes" in text


def test_plugin_reload_clears_bridge_module_cache() -> None:
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    init_source = (plugin_root / "Init.lua").read_text(encoding="utf-8")

    assert 'package.loaded["Bridge"] = nil' in init_source
    assert init_source.index('package.loaded["Bridge"] = nil') < init_source.index(
        'require "Bridge"'
    )


def test_plugin_contributes_menu_item_so_force_init_runs_at_startup() -> None:
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    info_source = (plugin_root / "Info.lua").read_text(encoding="utf-8")
    menu_source = (plugin_root / "OpenTool.lua").read_text(encoding="utf-8")
    provider_source = (plugin_root / "PluginInfoProvider.lua").read_text(
        encoding="utf-8"
    )

    assert "LrForceInitPlugin = true" in info_source
    assert "LrLibraryMenuItems" in info_source
    assert 'file = "OpenTool.lua"' in info_source
    assert 'LrHttp.openUrlInBrowser("photoai://open")' in menu_source
    assert "127.0.0.1:8765" not in menu_source
    assert not (plugin_root / "bridge-path.txt").exists()
    assert "预览使用并删除隔离虚拟副本；正式保存才建立快照并写入 XMP" in provider_source


def test_plugin_enforces_raw_xmp_and_crash_safety_protocol() -> None:
    plugin_source = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
        / "Bridge.lua"
    )
    text = plugin_source.read_text(encoding="utf-8")

    for extension in ("arw", "cr2", "cr3", "nef", "nrw", "raf", "orf", "rw2", "pef"):
        assert f"{extension} = true" in text
    assert "unsupported camera RAW extension for Lightroom XMP" in text
    assert "LrFileUtils.fileAttributes(path)" in text
    assert "attributes.fileSize" in text
    assert "attributes.fileModificationDate" in text
    assert "contents ~= before_contents" in text
    assert "SIDECAR_STABLE_POLLS = 3" in text

    assert 'XMP_BACKUP_PROTOCOL = "PHOTO_AI_LR_XMP_BACKUP/1"' in text
    assert "atomic_create(backup_path, contents)" in text
    assert "context.pre_sidecar_contents = contents" in text
    assert "contents ~= before_contents" in text
    assert "contents == stable_contents" in text
    assert (
        'require_xmp_number(context.final_sidecar_contents, "xmp:Rating", task.rating, 0)'
        in text
    )
    assert 'require_xmp_number(context.final_sidecar_contents, "crs:CropLeft"' in text
    assert (
        'require_xmp_string(context.final_sidecar_contents, "crs:CameraProfile"' in text
    )
    assert (
        "require_xmp_profile_amount(context.final_sidecar_contents, task.style.ProfileAmount)"
        in text
    )
    assert "XMP verification failed for" in text
    assert "self:restore_sidecar_backup(context)" in text
    assert "function Bridge:sidecar_is_unchanged(context)" in text
    assert "current ~= context.pre_sidecar_contents" in text
    assert "xmp_unchanged=preserved-in-place" in text
    assert "xmp_backup_state=" in text
    assert "refusing to overwrite safety record" in text

    assert "function Bridge:recover_running_tasks()" in text
    assert (
        "plugin restarted while task was running; previous Lightroom operation is indeterminate"
        in text
    )
    assert "local entries = collect_task_entries(self.paths.pending)" in text
    assert "local entries = collect_task_entries(self.paths.running)" in text
    assert "for path in LrFileUtils.directoryEntries(directory) do" in text


def test_plugin_honors_cancel_marker_and_rolls_back_explicitly() -> None:
    plugin_source = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
        / "Bridge.lua"
    )
    text = plugin_source.read_text(encoding="utf-8")

    assert 'cancelled = LrPathUtils.child(pointer, "cancelled")' in text
    assert 'task.batch_id .. ".cancel"' in text
    assert 'status == "cancelled"' in text
    assert 'cancelled and "cancelled" or "failed"' in text
    assert text.count("ensure_not_cancelled(") >= 15
    assert "photo:applyDevelopSnapshot(context.snapshot_id)" in text
    assert "photo:applyDevelopSettings(context.original_settings" in text
    assert "rollback=restored-from-" in text
    assert '"catalog_path=" .. encode_scalar(catalog_path)' in text


def test_plugin_supports_exact_presets_transient_previews_and_separate_outputs() -> (
    None
):
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    plugin_source = plugin_root / "Bridge.lua"
    text = plugin_source.read_text(encoding="utf-8")
    info = (plugin_root / "Info.lua").read_text(encoding="utf-8")

    assert bridge.PLUGIN_VERSION == "0.3.8"
    assert "LrSdkMinimumVersion = 14.3" in info
    assert "VERSION = { major = 0, minor = 3, revision = 8, build = 0 }" in info
    assert 'local LrExportSession = import "LrExportSession"' in text
    assert "LrApplication.developPresetByUuid(task.preset_uuid)" in text
    assert "LrApplication.getDevelopPresetsForPlugin(_PLUGIN, task.preset_uuid)" in text
    assert "photo:applyDevelopPreset(preset, _PLUGIN, task.preset_amount, true)" in text
    assert "photo:applyDevelopPreset(preset, nil, task.preset_amount, true)" in text
    assert "LrApplication.developPresetFolders()" in text
    assert "PRESET_PROTOCOL" in text
    assert "LrApplication.versionTable()" in text
    assert '"lightroom_version=" .. encode_scalar(lightroom_version)' in text

    assert 'LR_format = "JPEG"' in text
    assert "LR_jpeg_quality = 0.9" in text
    assert 'LR_export_colorSpace = "sRGB"' in text
    assert "LR_size_doConstrain = false" in text
    assert 'LR_outputSharpeningMedia = "screen"' in text
    assert "LR_outputSharpeningLevel = 2" in text
    assert 'LR_collisionHandling = "rename"' in text
    assert "export_settings.LR_size_maxWidth = 1024" in text
    assert "export_settings.LR_size_maxHeight = 1024" in text
    assert "rendition:waitForRender()" in text

    assert 'context.transient = task.output_mode == "jpeg"' in text
    assert 'context.result.restore_status = restored and "done" or "failed"' in text
    assert "rollback=not-run-after-successful-xmp-commit" in text
    assert "function Bridge:delete_transient_snapshot(photo, context)" in text
    assert 'local LrApplicationView = import "LrApplicationView"' in text
    assert "local primary_id = target.id_global or target.snapshotID" in text
    assert 'switch_module("develop"' in text
    assert "for poll = 1, 100 do" in text
    assert (
        "temporary Lightroom snapshot still exists after bounded cleanup wait" in text
    )
    assert "temporary_snapshot=deleted" in text
    for field in (
        "xmp_status",
        "jpeg_status",
        "restore_status",
        "isolation_kind",
        "isolation_status",
        "source_uuid",
        "working_uuid",
        "preset_list_path",
    ):
        assert f'"{field}"' in text


def test_plugin_applies_data_only_creative_look_and_strictly_verifies_it() -> None:
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    text = (plugin_root / "Bridge.lua").read_text(encoding="utf-8")

    assert 'local LrDigest = import "LrDigest"' in text
    assert 'LOOK_DESCRIPTOR_PROTOCOL = "PHOTO_AI_LR_LOOK/1"' in text
    assert (
        'looks = LrPathUtils.child(LrPathUtils.child(pointer, "presets"), "looks")'
        in text
    )
    assert (
        "local descriptor_path = LrPathUtils.child(self.paths.looks, expected_leaf)"
        in text
    )
    assert (
        "path_key(LrPathUtils.parent(descriptor_path)) ~= path_key(self.paths.looks)"
        in text
    )
    assert "task.look_descriptor_path" not in text
    assert "legacy_look_descriptor_path = fields.look_descriptor_path" in text
    assert "LrDigest.SHA256.digest(contents)" in text
    assert "actual_hash:lower() ~= task.look_descriptor_hash:lower()" in text
    assert "parse_look_descriptor(contents, task.look_uuid)" in text
    assert "loadfile" not in text
    assert "dofile" not in text
    assert (
        'photo:applyDevelopSettings({ Look = look }, "照片选片 · 创意外观", true)'
        in text
    )
    assert "local settings = photo:getDevelopSettings()" in text
    assert "actual.UUID ~= task.look_uuid" in text
    assert "math.abs(actual_amount - expected_amount) > 0.0001" in text
    for field in ("look_status", "look_uuid", "look_amount"):
        assert f'"{field}"' in text


def test_plugin_preview_uses_verified_virtual_copy_isolation_only() -> None:
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    text = (plugin_root / "Bridge.lua").read_text(encoding="utf-8")

    assert 'local LrSelection = import "LrSelection"' in text
    assert 'PREVIEW_COPY_PROTOCOL = "PHOTO_AI_LR_PREVIEW_COPY/1"' in text
    assert 'PREVIEW_COPY_PREFIX = "照片选片 · 隔离预览"' in text
    assert "catalog:createVirtualCopies(context.preview_copy_name)" in text
    assert "LrSelection.removeFromCatalog()" in text
    assert (
        "self:restore_selection(catalog, { active = photo, others = {} }, phase)"
        in text
    )
    assert "catalog:getActiveSources()" in text
    assert "catalog:getCurrentViewFilter()" in text
    assert "catalog:setActiveSources(catalog.kAllPhotos)" in text
    assert "catalog:setViewFilter(open_filter)" in text
    assert "self:restore_preview_library_context(" in text
    assert "self:restore_saved_preview_selection(" in text
    assert "function Bridge:restore_selection(catalog, selection, phase)" in text
    restore_selection_start = text.index(
        "function Bridge:restore_selection(catalog, selection, phase)"
    )
    restore_selection_end = text.index(
        "function Bridge:restore_saved_preview_selection", restore_selection_start
    )
    restore_selection_body = text[restore_selection_start:restore_selection_end]
    assert "for poll = 1, 40 do" in restore_selection_body
    assert "LrTasks.sleep(0.05)" in restore_selection_body
    assert "self:verify_selection(catalog, selection, phase)" in restore_selection_body
    assert "Lightroom selection did not stabilize" in restore_selection_body
    select_only_start = text.index("function Bridge:select_only(catalog, photo, phase)")
    select_only_end = text.index(
        "function Bridge:restore_selection(catalog, selection, phase)",
        select_only_start,
    )
    select_only_body = text[select_only_start:select_only_end]
    assert (
        "self:restore_selection(catalog, { active = photo, others = {} }, phase)"
        in select_only_body
    )
    assert "function Bridge:assert_preview_catalog_identity(context, phase)" in text
    assert (
        "function Bridge:verify_preview_single_selection(context, catalog, photo, phase)"
        in text
    )

    creation_start = text.index("function Bridge:create_preview_virtual_copy")
    creation_end = text.index(
        "function Bridge:remove_preview_virtual_copy", creation_start
    )
    creation_body = text[creation_start:creation_end]
    create_selection_check = creation_body.index(
        "self:verify_preview_single_selection("
    )
    create_call = creation_body.index(
        "catalog:createVirtualCopies(context.preview_copy_name)"
    )
    assert create_selection_check < create_call
    between_selection_and_create = creation_body[create_selection_check:create_call]
    for yielding_operation in (
        "ensure_not_cancelled",
        "findPhotoByPath",
        "findPhotoByUuid",
        "atomic_create",
        "LrTasks.sleep",
    ):
        assert yielding_operation not in between_selection_and_create
    assert (
        '"immediately before isolated preview creation"' in between_selection_and_create
    )
    creation_restore = creation_body.index(
        "self:restore_saved_preview_selection(", create_call
    )
    assert create_call < creation_restore

    removal_start = text.index("function Bridge:remove_preview_virtual_copy")
    removal_end = text.index("function Bridge:find_or_import", removal_start)
    removal_body = text[removal_start:removal_end]
    assert "ensure_not_cancelled" not in removal_body
    assert "LrSelection.removeFromCatalog()" in removal_body
    assert "catalog:findPhotoByUuid(context.working_uuid) == nil" in removal_body
    assert "for poll = 1, 40 do" in removal_body
    assert "if poll < 40 then LrTasks.sleep(0.05) end" in removal_body
    assert "preview_isolation=removed-after-sdk-error" in removal_body
    assert "isolated virtual copy still exists after removal" not in removal_body
    assert "self:archive_preview_copy_record" in removal_body
    final_selection_check = removal_body.index("self:verify_preview_single_selection(")
    remove_call = removal_body.index("LrSelection.removeFromCatalog()")
    assert final_selection_check < remove_call
    between_selection_and_remove = removal_body[final_selection_check:remove_call]
    assert "ensure_not_cancelled" not in between_selection_and_remove
    assert "createVirtualCopies" not in between_selection_and_remove
    after_remove = removal_body[remove_call:]
    assert '"after isolated preview removal"' in after_remove
    assert "self:assert_master_identity(" in after_remove
    assert "self:assert_preview_catalog_identity(" in after_remove
    assert "removal_verification_failed=" in removal_body
    assert "orphan_state=retained" in removal_body
    capture_prior = removal_body.index(
        "local prior_selection = context.selection_restore_override"
    )
    persist_prior = removal_body.index(
        "context.selection_restore_override = prior_selection"
    )
    destructive_remove = removal_body.index("LrSelection.removeFromCatalog()")
    assert capture_prior < persist_prior < destructive_remove
    identity_unknown = removal_body.index('"preview_isolation=identity-unknown')
    assert (
        removal_body.index("self:restore_saved_preview_selection(") < identity_unknown
    )
    main_restore = removal_body.index(
        "local restore_ok, restore_error = self:restore_preview_library_context("
    )
    main_archive = removal_body.index("if actually_removed and restore_ok then")
    assert main_restore < main_archive
    restore_body = removal_body[main_restore:main_archive]
    assert "prior_selection" in restore_body
    assert '"isolated preview removal restore"' in restore_body
    removal_loop = removal_body[remove_call:main_restore]
    assert removal_loop.index("for poll = 1, 40 do") < removal_loop.index(
        "return false"
    )
    assert removal_body.index(
        "if actually_removed and restore_ok then"
    ) < removal_body.index("if not operation_ok and not actually_removed then")

    identity_start = text.index("function Bridge:assert_preview_copy_identity")
    identity_end = text.index(
        "function Bridge:assert_working_photo_identity", identity_start
    )
    identity_body = text[identity_start:identity_end]
    for proof in (
        'getRawMetadata("isVirtualCopy")',
        'getRawMetadata("masterPhoto")',
        'getRawMetadata("path")',
        'getRawMetadata("uuid")',
        "catalog:findPhotoByUuid(context.working_uuid)",
        'getFormattedMetadata("copyName")',
        "self:assert_master_identity",
    ):
        assert proof in identity_body

    isolated_start = text.index("function Bridge:process_isolated_preview")
    isolated_end = text.index("function Bridge:process(running_path)", isolated_start)
    isolated_body = text[isolated_start:isolated_end]
    assert "self:find_existing_master(task, context)" in isolated_body
    assert "self:create_preview_virtual_copy(master, task, context)" in isolated_body
    assert "self:remove_preview_virtual_copy(context)" in isolated_body
    assert 'context.result.isolation_kind = "virtual_copy"' in isolated_body
    assert (
        'context.result.isolation_status = context.preview_copy_removed and "removed"'
        in isolated_body
    )
    assert "context.result.source_uuid = context.source_uuid" in isolated_body
    assert "context.result.working_uuid = context.working_uuid" in isolated_body
    for forbidden in (
        "backup_sidecar",
        "create_snapshot",
        "save_xmp",
        "saveMetadata",
        "find_or_import",
        "setRawMetadata",
        "rollback",
    ):
        assert forbidden not in isolated_body

    process_start = text.index("function Bridge:process(running_path)")
    process_end = text.index(
        "function Bridge:recover_active_preview_copy", process_start
    )
    process_body = text[process_start:process_end]
    preview_branch = process_body.index('if task.task_type == "preview" then')
    assert preview_branch < process_body.index("context.sidecar_path")
    assert preview_branch < process_body.index("self:backup_sidecar(task, context)")
    assert "self:process_isolated_preview(task, context)" in process_body
    assert 'context.preview_isolated and "target=isolated-virtual-copy"' in process_body
    assert "local cancelled = tostring(err):find(CANCEL_ERROR" in process_body
    assert "return self:remove_preview_virtual_copy(context)" in process_body
    assert (
        "local cleanup_call_ok, removed_or_error, cleanup_message = LrTasks.pcall(function()"
        in process_body
    )
    assert "context.preview_started_path" in process_body
    assert "preview_isolation=cleanup-exception" in process_body


def test_plugin_preview_orphan_recovery_requires_durable_uuid_and_restores_selection() -> (
    None
):
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    text = (plugin_root / "Bridge.lua").read_text(encoding="utf-8")

    assert 'stem .. ".intent"' in text
    assert 'stem .. ".started"' in text
    assert 'stem .. ".selection-restore-attempted"' in text
    assert 'stem .. ".state"' in text
    assert 'stem .. ".removed"' in text
    assert "atomic_create(paths.intent, preview_copy_record_line(record))" in text
    assert (
        "atomic_create(paths.started, preview_copy_record_line(started_record))" in text
    )
    assert (
        "atomic_create(paths.active, preview_copy_record_line(active_record))" in text
    )
    started_write = text.index(
        "atomic_create(paths.started, preview_copy_record_line(started_record))"
    )
    create_copy = text.index("catalog:createVirtualCopies(context.preview_copy_name)")
    assert started_write < create_copy
    assert "function Bridge:recover_preview_copy_states()" in text
    assert "self:recover_preview_copy_states()" in text
    assert "function Bridge:recover_started_preview_copy(path)" in text
    assert "function Bridge:recover_preview_selection_attempt(path)" in text
    assert 'collect_suffix_entries(self.paths.backups, ".preview-copy.started")' in text
    assert '".preview-copy.selection-restore-attempted"' in text
    assert "catalog:findPhotoByUuid(record.source_uuid)" in text
    assert "working_uuid = record.working_uuid" in text
    assert "catalog:findPhotoByUuid(context.working_uuid)" in text
    assert (
        "legacy preview intent may have started creation but has no verified working UUID; "
        in text
    )
    assert '"retained; " .. selection_note' in text
    assert (
        "preview creation may have started before its exact working UUID was durable"
        in text
    )
    assert "started/removed preview recovery convergence" in text
    assert "intent/" in text and "preview recovery convergence" in text
    assert "preview orphan belongs to another catalog; retained" in text
    assert "preview started record belongs to another catalog; retained" in text
    assert "preview intent belongs to another catalog; retained" in text
    assert "LrFileUtils.exists(paths.started)" in text
    assert "record_preview_selection_restore_attempt" in text
    assert (
        "prior selection-restore attempt marker found; selection not changed again"
        in text
    )
    assert "selection_from_record(catalog, record, true)" in text
    one_time_start = text.index(
        "function Bridge:restore_ambiguous_preview_selection_once"
    )
    one_time_end = text.index(
        "function Bridge:assert_preview_copy_identity", one_time_start
    )
    one_time_body = text[one_time_start:one_time_end]
    prior_attempt = one_time_body.index("self:preview_selection_restore_was_attempted(")
    reconstruct = one_time_body.index(
        "self:selection_from_record(catalog, record, true)"
    )
    mark_attempt = one_time_body.index("self:record_preview_selection_restore_attempt(")
    restore_once = one_time_body.index("self:restore_saved_preview_selection(")
    assert prior_attempt < reconstruct < mark_attempt < restore_once
    active_recovery = text[
        text.index("function Bridge:recover_active_preview_copy") : text.index(
            "function Bridge:recover_started_preview_copy"
        )
    ]
    remove_recovered = active_recovery.index(
        "self:remove_preview_virtual_copy(context)"
    )
    missing_report = active_recovery.index("saved selection member(s) no longer exist")
    assert remove_recovered < missing_report
    assert "LrTasks.pcall(function() self:recover_running_tasks() end)" in text


def test_plugin_preview_metadata_repair_is_catalog_only_and_narrowly_scoped() -> None:
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    text = (plugin_root / "Bridge.lua").read_text(encoding="utf-8")

    lookup_start = text.index("function Bridge:find_existing_photo(task)")
    lookup_end = text.index("function Bridge:find_or_import", lookup_start)
    lookup_body = text[lookup_start:lookup_end]
    assert "findPhotoByPath(task.photo_path)" in lookup_body
    assert "refuses to import" in lookup_body
    assert "addPhoto" not in lookup_body

    matcher_start = text.index("local function bridge_snapshot_identity(snapshot)")
    matcher_end = text.index(
        "function Bridge:delete_style_preview_snapshots", matcher_start
    )
    matcher_body = text[matcher_start:matcher_end]
    assert "SNAPSHOT_PREFIX .. delimiter" in matcher_body
    assert 'parsed.batch_id:sub(1, 6) ~= "style-"' in matcher_body
    assert "valid_identifier(batch_id)" in matcher_body
    assert "valid_identifier(task_id)" in matcher_body

    repair_start = text.index("function Bridge:repair_preview_metadata(task, context)")
    repair_end = text.index("function Bridge:wait_for_exact_sidecar", repair_start)
    repair_body = text[repair_start:repair_end]
    backup_at = repair_body.index("self:backup_sidecar(task, context)")
    lookup_at = repair_body.index("self:find_existing_photo(task)")
    cleanup_at = repair_body.index(
        "self:delete_style_preview_snapshots(photo, task, context)"
    )
    save_at = repair_body.index("photo:saveMetadata()")
    assert backup_at < lookup_at < cleanup_at < save_at
    for forbidden in (
        "find_or_import",
        "addPhoto",
        "readMetadata",
        "applyDevelopSettings",
        "applyDevelopPreset",
    ):
        assert forbidden not in repair_body

    process_start = text.index("function Bridge:process(running_path)")
    process_end = text.index("function Bridge:recover_running_tasks()", process_start)
    process_body = text[process_start:process_end]
    assert 'if task.task_type == "repair_preview_metadata" then' in process_body
    assert "self:repair_preview_metadata(task, context)" in process_body


def test_plugin_transient_snapshot_cleanup_is_exact_catalog_only_and_sidecar_free() -> (
    None
):
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    text = (plugin_root / "Bridge.lua").read_text(encoding="utf-8")

    cleanup_start = text.index(
        "function Bridge:cleanup_transient_snapshot(task, context)"
    )
    cleanup_end = text.index("function Bridge:resolve_preset", cleanup_start)
    cleanup_body = text[cleanup_start:cleanup_end]
    assert "self:find_existing_photo(task)" in cleanup_body
    assert "identity.batch_id == task.source_batch_id" in cleanup_body
    assert "identity.task_id == task.source_task_id" in cleanup_body
    assert "#targets ~= 1" in cleanup_body
    assert "self:delete_transient_snapshot(photo, context)" in cleanup_body
    for forbidden in (
        "find_or_import",
        "backup_sidecar",
        "saveMetadata",
        "readMetadata",
        "applyDevelopSettings",
        "applyDevelopPreset",
        "export_jpeg",
    ):
        assert forbidden not in cleanup_body

    delete_start = text.index(
        "function Bridge:delete_transient_snapshot(photo, context)"
    )
    delete_end = text.index("function Bridge:cleanup_transient_snapshot", delete_start)
    delete_body = text[delete_start:delete_end]
    assert "local primary_id = target.id_global or target.snapshotID" in delete_body
    activate = delete_body.index('switch_module("develop"')
    delete_call = delete_body.index("photo:deleteDevelopSnapshot(delete_id)")
    restore = delete_body.index("switch_module(prior_module", delete_call)
    assert activate < delete_call < restore
    assert "snapshot.name == context.snapshot_name" in delete_body
    assert "snapshot.snapshotID == context.snapshot_id" in delete_body
    assert "if poll < 100 then LrTasks.sleep(0.05) end" in delete_body

    process_start = text.index("function Bridge:process(running_path)")
    process_end = text.index(
        "function Bridge:recover_active_preview_copy", process_start
    )
    process_body = text[process_start:process_end]
    cleanup_branch = process_body.index(
        'if task.task_type == "cleanup_transient_snapshot" then'
    )
    assert cleanup_branch < process_body.index("context.sidecar_path")
    assert (
        'context.result.xmp_status = "not_requested"' in process_body[cleanup_branch:]
    )
    assert (
        "self:cleanup_transient_snapshot(task, context)"
        in process_body[cleanup_branch:]
    )
    assert '"cleanup_count"' in text


def test_plugin_registers_managed_presets_and_publishes_real_plugin_uuids() -> None:
    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    text = (plugin_root / "Bridge.lua").read_text(encoding="utf-8")

    # The registry location is derived from the configured portable bridge root;
    # no task or registry content can select an arbitrary executable Lua path.
    assert 'MANAGED_REGISTRY_FILE = "managed-preset-registry.lua"' in text
    assert 'MANAGED_REGISTRATION_FILE = "managed-preset-registration.json"' in text
    assert 'path_key(LrPathUtils.leafName(pointer)) == "lightroom-bridge"' in text
    assert 'pointer:match("^%a:[/\\\\]")' in text
    assert "not has_relative_segment(pointer)" in text
    assert "path_key(LrPathUtils.parent(registry_path)) == path_key(style_root)" in text
    assert "read_all(path, MAX_MANAGED_REGISTRY_BYTES)" in text
    assert "parse_managed_registry_literal(contents)" in text
    assert "loadstring" not in text
    assert "setfenv" not in text
    assert "registry bytes are never compiled" in text.lower()
    assert "Adobe local-only presets must not enter the managed plugin registry" in text

    # Lightroom is the UUID authority. Existing names are reused, missing names
    # are registered once, and a post-registration enumeration supplies UUIDs.
    assert "function Bridge:register_managed_presets(force)" in text
    assert "LrApplication.addDevelopPresetForPlugin(" in text
    assert "existing = plugin_presets_by_name()" in text
    assert "plugin_uuid = found and found.uuid or nil" in text
    assert 'status = found and "registered" or "failed"' in text
    assert "atomic_write(self.managed_paths.registration, contents)" in text
    assert "registry.registry_hash == self.managed_registry_hash" in text
    assert "signature == self.managed_registry_signature" in text

    # Registration happens without touching a photo: on startup, when a changed
    # registry is noticed, before enumeration, and before resolving plugin UUIDs.
    assert text.count("self:register_managed_presets(false)") >= 4
    registration = text.index("function Bridge:register_managed_presets(force)")
    photo_lookup = text.index("function Bridge:find_or_import")
    assert registration > photo_lookup  # registration itself is a separate path
    registration_body = text[
        registration : text.index("function Bridge:refresh_managed_presets_if_due")
    ]
    assert "findPhotoByPath" not in registration_body
    assert "saveMetadata" not in registration_body
    assert "applyDevelopSettings" not in registration_body


def test_managed_registry_literal_parser_reads_large_registry_without_execution(
    tmp_path: Path,
) -> None:
    """Exercise the plug-in parser with a real Lua runtime when one is available."""

    texlua = shutil.which("texlua")
    if texlua is None:
        pytest.skip("texlua is not installed")

    # Some Windows TeX distributions expose ``texlua`` on PATH while their
    # launcher itself takes tens of seconds to initialize (even for an empty
    # script). That is not a usable Lua test runtime and must not be confused
    # with parser time. Responsive runtimes still exercise the full 1006-entry
    # registry below under the independent 30-second process deadline.
    probe = tmp_path / "texlua-probe.lua"
    probe.write_text('io.write("ready")\n', encoding="utf-8")
    try:
        probe_result = subprocess.run(
            [texlua, str(probe)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("texlua launcher takes more than 5 seconds to initialize")
    if probe_result.returncode != 0 or probe_result.stdout != "ready":
        pytest.skip("texlua is present but cannot execute a trivial script")

    plugin_root = (
        Path(bridge.__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )
    bridge_text = (plugin_root / "Bridge.lua").read_text(encoding="utf-8")
    parser_start = bridge_text.index("local function parse_managed_registry_literal")
    parser_end = bridge_text.index("local function load_managed_registry", parser_start)
    parser_source = bridge_text[parser_start:parser_end].replace(
        "local function parse_managed_registry_literal",
        "function parse_managed_registry_literal",
        1,
    )
    runner = tmp_path / "parse-registry.lua"
    runner.write_text(
        parser_source
        + """
local handle = assert(io.open(arg[1], "rb"))
local contents = handle:read("*a")
handle:close()
local parse_started = os.clock()
local payload = parse_managed_registry_literal(contents)
local parse_seconds = os.clock() - parse_started
assert(parse_seconds < 10, "registry parser exceeded 10 CPU seconds")
assert(#payload.entries == tonumber(arg[2]), "entry count mismatch")
local attacked = pcall(parse_managed_registry_literal,
    'return { entries = {}, injected = os.execute("echo unsafe"), }')
assert(not attacked, "registry parser executed or accepted a function call")
io.write(tostring(#payload.entries))
""",
        encoding="utf-8",
    )
    registry = tmp_path / "managed-preset-registry.lua"
    registry.write_text(
        "return "
        + _lua_literal(
            {
                "schema_version": 1,
                "registry_hash": "a" * 64,
                "entries": [
                    {
                        "preset_id": f"managed:{index}",
                        "file_hash": f"{index:064x}"[-64:],
                        "plugin_name": f"PhotoAI::{index:04d}",
                        "target_scope": "plugin",
                        "develop_settings": {
                            "Exposure2012": (index % 9 - 4) / 10,
                            "ProcessVersion": "15.3",
                        },
                    }
                    for index in range(1006)
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [texlua, str(runner), str(registry), "1006"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "1006"
