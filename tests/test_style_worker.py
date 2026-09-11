from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import landscape_culler.style_worker as style_worker_module
from landscape_culler.lightroom_apply import LightroomApplyError
from landscape_culler.style_worker import (
    REQUIRED_PREVIEW_PLUGIN_VERSION,
    StyleWorkerError,
    require_lightroom_preview_plugin,
    run_style_worker,
    start_style_preview_batch,
)
from landscape_culler.util import read_json, write_json


def _catalog(count: int = 6) -> dict[str, Any]:
    entries = []
    for index in range(count):
        entries.append(
            {
                "preset_id": f"uuid:p{index}",
                "file_hash": f"hash-{index}",
                "name": f"Landscape {index}",
                "category": "landscape" if index < 3 else "tone",
                "source": "Adobe",
                "source_kind": "adobe-installed",
                "registration_status": "installed",
                "compatibility": "compatible",
                "runtime_preset_uuid": f"runtime-p{index}",
                "uuid": f"source-p{index}",
                "preset_scope": "catalog",
                "supports_amount": True,
                "ai_eligible": True,
            }
        )
    return {
        "generated_at": "now",
        "default_pool": [item["preset_id"] for item in entries],
        "entries": entries,
    }


def _mark_creative_look(entry: dict[str, Any], index: int = 0) -> dict[str, Any]:
    look_uuid = f"LOOK-{index}"
    payload = {
        "SchemaVersion": 1,
        "UUID": look_uuid,
        "Name": str(entry.get("name") or f"Creative {index}"),
        "Group": "Creative",
        "Cluster": "Adobe",
        "SupportsAmount": True,
        "Parameters": {
            "ProcessVersion": "15.4",
            "Saturation": -10 - index,
        },
        "TableDigests": {},
        "ComplexParameterDigests": {},
    }
    descriptor_hash = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    entry.update(
        look_kind="lightroom_profile",
        profile_name=payload["Name"],
        profile_hash=entry["file_hash"],
        xmp_compatible=True,
        runtime_preset_uuid="MUST-NOT-BE-USED",
        uuid=look_uuid,
        supports_amount=True,
        look_descriptor={**payload, "Hash": descriptor_hash},
        look_descriptor_hash=descriptor_hash,
    )
    return entry


def _run(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    run_dir = data_dir / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    raw = tmp_path / "source.ARW"
    raw.write_bytes(b"raw-test-data")
    write_json(
        run_dir / "results.json",
        {
            "run_id": "run-1",
            "results": [
                {
                    "path": str(raw),
                    "group_id": 1,
                    "rating": 4,
                    "technical": {"brightness": 0.48},
                }
            ],
        },
    )
    write_json(
        run_dir / "develop.json",
        {
            "schema_version": 5,
            "plan_id": "develop-test",
            "run_id": "run-1",
            "source_review_revision": 0,
            "revision": 3,
            "crop": {"status": "confirmed"},
            "basic_color": {"status": "pending"},
            "creative_style": {"status": "pending", "groups": {}},
            "color_mode": "pending",
            "items": [
                {
                    "index": 0,
                    "path": str(raw),
                    "filename": raw.name,
                    "group_id": 1,
                    "crop_id": "original",
                    "crop_candidates": [
                        {
                            "id": "original",
                            "bounds": {
                                "left": 0.0,
                                "top": 0.0,
                                "right": 1.0,
                                "bottom": 1.0,
                            },
                        }
                    ],
                    "angle": 0.0,
                    "base": {
                        "AutoTone": "True",
                        "WhiteBalance": "Auto",
                        "AutoLateralCA": 1,
                        "LensProfileEnable": 1,
                    },
                    "smart_crop": {
                        "scene_type": "landscape",
                        "composition": "layers",
                        "summary": "mountain lake at sunset",
                        "engines": {"scene": "qwen3-vl:8b-instruct"},
                    },
                }
            ],
        },
    )
    return data_dir, run_dir, raw


def _add_second_group(run_dir: Path, source_dir: Path) -> Path:
    second = source_dir / "source-2.ARW"
    second.write_bytes(b"second-raw-test-data")
    results_path = run_dir / "results.json"
    results = read_json(results_path)
    results["results"].append(
        {
            "path": str(second),
            "group_id": 2,
            "rating": 4,
            "technical": {"brightness": 0.36},
        }
    )
    write_json(results_path, results)

    develop_path = run_dir / "develop.json"
    develop = read_json(develop_path)
    first = dict(develop["items"][0])
    first.update(
        index=1,
        path=str(second),
        filename=second.name,
        group_id=2,
        smart_crop={
            "scene_type": "landscape",
            "composition": "leading-lines",
            "summary": "forest trail in overcast weather",
            "engines": {"scene": "qwen3-vl:8b-instruct"},
        },
    )
    develop["items"].append(first)
    write_json(develop_path, develop)
    return second


def _online(_data_dir: Path | str) -> dict[str, Any]:
    return {
        "heartbeat": {
            "state": "online",
            "plugin_version": REQUIRED_PREVIEW_PLUGIN_VERSION,
        }
    }


def test_plugin_gate_rejects_online_old_plugin(tmp_path: Path) -> None:
    with pytest.raises(StyleWorkerError, match=REQUIRED_PREVIEW_PLUGIN_VERSION):
        require_lightroom_preview_plugin(
            tmp_path,
            status_reader=lambda _path: {
                "heartbeat": {"state": "online", "plugin_version": "0.1.2"}
            },
        )


def test_start_publishes_original_raw_neutral_plus_three_candidates(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, raw = _run(tmp_path)
    captured: list[Any] = []

    def create_batch(_data_dir: Path, tasks: list[Any], *, batch_id: str) -> dict:
        captured.extend(tasks)
        return {"batch_id": batch_id, "status": "pending"}

    started = start_style_preview_batch(
        run_dir,
        data_dir,
        "style-batch-1",
        3,
        catalog=_catalog(),
        status_reader=_online,
        batch_creator=create_batch,
    )

    assert started["published_count"] == 4
    assert len(captured) == 4
    assert all(Path(task.photo_path) == raw.resolve() for task in captured)
    assert all(task.task_type == "preview" for task in captured)
    assert sum(task.preset_uuid is None for task in captured) == 1
    assert {task.preset_uuid for task in captured if task.preset_uuid} == {
        f"runtime-p{index}" for index in range(3)
    }
    assert all(task.look_descriptor_path is None for task in captured)
    assert all(task.look_uuid is None for task in captured)
    assert all(task.look_amount == 100 for task in captured)


def test_start_uses_dino_qwen_clip_cascade_before_lightroom(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, raw = _run(tmp_path)
    catalog = _catalog(10)
    cascade_calls: list[str] = []
    captured: list[Any] = []

    def cascade(groups, entries, data_root, **kwargs):
        cascade_calls.append("cascade")
        assert list(groups) == ["1"]
        assert Path(data_root) == data_dir.resolve()
        assert len(entries) == 10
        assert kwargs["seed_scenes"]["1"]["scene_type"] == "landscape"
        return {
            "groups": {
                "1": {
                    "probes": {
                        "representative": str(raw.resolve()),
                        "brightest": str(raw.resolve()),
                        "darkest": str(raw.resolve()),
                        "basis": "dinov2_medoid",
                    },
                    "scene": {"scene": "mountain", "search_terms": ["alpine"]},
                    "clip_scores": {
                        f"uuid:p{index}": 0.50 + index * 0.04 for index in range(10)
                    },
                    "stages": {
                        name: {
                            "status": "complete",
                            "model": name,
                            "used": True,
                            "cached": False,
                        }
                        for name in ("dinov2", "qwen3_vl", "clip")
                    },
                }
            },
            "stages": {
                name: {
                    "status": "complete",
                    "model": name,
                    "used": True,
                    "cached": False,
                }
                for name in ("dinov2", "qwen3_vl", "clip")
            },
        }

    def create_batch(_data_dir: Path, tasks: list[Any], *, batch_id: str) -> dict:
        captured.extend(tasks)
        return {"batch_id": batch_id, "status": "pending"}

    started = start_style_preview_batch(
        run_dir,
        data_dir,
        "style-cascade",
        3,
        catalog=catalog,
        cascade_runner=cascade,
        status_reader=_online,
        batch_creator=create_batch,
    )

    assert cascade_calls == ["cascade"]
    group = started["plan"]["groups"][0]
    assert group["recall_basis"] == "clip_image_text"
    assert group["candidates"][0]["preset_id"] == "uuid:p9"
    assert group["candidates"][0]["clip_score"] == pytest.approx(0.86)
    assert group["missing_stages"] == ["qrealign_rerank"]
    assert started["plan"]["ai_pipeline"]["stages"]["clip"]["used"] is True
    assert len(captured) == 4


def test_lightroom_profile_preview_uses_compact_look_after_base_style(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(1)
    catalog["entries"][0]["name"] = "Adobe Vintage 04"
    _mark_creative_look(catalog["entries"][0])
    captured: list[Any] = []

    def create_batch(_data_dir: Path, tasks: list[Any], *, batch_id: str) -> dict:
        captured.extend(tasks)
        return {"batch_id": batch_id, "status": "pending"}

    started = start_style_preview_batch(
        run_dir,
        data_dir,
        "style-profile-amount",
        3,
        group_id=1,
        preset_id="uuid:p0",
        preset_hash="hash-0",
        amount=135,
        preview_only=True,
        catalog=catalog,
        status_reader=_online,
        batch_creator=create_batch,
    )

    profile_task = next(task for task in captured if task.look_uuid)
    assert profile_task.preset_uuid is None
    assert "CameraProfile" not in profile_task.style
    assert profile_task.look_uuid == "LOOK-0"
    assert profile_task.look_amount == 135
    assert Path(profile_task.look_descriptor_path).parent == (
        data_dir.resolve() / "lightroom-bridge" / "presets" / "looks"
    )
    assert Path(profile_task.look_descriptor_path).name == (
        f"{profile_task.look_descriptor_hash}.look"
    )
    assert profile_task.auto_tone is True
    assert profile_task.auto_white_balance is True
    candidate = started["plan"]["groups"][0]["candidates"][0]
    assert candidate["look_kind"] == "lightroom_profile"
    assert candidate["profile_name"] == "Adobe Vintage 04"
    assert candidate["look_uuid"] == "LOOK-0"
    assert (
        candidate["look_descriptor_hash"]
        == catalog["entries"][0]["look_descriptor_hash"]
    )
    assert candidate["xmp_compatible"] is True


def test_lightroom_look_never_reuses_catalog_preset_uuid(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(1)
    catalog["entries"][0]["name"] = "Film-Inspired 12"
    _mark_creative_look(catalog["entries"][0])
    catalog["entries"][0]["runtime_preset_uuid"] = "ADOBE-CATALOG-UUID"
    captured: list[Any] = []

    def create_batch(_data_dir: Path, tasks: list[Any], *, batch_id: str) -> dict:
        captured.extend(tasks)
        return {"batch_id": batch_id, "status": "pending"}

    start_style_preview_batch(
        run_dir,
        data_dir,
        "style-profile-uuid",
        3,
        group_id=1,
        preset_id="uuid:p0",
        preset_hash="hash-0",
        amount=135,
        preview_only=True,
        catalog=catalog,
        status_reader=_online,
        batch_creator=create_batch,
    )

    look_task = next(task for task in captured if task.look_uuid)
    assert look_task.preset_uuid is None
    assert look_task.look_uuid == "LOOK-0"
    assert look_task.look_amount == 135
    assert "CameraProfile" not in look_task.style


def test_default_catalog_prefers_creative_profiles_exclusively() -> None:
    catalog = _catalog(4)
    for index, entry in enumerate(catalog["entries"][:2]):
        entry["name"] = f"Creative {index}"
        _mark_creative_look(entry, index)

    preferred = style_worker_module._prefer_creative_profiles(catalog)

    assert preferred["candidate_mode"] == "creative_profiles"
    assert preferred["default_pool"] == ["uuid:p0", "uuid:p1"]


def test_profile_recommendation_and_develop_keep_export_identity(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(7)
    for index, entry in enumerate(catalog["entries"]):
        entry["name"] = f"Creative {index}"
        _mark_creative_look(entry, index)
    lightroom = _FakeLightroom()

    result = run_style_worker(
        run_dir,
        data_dir,
        "style-profile-persist",
        3,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=lightroom.create,
        batch_waiter=lightroom.wait,
        scorer_factory=_FakeScorer,
    )

    group = result["recommendation"]["groups"][0]
    assert group["recommended_look_kind"] == "lightroom_profile"
    winner = next(
        item
        for item in group["top3"]
        if item["preset_id"] == group["recommended_preset_id"]
    )
    assert group["recommended_profile_name"] == winner["profile_name"]
    assert group["recommended_profile_hash"] == winner["profile_hash"]
    assert group["recommended_xmp_compatible"] is True
    assert all(item["look_kind"] == "lightroom_profile" for item in group["top3"])
    assert all(item["xmp_compatible"] is True for item in group["top3"])
    persisted = result["develop"]["creative_style"]["groups"]["1"]
    assert persisted["recommended_profile_name"] == winner["profile_name"]
    assert persisted["recommended_profile_hash"] == winner["profile_hash"]
    assert all(item["xmp_compatible"] is True for item in persisted["top3"])


@pytest.mark.parametrize(
    ("preset_id", "preset_hash"),
    [("uuid:p0", None), (None, "hash-0")],
)
def test_worker_rejects_unpaired_preset_identity_before_writes(
    tmp_path: Path,
    preset_id: str | None,
    preset_hash: str | None,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)

    with pytest.raises(StyleWorkerError, match="必须同时提供"):
        start_style_preview_batch(
            run_dir,
            data_dir,
            "style-unpaired",
            3,
            group_id=1,
            preset_id=preset_id,
            preset_hash=preset_hash,
            preview_only=True,
            catalog=_catalog(1),
            status_reader=lambda _path: pytest.fail("plugin gate must not run"),
            batch_creator=lambda *_args, **_kwargs: pytest.fail(
                "batch must not be created"
            ),
        )

    assert not (run_dir / "style-recommendations.json").exists()
    assert read_json(run_dir / "develop.json")["revision"] == 3


def test_worker_rejects_amount_for_preset_without_amount_support(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(1)
    catalog["entries"][0]["supports_amount"] = False

    with pytest.raises(StyleWorkerError, match="不支持强度调整"):
        start_style_preview_batch(
            run_dir,
            data_dir,
            "style-no-amount",
            3,
            group_id=1,
            preset_id="uuid:p0",
            preset_hash="hash-0",
            amount=65,
            preview_only=True,
            catalog=catalog,
            status_reader=lambda _path: pytest.fail("plugin gate must not run"),
            batch_creator=lambda *_args, **_kwargs: pytest.fail(
                "batch must not be created"
            ),
        )


def test_worker_rejects_amount_for_hidden_plugin_preset_even_if_source_xmp_claims_it(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(1)
    catalog["entries"][0].update(
        preset_scope="plugin",
        registration_status="registered",
        source_supports_amount=True,
        supports_amount=True,
    )

    with pytest.raises(StyleWorkerError, match="不支持强度调整"):
        start_style_preview_batch(
            run_dir,
            data_dir,
            "style-plugin-no-runtime-amount",
            3,
            group_id=1,
            preset_id="uuid:p0",
            preset_hash="hash-0",
            amount=150,
            preview_only=True,
            catalog=catalog,
            status_reader=lambda _path: pytest.fail("plugin gate must not run"),
            batch_creator=lambda *_args, **_kwargs: pytest.fail(
                "batch must not be created"
            ),
        )


def test_worker_refreshes_index_after_managed_registration_writeback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    stale = _catalog(1)
    entry = stale["entries"][0]
    entry.update(
        source_kind="managed",
        registration_status="awaiting_registration",
        runtime_preset_uuid=None,
        preset_scope="plugin",
        ai_eligible=False,
    )
    stale["default_pool"] = []
    refreshed = {
        **stale,
        "entries": [
            {
                **entry,
                "registration_status": "registered",
                "runtime_preset_uuid": "PLUGIN-P0",
                "preset_scope": "plugin",
                "ai_eligible": True,
            }
        ],
        "default_pool": [entry["preset_id"]],
    }
    write_json(
        data_dir / "style-library" / "managed-preset-registration.json",
        {
            "schema_version": 1,
            "entries": [
                {
                    "preset_id": entry["preset_id"],
                    "file_hash": entry["file_hash"],
                    "plugin_uuid": "PLUGIN-P0",
                    "scope": "plugin",
                    "status": "registered",
                }
            ],
        },
    )
    synced: list[Path] = []
    current_catalog = [stale]
    monkeypatch.setattr(
        style_worker_module, "load_style_index", lambda _path: current_catalog[0]
    )

    def fake_sync(path: Path) -> dict[str, Any]:
        synced.append(Path(path))
        current_catalog[0] = refreshed
        write_json(data_dir / "style-library" / "index.json", refreshed)
        return refreshed

    monkeypatch.setattr(style_worker_module, "sync_style_library", fake_sync)
    monkeypatch.setattr(
        style_worker_module,
        "_enumerate_runtime_presets",
        lambda _path: [
            {
                "scope": "plugin",
                "uuid": "PLUGIN-P0",
                "name": "Managed P0",
                "folder": "Hidden",
                "file": "",
            }
        ],
    )
    captured: list[Any] = []

    def create_batch(_data_dir: Path, tasks: list[Any], *, batch_id: str) -> dict:
        captured.extend(tasks)
        return {"batch_id": batch_id, "status": "pending"}

    started = start_style_preview_batch(
        run_dir,
        data_dir,
        "style-registration-refresh",
        3,
        status_reader=_online,
        batch_creator=create_batch,
    )

    assert synced == [data_dir.resolve()]
    assert started["published_count"] == 2
    preset_task = next(task for task in captured if task.preset_uuid)
    assert preset_task.preset_uuid == "PLUGIN-P0"
    assert preset_task.preset_scope == "plugin"


def test_runtime_listing_gates_sdk_unavailable_presets_and_keeps_managed() -> None:
    catalog = {
        "default_pool": ["missing", "catalog", "managed"],
        "entries": [
            {
                "preset_id": "missing",
                "registration_status": "installed",
                "runtime_preset_uuid": "ADOBE-HIDDEN",
                "preset_scope": "catalog",
                "ai_eligible": True,
            },
            {
                "preset_id": "catalog",
                "registration_status": "installed",
                "runtime_preset_uuid": "FILE-UUID",
                "preset_scope": "catalog",
                "path": r"E:\Adobe\Settings\Premium\Landscape\Visible.xmp",
                "ai_eligible": True,
            },
            {
                "preset_id": "managed",
                "registration_status": "registered",
                "runtime_preset_uuid": "PLUGIN-UUID",
                "preset_scope": "plugin",
                "plugin_name": "PhotoAI::hash::Managed",
                "ai_eligible": True,
            },
        ],
    }
    verified = style_worker_module._apply_runtime_preset_listing(
        catalog,
        [
            {
                "scope": "catalog",
                "uuid": "SDK-CATALOG-UUID",
                "name": "Visible",
                "folder": "User Presets",
                "file": r"C:\ProgramData\CameraRaw\Premium\Landscape\Visible.xmp",
            },
            {
                "scope": "plugin",
                "uuid": "PLUGIN-UUID",
                "name": "PhotoAI::hash::Managed",
                "folder": "照片选片（隐藏）",
                "file": "",
            },
        ],
    )

    by_id = {item["preset_id"]: item for item in verified["entries"]}
    assert by_id["missing"]["runtime_resolvable"] is False
    assert by_id["missing"]["ai_eligible"] is False
    assert by_id["catalog"]["runtime_resolvable"] is True
    assert by_id["catalog"]["runtime_preset_uuid"] == "SDK-CATALOG-UUID"
    assert by_id["managed"]["runtime_resolvable"] is True
    assert by_id["managed"]["runtime_preset_uuid"] == "PLUGIN-UUID"
    assert verified["default_pool"] == ["catalog", "managed"]


def test_runtime_enumeration_writes_e_drive_snapshot_from_guarded_listing(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    listing = data_dir / "lightroom-bridge" / "presets" / "runtime.presets"
    listing.parent.mkdir(parents=True)
    listing.write_text("listing", encoding="utf-8")
    records = [
        {
            "scope": "plugin",
            "uuid": "PLUGIN-UUID",
            "name": "Managed",
            "folder": "Hidden",
            "file": "",
        }
    ]
    created: list[str] = []

    def create(_data_dir: Path, *, batch_id: str, task_id: str) -> dict[str, Any]:
        created.extend([batch_id, task_id])
        return {"batch_id": batch_id}

    progress_options: dict[str, Any] = {}

    def wait(
        _data_dir: Path,
        batch_id: str,
        timeout: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert batch_id == created[0]
        assert timeout == style_worker_module.RUNTIME_PRESET_ENUMERATION_TIMEOUT
        progress_options.update(kwargs)
        return {
            "status": "complete",
            "tasks": [
                {
                    "status": "done",
                    "result": {
                        "preset_status": "done",
                        "preset_list_path": str(listing),
                    },
                }
            ],
        }

    result = style_worker_module._enumerate_runtime_presets(
        data_dir,
        batch_creator=create,
        batch_waiter=wait,
        listing_reader=lambda path: records if Path(path) == listing.resolve() else [],
    )

    assert result == records
    assert created[1] == "presets"
    assert progress_options == {
        "progress_phase": "presets",
        "progress_label": "核对 Lightroom 可用预设",
        "progress_unit": "项",
        "progress_total": 1,
    }
    snapshot = read_json(
        data_dir / "style-library" / style_worker_module.RUNTIME_PRESET_SNAPSHOT
    )
    assert snapshot["plugin_version"] == REQUIRED_PREVIEW_PLUGIN_VERSION
    assert snapshot["entries"] == records


def test_recommendation_reuses_fresh_runtime_snapshot_without_enumerating(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(6)
    for index, entry in enumerate(catalog["entries"]):
        entry["name"] = f"Creative {index}"
        _mark_creative_look(entry, index)
        entry["compatibility"] = "compatible"
    monkeypatch.setattr(
        style_worker_module,
        "_load_runtime_style_catalog",
        lambda _data_dir: catalog,
    )
    monkeypatch.setattr(
        style_worker_module,
        "_read_runtime_preset_snapshot",
        lambda _data_dir: [],
    )
    monkeypatch.setattr(
        style_worker_module,
        "_enumerate_runtime_presets",
        lambda _data_dir: pytest.fail(
            "a fresh runtime snapshot must avoid Lightroom preset enumeration"
        ),
    )
    captured: list[Any] = []

    def create_batch(_data_dir: Path, tasks: list[Any], *, batch_id: str) -> dict:
        captured.extend(tasks)
        return {"batch_id": batch_id, "status": "pending"}

    result = start_style_preview_batch(
        run_dir,
        data_dir,
        "style-snapshot-reuse",
        3,
        scope="global",
        status_reader=_online,
        batch_creator=create_batch,
        cascade_runner=lambda *_args, **_kwargs: {"groups": {}, "stages": {}},
    )

    assert result["published_count"] == 4
    assert len(captured) == 4


def test_forced_preview_rejects_sdk_unresolvable_catalog_entry(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(1)
    catalog["entries"][0]["runtime_resolvable"] = False

    with pytest.raises(StyleWorkerError, match="Lightroom 解析"):
        start_style_preview_batch(
            run_dir,
            data_dir,
            "style-unresolvable",
            3,
            group_id=1,
            preset_id="uuid:p0",
            preset_hash="hash-0",
            preview_only=True,
            catalog=catalog,
            status_reader=_online,
            batch_creator=lambda *_args, **_kwargs: pytest.fail(
                "batch must not be created"
            ),
        )


class _FakeScorer:
    def __init__(self, _data_dir: Path) -> None:
        self.released = False

    def score(self, paths: list[Path]) -> list[dict[str, float]]:
        assert len(paths) == 4
        return [
            {"aesthetic": 0.40, "quality": 0.40},
            {"aesthetic": 0.98, "quality": 0.95},
            *(
                {"aesthetic": 0.30 + index * 0.01, "quality": 0.32}
                for index in range(2)
            ),
        ]

    def release(self) -> None:
        self.released = True


class _FakeLightroom:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.tasks: list[Any] = []

    def create(self, _data_dir: Path, tasks: list[Any], *, batch_id: str) -> dict:
        self.rows = []
        self.tasks = list(tasks)
        for index, task in enumerate(tasks):
            target = Path(task.jpeg_output_dir) / f"{task.task_id}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new(
                "RGB",
                (96, 64),
                (90 + index * 8, 110 + index * 4, 125 + index * 3),
            ).save(target, "JPEG", quality=90)
            self.rows.append(
                {
                    "task_id": task.task_id,
                    "photo_path": str(task.photo_path),
                    "status": "done",
                    "result": {
                        "batch_id": batch_id,
                        "task_id": task.task_id,
                        "restore_status": "done",
                        "isolation_kind": "virtual_copy",
                        "isolation_status": "removed",
                        "look_status": "done" if task.look_uuid else "not_requested",
                        "look_uuid": task.look_uuid,
                        "look_amount": task.look_amount,
                        "jpeg_path": str(target),
                    },
                }
            )
        return {"batch_id": batch_id, "status": "pending"}

    def wait(
        self,
        _data_dir: Path,
        batch_id: str,
        _timeout: float,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "status": "complete",
            "tasks": self.rows,
        }


class _FakeLutEngine:
    def __init__(self, project_root: Path, *, lut_hash: str = "lut-hash") -> None:
        self.project_root = Path(project_root)
        self.descriptor = SimpleNamespace(
            lut_id="cube-test",
            lut_hash=lut_hash,
            name="Warm Landscape",
            kind="3d",
            source_label="Test LUT",
        )
        self.render_calls: list[dict[str, Any]] = []

    def get_lut(self, lut_id: str) -> Any:
        assert lut_id == self.descriptor.lut_id
        return self.descriptor

    def cache_key(
        self,
        input_jpeg: Path,
        *,
        lut_hash: str,
        strength: float,
    ) -> str:
        payload = (
            Path(input_jpeg).read_bytes()
            + lut_hash.encode("utf-8")
            + str(strength).encode("ascii")
        )
        return hashlib.sha256(payload).hexdigest()

    def render_jpeg(self, input_jpeg: Path, **kwargs: Any) -> Any:
        output_path = Path(kwargs["output_path"])
        self.render_calls.append({"input": Path(input_jpeg), **kwargs})
        with Image.open(input_jpeg) as image:
            image.convert("RGB").save(output_path, "JPEG", quality=90)
        return SimpleNamespace(
            output_path=output_path,
            lut_id=self.descriptor.lut_id,
            lut_hash=self.descriptor.lut_hash,
            strength=float(kwargs["strength"]),
            xmp_compatible=False,
        )


def test_global_scope_renders_once_and_persistent_request_cache_survives_group_switch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir, run_dir, first_raw = _run(tmp_path)
    _add_second_group(run_dir, tmp_path)
    catalog = _catalog(7)
    cascade_calls: list[list[str]] = []

    def cascade(groups, _entries, _data_root, **_kwargs):
        cascade_calls.append(list(groups))
        rows: dict[str, dict[str, Any]] = {}
        for group_id, items in groups.items():
            representative = str(Path(str(items[0]["path"])).resolve())
            rows[str(group_id)] = {
                "probes": {
                    "representative": representative,
                    "brightest": representative,
                    "darkest": representative,
                    "basis": "dinov2_medoid",
                },
                "scene": {"scene": "landscape", "search_terms": ["natural"]},
                "clip_scores": {
                    f"uuid:p{index}": 0.9 - index * 0.03 for index in range(7)
                },
                "stages": {
                    stage: {
                        "status": "complete",
                        "model": stage,
                        "used": True,
                        "cached": False,
                    }
                    for stage in ("dinov2", "qwen3_vl", "clip")
                },
            }
        return {
            "groups": rows,
            "stages": {
                stage: {
                    "status": "complete",
                    "model": stage,
                    "used": True,
                    "cached": False,
                }
                for stage in ("dinov2", "qwen3_vl", "clip")
            },
        }

    first_lightroom = _FakeLightroom()
    first = run_style_worker(
        run_dir,
        data_dir,
        "style-global-first",
        3,
        scope="global",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=first_lightroom.create,
        batch_waiter=first_lightroom.wait,
        scorer_factory=_FakeScorer,
        cascade_runner=cascade,
    )

    assert cascade_calls == [["global"]]
    assert first["published_count"] == 4
    assert len(first_lightroom.tasks) == 4
    assert {Path(task.photo_path) for task in first_lightroom.tasks} == {
        first_raw.resolve()
    }
    assert first["recommendation"]["scope"] == "global"
    assert first["recommendation"]["apply_group_ids"] == ["1", "2"]
    assert [row["group_id"] for row in first["recommendation"]["groups"]] == ["global"]
    global_selection = first["develop"]["creative_style"]["global_selection"]
    assert global_selection["scope"] == "global"
    assert len(global_selection["top3"]) == 3
    assert first["develop"]["creative_style"]["groups"] == {}
    global_key = first["recommendation"]["request_cache_key"]
    global_cache = run_dir / "style-request-cache" / f"{global_key}.json"
    assert global_cache.is_file()

    # A group request replaces the current recommendation document but must
    # not destroy the independently keyed global result.
    group_lightroom = _FakeLightroom()
    group = run_style_worker(
        run_dir,
        data_dir,
        "style-group-between",
        4,
        scope="group",
        group_id=1,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=group_lightroom.create,
        batch_waiter=group_lightroom.wait,
        scorer_factory=_FakeScorer,
        cascade_runner=cascade,
    )
    assert cascade_calls == [["global"], ["1"]]
    assert group["recommendation"]["scope"] == "group"
    assert group["recommendation"]["request_cache_key"] != global_key
    assert global_cache.is_file()

    def must_not_run(*_args, **_kwargs):
        pytest.fail(
            "a complete persistent request cache hit must run zero AI/Lightroom"
        )

    monkeypatch.setattr(
        style_worker_module,
        "ensure_lightroom_preview_plugin",
        must_not_run,
    )
    monkeypatch.setattr(
        style_worker_module,
        "_enumerate_runtime_presets",
        must_not_run,
    )

    repeated = run_style_worker(
        run_dir,
        data_dir,
        "style-global-repeat",
        5,
        scope="global",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=must_not_run,
        batch_waiter=must_not_run,
        scorer_factory=must_not_run,
        cascade_runner=must_not_run,
    )

    assert repeated["recommendation_cached"] is True
    assert repeated["published_count"] == 0
    assert repeated["cached_count"] == 4
    assert repeated["recommendation"]["request_cache_key"] == global_key
    assert repeated["develop"]["creative_style"]["scope"] == "global"
    assert repeated["develop"]["revision"] == 6


def test_all_groups_scope_keeps_independent_recommendations_and_reuses_cache(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, first_raw = _run(tmp_path)
    second_raw = _add_second_group(run_dir, tmp_path)
    catalog = _catalog(7)
    lightroom = _FakeLightroom()
    progress_events: list[dict[str, Any]] = []

    first = run_style_worker(
        run_dir,
        data_dir,
        "style-all-groups-first",
        3,
        scope="group",
        group_id=None,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=lightroom.create,
        batch_waiter=lightroom.wait,
        scorer_factory=_FakeScorer,
        preserve_confirmed=True,
        progress=progress_events.append,
    )

    assert first["published_count"] == 8
    assert len(lightroom.tasks) == 8
    assert {Path(task.photo_path) for task in lightroom.tasks} == {
        first_raw.resolve(),
        second_raw.resolve(),
    }
    recommendation_groups = {
        str(group["group_id"]): group for group in first["recommendation"]["groups"]
    }
    assert set(recommendation_groups) == {"1", "2"}
    assert Path(recommendation_groups["1"]["probes"]["representative"]) == (
        first_raw.resolve()
    )
    assert Path(recommendation_groups["2"]["probes"]["representative"]) == (
        second_raw.resolve()
    )
    assert all(len(group["top3"]) == 3 for group in recommendation_groups.values())
    creative_groups = first["develop"]["creative_style"]["groups"]
    assert set(creative_groups) == {"1", "2"}
    assert all(
        group["recommendation_status"] == "complete"
        for group in creative_groups.values()
    )
    assert any(event.get("phase") == "collect" for event in progress_events)

    develop_path = run_dir / "develop.json"
    frozen = read_json(develop_path)
    frozen_groups = frozen["creative_style"]["groups"]
    selected = {
        **dict(frozen_groups["1"]["top3"][0]),
        "preset_id": "uuid:historical-choice",
        "preset_hash": "historical-hash",
        "preset_uuid": "runtime-historical-choice",
        "name": "Historical confirmed look",
    }
    frozen_groups["1"]["top3"].insert(0, selected)
    frozen_groups["1"].update(
        status="confirmed",
        preset_id=selected["preset_id"],
        preset_hash=selected["preset_hash"],
        preset_uuid=selected["preset_uuid"],
        preset_scope=selected["preset_scope"],
        look_kind=selected["look_kind"],
        amount=150,
        selected_preview_key=selected["preview_key"],
        selected_preview_path=selected["preview_path"],
        manual_override=True,
    )
    frozen_groups["2"].update(status="skipped", manual_override=True)
    write_json(develop_path, frozen)

    def must_not_run(*_args, **_kwargs):
        pytest.fail("a complete all-groups cache hit must run zero AI/Lightroom")

    repeated = run_style_worker(
        run_dir,
        data_dir,
        "style-all-groups-repeat",
        4,
        scope="group",
        group_id=None,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=must_not_run,
        batch_waiter=must_not_run,
        scorer_factory=must_not_run,
        cascade_runner=must_not_run,
        preserve_confirmed=True,
    )

    assert repeated["recommendation_cached"] is True
    assert repeated["published_count"] == 0
    assert repeated["cached_count"] == 8
    repeated_groups = repeated["develop"]["creative_style"]["groups"]
    assert set(repeated_groups) == {"1", "2"}
    assert repeated_groups["1"]["status"] == "confirmed"
    assert repeated_groups["1"]["preset_id"] == selected["preset_id"]
    assert repeated_groups["1"]["amount"] == 150
    assert repeated_groups["1"]["manual_override"] is True
    assert repeated_groups["1"]["top3"][0]["preset_id"] == selected["preset_id"]
    assert len(repeated_groups["1"]["top3"]) == 3
    assert repeated_groups["2"]["status"] == "skipped"
    assert repeated_groups["2"]["manual_override"] is True


def test_all_groups_request_cache_changes_when_group_boundaries_change(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    _add_second_group(run_dir, tmp_path)
    catalog = _catalog(7)
    lightroom = _FakeLightroom()

    first = run_style_worker(
        run_dir,
        data_dir,
        "style-group-boundaries-first",
        3,
        scope="group",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=lightroom.create,
        batch_waiter=lightroom.wait,
        scorer_factory=_FakeScorer,
    )
    first_key = first["recommendation"]["request_cache_key"]

    develop_path = run_dir / "develop.json"
    regrouped = read_json(develop_path)
    regrouped["items"][1]["group_id"] = 1
    write_json(develop_path, regrouped)
    results_path = run_dir / "results.json"
    regrouped_results = read_json(results_path)
    regrouped_results["results"][1]["group_id"] = 1
    write_json(results_path, regrouped_results)
    second_lightroom = _FakeLightroom()

    second = run_style_worker(
        run_dir,
        data_dir,
        "style-group-boundaries-second",
        4,
        scope="group",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=second_lightroom.create,
        batch_waiter=second_lightroom.wait,
        scorer_factory=_FakeScorer,
    )

    assert second["recommendation_cached"] is False
    assert second["recommendation"]["request_cache_key"] != first_key
    assert second["recommendation"]["apply_group_ids"] == ["1"]


def test_all_groups_partial_lightroom_failure_saves_success_and_retries_failed_group(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, first_raw = _run(tmp_path)
    second_raw = _add_second_group(run_dir, tmp_path)
    catalog = _catalog(7)
    rows: list[dict[str, Any]] = []

    def create_partial_batch(
        _data_dir: Path, tasks: list[Any], *, batch_id: str
    ) -> dict[str, Any]:
        rows.clear()
        for index, task in enumerate(tasks):
            if Path(task.photo_path) == second_raw.resolve():
                rows.append(
                    {
                        "task_id": task.task_id,
                        "photo_path": task.photo_path,
                        "status": "failed",
                        "result": {"message": "test render failure"},
                    }
                )
                continue
            target = Path(task.jpeg_output_dir) / f"{task.task_id}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (96, 64), (90 + index, 110, 125)).save(
                target, "JPEG", quality=90
            )
            rows.append(
                {
                    "task_id": task.task_id,
                    "photo_path": task.photo_path,
                    "status": "done",
                    "result": {
                        "batch_id": batch_id,
                        "task_id": task.task_id,
                        "restore_status": "done",
                        "isolation_kind": "virtual_copy",
                        "isolation_status": "removed",
                        "look_status": "not_requested",
                        "jpeg_path": str(target),
                    },
                }
            )
        return {"batch_id": batch_id, "status": "pending"}

    def failed_status(_data_dir: Path | str, batch_id: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "status": "failed",
            "completed_count": len(rows),
            "tasks": rows,
        }

    def fail_wait(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise LightroomApplyError("one group failed")

    partial = run_style_worker(
        run_dir,
        data_dir,
        "style-all-groups-partial",
        3,
        scope="group",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=create_partial_batch,
        batch_waiter=fail_wait,
        batch_status_reader=failed_status,
        scorer_factory=_FakeScorer,
        preserve_confirmed=True,
    )

    assert partial["status"] == "partial"
    assert partial["succeeded_group_count"] == 1
    assert partial["failed_group_count"] == 1
    assert partial["recommendation"]["worker"]["successful_group_ids"] == ["1"]
    assert partial["recommendation"]["worker"]["failed_group_ids"] == ["2"]
    partial_groups = partial["develop"]["creative_style"]["groups"]
    assert partial_groups["1"]["recommendation_status"] == "complete"
    assert partial_groups["2"]["recommendation_status"] == "failed"
    assert not (
        run_dir
        / "style-request-cache"
        / f"{partial['recommendation']['request_cache_key']}.json"
    ).exists()

    successful_evidence = partial_groups["1"]["recommended_preset_id"]
    retry_lightroom = _FakeLightroom()
    retried = run_style_worker(
        run_dir,
        data_dir,
        "style-retry-failed-group",
        4,
        scope="group",
        group_id=2,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=retry_lightroom.create,
        batch_waiter=retry_lightroom.wait,
        scorer_factory=_FakeScorer,
    )

    retried_groups = retried["develop"]["creative_style"]["groups"]
    assert retried_groups["1"]["recommended_preset_id"] == successful_evidence
    assert retried_groups["2"]["recommendation_status"] == "complete"
    assert "recommendation_error" not in retried_groups["2"]
    assert {Path(task.photo_path) for task in retry_lightroom.tasks} == {
        second_raw.resolve()
    }
    assert first_raw.is_file() and second_raw.is_file()


def test_cancelled_all_groups_adopts_only_safe_done_tasks_and_retries_the_rest(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, first_raw = _run(tmp_path)
    second_raw = _add_second_group(run_dir, tmp_path)
    catalog = _catalog(7)
    rows: list[dict[str, Any]] = []
    invalid_source: Path | None = None

    def create_cancelled_batch(
        _data_dir: Path, tasks: list[Any], *, batch_id: str
    ) -> dict[str, Any]:
        nonlocal invalid_source
        rows.clear()
        for index, task in enumerate(tasks):
            if index >= 6:
                rows.append(
                    {
                        "task_id": task.task_id,
                        "photo_path": task.photo_path,
                        "status": "cancelled",
                        "result": {"message": "cancelled by user"},
                    }
                )
                continue
            target = Path(task.jpeg_output_dir) / f"{task.task_id}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (96, 64), (85 + index, 105, 125)).save(
                target, "JPEG", quality=90
            )
            result = {
                "batch_id": batch_id,
                "task_id": task.task_id,
                "restore_status": "done",
                "isolation_kind": "virtual_copy",
                "isolation_status": "removed",
                "look_status": "not_requested",
                "jpeg_path": str(target),
            }
            if index == 5:
                # A JPEG existing in the incoming directory is not sufficient:
                # rollback/isolation evidence must also be exact.
                result["restore_status"] = "failed"
                invalid_source = target
            rows.append(
                {
                    "task_id": task.task_id,
                    "photo_path": task.photo_path,
                    "status": "done",
                    "result": result,
                }
            )
        return {"batch_id": batch_id, "status": "pending"}

    def cancelled_status(_data_dir: Path | str, batch_id: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "status": "cancelled",
            "completed_count": len(rows),
            "cancellation": {"batch_id": batch_id, "reason": "user"},
            "tasks": rows,
        }

    def cancel_wait(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise LightroomApplyError("cancelled")

    progress_events: list[dict[str, Any]] = []
    cancelled = run_style_worker(
        run_dir,
        data_dir,
        "style-all-groups-cancelled",
        3,
        scope="group",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=create_cancelled_batch,
        batch_waiter=cancel_wait,
        batch_status_reader=cancelled_status,
        scorer_factory=_FakeScorer,
        preserve_confirmed=True,
        progress=progress_events.append,
    )

    assert cancelled["status"] == "cancelled"
    assert cancelled["succeeded_group_count"] == 1
    assert cancelled["failed_group_count"] == 1
    assert cancelled["recommendation"]["worker"]["cancelled"] is True
    assert cancelled["recommendation"]["worker"]["completed_count"] == 5
    assert cancelled["develop"]["creative_style"]["groups"]["1"][
        "recommendation_status"
    ] == "complete"
    assert cancelled["develop"]["creative_style"]["groups"]["2"][
        "recommendation_status"
    ] == "failed"
    assert progress_events[-1]["status"] == "cancelled"
    assert invalid_source is not None and invalid_source.is_file()
    invalid_task = cancelled["recommendation"]["groups"][1]["render_tasks"][0]
    assert not Path(invalid_task["preview_path"]).is_file()
    assert not (
        run_dir
        / "style-request-cache"
        / f"{cancelled['recommendation']['request_cache_key']}.json"
    ).exists()

    retry_lightroom = _FakeLightroom()
    retried = run_style_worker(
        run_dir,
        data_dir,
        "style-all-groups-after-cancel",
        4,
        scope="group",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=retry_lightroom.create,
        batch_waiter=retry_lightroom.wait,
        batch_status_reader=cancelled_status,
        scorer_factory=_FakeScorer,
        preserve_confirmed=True,
    )

    assert retried["status"] == "complete"
    assert retried["published_count"] == 3
    assert retried["cached_count"] == 5
    assert len(retry_lightroom.tasks) == 3
    assert {Path(task.photo_path) for task in retry_lightroom.tasks} == {
        second_raw.resolve()
    }
    assert first_raw.is_file() and second_raw.is_file()


def test_next_run_recovers_safe_done_tasks_after_worker_was_terminated(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _first_raw = _run(tmp_path)
    second_raw = _add_second_group(run_dir, tmp_path)
    catalog = _catalog(7)
    old_rows: list[dict[str, Any]] = []

    def create_interrupted_batch(
        _data_dir: Path, tasks: list[Any], *, batch_id: str
    ) -> dict[str, Any]:
        old_rows.clear()
        for index, task in enumerate(tasks):
            if index >= 5:
                old_rows.append(
                    {
                        "task_id": task.task_id,
                        "photo_path": task.photo_path,
                        "status": "cancelled",
                        "result": {"message": "worker terminated"},
                    }
                )
                continue
            target = Path(task.jpeg_output_dir) / f"{task.task_id}.jpg"
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (96, 64), (90 + index, 115, 130)).save(
                target, "JPEG", quality=90
            )
            old_rows.append(
                {
                    "task_id": task.task_id,
                    "photo_path": task.photo_path,
                    "status": "done",
                    "result": {
                        "batch_id": batch_id,
                        "task_id": task.task_id,
                        "restore_status": "done",
                        "isolation_kind": "virtual_copy",
                        "isolation_status": "removed",
                        "look_status": "not_requested",
                        "jpeg_path": str(target),
                    },
                }
            )
        return {"batch_id": batch_id, "status": "pending"}

    started = start_style_preview_batch(
        run_dir,
        data_dir,
        "style-terminated-old-batch",
        3,
        scope="group",
        catalog=catalog,
        status_reader=_online,
        batch_creator=create_interrupted_batch,
    )
    assert started["published_count"] == 8
    assert not list((run_dir / "style-previews").glob("*.jpg"))

    # The legacy cancellation failure handler advanced only the bookkeeping
    # revision before the process ended. The source/crop/base/group inputs are
    # unchanged, so the fresh plan must be allowed to match immutable keys.
    develop_path = run_dir / "develop.json"
    legacy_failed = read_json(develop_path)
    legacy_failed["revision"] = 4
    write_json(develop_path, legacy_failed)

    def old_cancelled_status(
        _data_dir: Path | str, batch_id: str
    ) -> dict[str, Any]:
        assert batch_id == "style-terminated-old-batch"
        return {
            "batch_id": batch_id,
            "status": "cancelled",
            "completed_count": len(old_rows),
            "cancellation": {"batch_id": batch_id, "reason": "worker terminated"},
            "tasks": old_rows,
        }

    retry_lightroom = _FakeLightroom()
    progress_events: list[dict[str, Any]] = []
    resumed = run_style_worker(
        run_dir,
        data_dir,
        "style-resume-new-batch",
        4,
        scope="group",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=retry_lightroom.create,
        batch_waiter=retry_lightroom.wait,
        batch_status_reader=old_cancelled_status,
        scorer_factory=_FakeScorer,
        preserve_confirmed=True,
        progress=progress_events.append,
    )

    assert resumed["status"] == "complete"
    assert resumed["cached_count"] == 5
    assert resumed["published_count"] == 3
    assert len(retry_lightroom.tasks) == 3
    assert {Path(task.photo_path) for task in retry_lightroom.tasks} == {
        second_raw.resolve()
    }
    assert len(list((run_dir / "style-previews").glob("*.jpg"))) == 8
    render_events = [
        event
        for event in progress_events
        if event.get("phase") in {"process", "collect"}
    ]
    assert render_events
    assert render_events[0]["phase"] == "process"
    assert render_events[0]["current"] == 1
    assert render_events[0]["total"] == 8
    last_process = max(
        index
        for index, event in enumerate(render_events)
        if event.get("phase") == "process"
    )
    first_collect = next(
        index
        for index, event in enumerate(render_events)
        if event.get("phase") == "collect"
    )
    assert last_process < first_collect
    assert render_events[last_process]["current"] == 5
    assert render_events[first_collect]["current"] == 6
    assert all(event["total"] == 8 for event in render_events)


@pytest.mark.parametrize("frozen_status", ["confirmed", "skipped"])
def test_partial_batch_failure_preserves_existing_valid_group_choice(
    tmp_path: Path,
    frozen_status: str,
) -> None:
    _data_dir, run_dir, _raw = _run(tmp_path)
    _add_second_group(run_dir, tmp_path)
    develop_path = run_dir / "develop.json"
    develop = read_json(develop_path)
    frozen_candidate = {
        "preset_id": "uuid:frozen",
        "preset_hash": "frozen-hash",
        "name": "Frozen look",
        "preview_key": "a" * 64,
        "preview_path": str(run_dir / "style-previews" / f"{'a' * 64}.jpg"),
        "render_status": "ready",
    }
    develop["creative_style"]["groups"] = {
        "2": {
            "group_id": 2,
            "status": frozen_status,
            "recommendation_status": "complete",
            "preset_id": "uuid:frozen" if frozen_status == "confirmed" else None,
            "preset_hash": "frozen-hash" if frozen_status == "confirmed" else None,
            "amount": 100,
            "manual_override": False,
            "top3": [frozen_candidate],
        }
    }
    write_json(develop_path, develop)
    recommendation = {
        "scope": "group",
        "worker": {
            "group_ids": ["2"],
            "preserve_confirmed": True,
        },
        "groups": [
            {
                "group_id": 2,
                "recommendation_status": "failed",
                "error": "new attempt failed",
            }
        ],
    }

    merged = style_worker_module._merge_develop(
        run_dir, recommendation, base_revision=3, preview_only=False
    )
    preserved = merged["creative_style"]["groups"]["2"]

    assert preserved["status"] == frozen_status
    assert preserved["recommendation_status"] == "complete"
    assert preserved["manual_override"] is False
    assert preserved["top3"] == [frozen_candidate]
    assert preserved["last_attempt_status"] == "failed"
    assert preserved["last_attempt_error"] == "new attempt failed"
    if frozen_status == "confirmed":
        assert preserved["preset_id"] == "uuid:frozen"


def test_all_groups_qrealign_failure_isolated_to_one_group(tmp_path: Path) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    _add_second_group(run_dir, tmp_path)
    lightroom = _FakeLightroom()

    class OneGroupFailsScorer:
        def __init__(self, _data_dir: Path) -> None:
            self.calls = 0

        def score(self, paths: list[Path]) -> list[dict[str, float]]:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("Q-ReAlign test failure")
            return _FakeScorer(data_dir).score(paths)

        def release(self) -> None:
            pass

    result = run_style_worker(
        run_dir,
        data_dir,
        "style-all-groups-rerank-partial",
        3,
        scope="group",
        catalog=_catalog(7),
        bridge_status_reader=_online,
        batch_creator=lightroom.create,
        batch_waiter=lightroom.wait,
        scorer_factory=OneGroupFailsScorer,
        preserve_confirmed=True,
    )

    assert result["status"] == "partial"
    assert result["succeeded_group_count"] == 1
    assert result["failed_group_count"] == 1
    groups = result["develop"]["creative_style"]["groups"]
    assert groups["1"]["recommendation_status"] == "complete"
    assert groups["2"]["recommendation_status"] == "failed"
    assert "Q-ReAlign test failure" in groups["2"]["recommendation_error"]


def test_worker_ranks_real_metrics_and_forced_preview_confirms_selection(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(7)
    lightroom = _FakeLightroom()
    first = run_style_worker(
        run_dir,
        data_dir,
        "style-batch-rank",
        3,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=lightroom.create,
        batch_waiter=lightroom.wait,
        scorer_factory=_FakeScorer,
    )

    group = first["recommendation"]["groups"][0]
    assert group["selected"]["kind"] == "preset"
    assert set(group["top3"][0]["metrics"]) == {
        "style_match",
        "aesthetic_improvement",
        "technical_quality",
        "group_consistency",
    }
    develop = read_json(run_dir / "develop.json")
    selection = develop["creative_style"]["groups"]["1"]
    recommended_before = selection["recommended_preset_id"]
    assert selection["recommendation_status"] == "complete"
    assert selection["status"] == "pending"
    assert selection["top3"][0]["preview_path"].startswith(
        str(run_dir / "style-previews")
    )
    cache_files = list((run_dir / "style-previews").glob("*.jpg"))
    assert len(cache_files) == 4
    assert all(len(path.stem) == 64 for path in cache_files)

    develop["creative_style"]["groups"]["1"]["recommendation_error"] = "older failure"
    write_json(run_dir / "develop.json", develop)

    forced = catalog["entries"][-1]
    second_lightroom = _FakeLightroom()
    second = run_style_worker(
        run_dir,
        data_dir,
        "style-batch-forced",
        4,
        group_id=1,
        preset_id=forced["preset_id"],
        preset_hash=forced["file_hash"],
        amount=65,
        preview_only=True,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=second_lightroom.create,
        batch_waiter=second_lightroom.wait,
        scorer_factory=lambda _path: pytest.fail("preview_only must not run Q-ReAlign"),
    )

    selected = second["develop"]["creative_style"]["groups"]["1"]
    assert selected["status"] == "confirmed"
    assert selected["manual_override"] is True
    assert selected["preset_id"] == forced["preset_id"]
    assert selected["amount"] == 65
    assert selected["recommended_preset_id"] == recommended_before
    assert "recommendation_error" not in selected
    assert selected["top3"][0]["preset_id"] == forced["preset_id"]
    assert Path(selected["selected_preview_path"]).is_file()


def test_forced_global_preview_updates_project_wide_selection(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog(7)
    initial_lightroom = _FakeLightroom()
    first = run_style_worker(
        run_dir,
        data_dir,
        "style-global-before-strength",
        3,
        scope="global",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=initial_lightroom.create,
        batch_waiter=initial_lightroom.wait,
        scorer_factory=_FakeScorer,
    )
    previous = first["develop"]["creative_style"]["global_selection"]
    forced = catalog["entries"][-1]
    preview_lightroom = _FakeLightroom()

    second = run_style_worker(
        run_dir,
        data_dir,
        "style-global-strength",
        4,
        scope="global",
        preset_id=forced["preset_id"],
        preset_hash=forced["file_hash"],
        amount=135,
        preview_only=True,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=preview_lightroom.create,
        batch_waiter=preview_lightroom.wait,
        scorer_factory=lambda _path: pytest.fail("preview_only must not run Q-ReAlign"),
    )

    assert second["published_count"] == 1
    assert len(preview_lightroom.tasks) == 1
    assert preview_lightroom.tasks[0].preset_amount == 135
    selected = second["develop"]["creative_style"]["global_selection"]
    assert selected["scope"] == "global"
    assert selected["status"] == "confirmed"
    assert selected["manual_override"] is True
    assert selected["preset_id"] == forced["preset_id"]
    assert selected["amount"] == 135
    assert second["develop"]["creative_style"]["status"] == "confirmed"
    assert selected["recommended_preset_id"] == previous["recommended_preset_id"]
    assert Path(selected["selected_preview_path"]).is_file()


def test_forced_global_preview_renders_stable_multi_photo_sample_set(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, first_raw = _run(tmp_path)
    second_raw = _add_second_group(run_dir, tmp_path)
    catalog = _catalog(7)
    initial_lightroom = _FakeLightroom()
    first = run_style_worker(
        run_dir,
        data_dir,
        "style-global-samples-before",
        3,
        scope="global",
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=initial_lightroom.create,
        batch_waiter=initial_lightroom.wait,
        scorer_factory=_FakeScorer,
    )

    forced = catalog["entries"][-1]
    preview_lightroom = _FakeLightroom()
    second = run_style_worker(
        run_dir,
        data_dir,
        "style-global-samples-150",
        int(first["develop"]["revision"]),
        scope="global",
        preset_id=forced["preset_id"],
        preset_hash=forced["file_hash"],
        amount=150,
        preview_only=True,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=preview_lightroom.create,
        batch_waiter=preview_lightroom.wait,
        scorer_factory=lambda _path: pytest.fail(
            "preview_only must not run Q-ReAlign"
        ),
    )

    assert second["published_count"] == 2
    assert {Path(task.photo_path) for task in preview_lightroom.tasks} == {
        first_raw.resolve(),
        second_raw.resolve(),
    }
    assert {task.preset_amount for task in preview_lightroom.tasks} == {150}
    selected = second["develop"]["creative_style"]["global_selection"]
    assert second["develop"]["creative_style"]["status"] == "confirmed"
    samples = selected["selected_preview_samples"]
    assert len(samples) == 2
    assert "representative" in {sample["role"] for sample in samples}
    assert {sample["index"] for sample in samples} == {0, 1}
    assert all(Path(sample["preview_path"]).is_file() for sample in samples)
    candidate_samples = selected["top3"][0]["preview_samples"]
    assert candidate_samples == samples


def test_forced_lut_preview_uses_existing_lightroom_base_and_persists_capability(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "project" / ".runtime"
    data_dir, run_dir, _raw = _run(runtime_root)
    lightroom = _FakeLightroom()
    first = run_style_worker(
        run_dir,
        data_dir,
        "style-before-lut",
        3,
        catalog=_catalog(7),
        bridge_status_reader=_online,
        batch_creator=lightroom.create,
        batch_waiter=lightroom.wait,
        scorer_factory=_FakeScorer,
    )
    neutral_path = Path(
        first["recommendation"]["groups"][0]["neutral_preview_path"]
    ).resolve()
    recommended_before = first["develop"]["creative_style"]["groups"]["1"][
        "recommended_preset_id"
    ]
    fake_engine = _FakeLutEngine(tmp_path / "project")

    result = run_style_worker(
        run_dir,
        data_dir,
        "style-lut-150",
        4,
        group_id=1,
        lut_id="cube-test",
        lut_hash="lut-hash",
        amount=150,
        preview_only=True,
        bridge_status_reader=lambda _path: pytest.fail(
            "LUT preview must not require Lightroom after the base preview exists"
        ),
        scorer_factory=lambda _path: pytest.fail(
            "forced LUT preview must not run Q-ReAlign"
        ),
        lut_engine_factory=lambda project_root: (
            fake_engine
            if Path(project_root) == (tmp_path / "project").resolve()
            else pytest.fail("worker resolved the wrong LUT project root")
        ),
    )

    assert len(fake_engine.render_calls) == 1
    assert fake_engine.render_calls[0]["input"].resolve() == neutral_path
    assert fake_engine.render_calls[0]["strength"] == 150
    assert fake_engine.render_calls[0]["overwrite"] is True
    candidate = result["recommendation"]["groups"][0]["top3"][0]
    assert candidate["lut_id"] == "cube-test"
    assert candidate["lut_hash"] == "lut-hash"
    assert candidate["look_kind"] == "rendered_lut"
    assert candidate["xmp_compatible"] is False
    assert candidate["amount_supported"] is True
    assert candidate["amount"] == candidate["strength"] == 150
    assert Path(candidate["preview_path"]).is_file()
    assert result["recommendation"]["groups"][0]["selected"]["kind"] == "lut"

    selected = result["develop"]["creative_style"]["groups"]["1"]
    assert selected["status"] == "confirmed"
    assert selected["preset_id"] is None
    assert selected["lut_id"] == "cube-test"
    assert selected["lut_hash"] == "lut-hash"
    assert selected["look_kind"] == "rendered_lut"
    assert selected["xmp_compatible"] is False
    assert selected["amount"] == selected["strength"] == 150
    assert selected["recommended_preset_id"] == recommended_before
    assert selected["top3"][0]["lut_id"] == "cube-test"
    assert result["develop"]["revision"] == 5


def test_forced_lut_preview_requires_existing_lightroom_base(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "project" / ".runtime"
    data_dir, run_dir, _raw = _run(runtime_root)

    with pytest.raises(StyleWorkerError, match="Lightroom.*sRGB 预览"):
        run_style_worker(
            run_dir,
            data_dir,
            "style-lut-no-base",
            3,
            group_id=1,
            lut_id="cube-test",
            lut_hash="lut-hash",
            preview_only=True,
            bridge_status_reader=lambda _path: pytest.fail(
                "missing base must fail before checking Lightroom"
            ),
            lut_engine_factory=lambda _root: pytest.fail(
                "missing base must fail before opening the LUT engine"
            ),
        )

    failed = read_json(run_dir / "develop.json")
    assert failed["creative_style"]["groups"]["1"]["recommendation_status"] == "failed"


def test_forced_lut_preview_rejects_changed_lut_hash_before_render(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "project" / ".runtime"
    data_dir, run_dir, _raw = _run(runtime_root)
    lightroom = _FakeLightroom()
    run_style_worker(
        run_dir,
        data_dir,
        "style-before-stale-lut",
        3,
        catalog=_catalog(7),
        bridge_status_reader=_online,
        batch_creator=lightroom.create,
        batch_waiter=lightroom.wait,
        scorer_factory=_FakeScorer,
    )
    fake_engine = _FakeLutEngine(tmp_path / "project", lut_hash="new-hash")

    with pytest.raises(StyleWorkerError, match="LUT 版本已经变化"):
        run_style_worker(
            run_dir,
            data_dir,
            "style-stale-lut",
            4,
            group_id=1,
            lut_id="cube-test",
            lut_hash="old-hash",
            amount=80,
            preview_only=True,
            lut_engine_factory=lambda _root: fake_engine,
        )

    assert fake_engine.render_calls == []


def test_cached_worker_still_rejects_legacy_plugin(tmp_path: Path) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    catalog = _catalog()
    lightroom = _FakeLightroom()
    run_style_worker(
        run_dir,
        data_dir,
        "style-cache-current",
        3,
        catalog=catalog,
        bridge_status_reader=_online,
        batch_creator=lightroom.create,
        batch_waiter=lightroom.wait,
        scorer_factory=_FakeScorer,
    )
    assert len(list((run_dir / "style-previews").glob("*.jpg"))) == 4

    with pytest.raises(StyleWorkerError, match=REQUIRED_PREVIEW_PLUGIN_VERSION):
        run_style_worker(
            run_dir,
            data_dir,
            "style-cache-legacy",
            4,
            catalog=catalog,
            bridge_status_reader=lambda _path: {
                "heartbeat": {"state": "online", "plugin_version": "0.1.2"}
            },
            batch_creator=lambda *_args, **_kwargs: pytest.fail(
                "cached run must not create a batch"
            ),
            batch_waiter=lambda *_args, **_kwargs: pytest.fail(
                "cached run must not wait for a batch"
            ),
            scorer_factory=lambda _path: pytest.fail(
                "legacy plugin must fail before cached previews are scored"
            ),
        )


def test_worker_rejects_external_jpeg_and_persists_failed_state(
    tmp_path: Path,
) -> None:
    data_dir, run_dir, _raw = _run(tmp_path)
    _add_second_group(run_dir, tmp_path)
    outside = tmp_path / "outside-worker-output.jpg"
    Image.new("RGB", (96, 64), (80, 100, 120)).save(outside, "JPEG")
    rows: list[dict[str, Any]] = []

    def create_batch(_data_dir: Path, tasks: list[Any], *, batch_id: str) -> dict:
        rows.extend(
            {
                "task_id": task.task_id,
                "status": "done",
                "result": {
                    "batch_id": batch_id,
                    "task_id": task.task_id,
                    "restore_status": "done",
                    "isolation_kind": "virtual_copy",
                    "isolation_status": "removed",
                    "jpeg_path": str(outside),
                },
            }
            for task in tasks
        )
        return {"batch_id": batch_id, "status": "pending"}

    def wait_batch(
        _data_dir: Path,
        batch_id: str,
        _timeout: float,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {"batch_id": batch_id, "status": "complete", "tasks": rows}

    with pytest.raises(StyleWorkerError, match="超出本次预览缓存范围"):
        run_style_worker(
            run_dir,
            data_dir,
            "style-batch-outside",
            3,
            catalog=_catalog(),
            bridge_status_reader=_online,
            batch_creator=create_batch,
            batch_waiter=wait_batch,
            scorer_factory=lambda _path: pytest.fail(
                "invalid output must not be scored"
            ),
        )

    assert outside.is_file()
    assert not list((run_dir / "style-previews").glob("*.jpg"))
    recommendation = read_json(run_dir / "style-recommendations.json")
    assert recommendation["status"] == "failed"
    assert recommendation["worker"]["status"] == "failed"
    develop = read_json(run_dir / "develop.json")
    assert develop["revision"] == 4
    failed_groups = develop["creative_style"]["groups"]
    assert set(failed_groups) == {"1", "2"}
    assert all(
        group["recommendation_status"] == "failed"
        for group in failed_groups.values()
    )


@pytest.mark.parametrize(
    ("isolation_kind", "isolation_status", "expected"),
    [
        (None, "removed", "隔离虚拟副本"),
        ("catalog", "removed", "隔离虚拟副本"),
        ("virtual_copy", None, "已经移除"),
        ("virtual_copy", "active", "已经移除"),
    ],
)
def test_secure_preview_collection_requires_removed_virtual_copy_isolation(
    tmp_path: Path,
    isolation_kind: str | None,
    isolation_status: str | None,
    expected: str,
) -> None:
    incoming = tmp_path / "incoming"
    source = incoming / "preview.jpg"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (32, 24), (70, 90, 110)).save(source, "JPEG")

    with pytest.raises(StyleWorkerError, match=expected):
        style_worker_module._secure_collect_jpeg(
            {
                "batch_id": "style-batch",
                "task_id": "candidate-1",
                "restore_status": "done",
                "isolation_kind": isolation_kind,
                "isolation_status": isolation_status,
                "jpeg_path": str(source),
            },
            {},
            incoming_root=incoming,
            cache_target=tmp_path / "cache" / "preview.jpg",
            batch_id="style-batch",
            task_id="candidate-1",
        )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {"look_status": "failed", "look_uuid": "LOOK-1", "look_amount": 135},
            "完整应用",
        ),
        (
            {"look_status": "done", "look_uuid": "LOOK-2", "look_amount": 135},
            "UUID 不匹配",
        ),
        (
            {"look_status": "done", "look_uuid": "LOOK-1", "look_amount": 100},
            "强度不匹配",
        ),
        (
            {"look_status": "done", "look_uuid": "LOOK-1", "look_amount": 135.5},
            "强度不匹配",
        ),
    ],
)
def test_creative_look_result_must_confirm_exact_uuid_and_amount(
    result: dict[str, Any],
    expected: str,
) -> None:
    with pytest.raises(StyleWorkerError, match=expected):
        style_worker_module._validate_look_result(
            result,
            {"look_uuid": "LOOK-1", "amount": 135},
        )

    style_worker_module._validate_look_result(
        {"look_status": "done", "look_uuid": "LOOK-1", "look_amount": 135},
        {"look_uuid": "LOOK-1", "amount": 135},
    )
    style_worker_module._validate_look_result(
        {"look_status": "done", "look_uuid": "LOOK-1", "look_amount": 135.0},
        {"look_uuid": "LOOK-1", "amount": 135},
    )
