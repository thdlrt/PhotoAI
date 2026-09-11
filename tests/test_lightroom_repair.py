from __future__ import annotations

from pathlib import Path

import pytest

from landscape_culler.lightroom_bridge import RESULT_PROTOCOL, _line, bridge_paths
from landscape_culler.lightroom_repair import (
    LightroomRepairError,
    execute_preview_metadata_repair,
    find_preview_metadata_repair_candidates,
)


def _legacy_result(data_dir: Path, raw: Path, stem: str, backup: bytes) -> None:
    paths = bridge_paths(data_dir, create=True)
    fields = {
        "batch_id": stem.split("--", 1)[0],
        "task_id": stem.split("--", 1)[1],
        "photo_path": str(raw),
        "status": "done",
        "finished_at": "2026-09-01T12:00:00Z",
        "message": "rollback=restored-from-settings; xmp_restore=byte-identical-backup",
        "task_type": "preview",
        "output_mode": "jpeg",
        "xmp_status": "not_requested",
        "jpeg_status": "done",
        "restore_status": "done",
    }
    (paths.done / f"{stem}.result").write_text(
        _line(RESULT_PROTOCOL, fields), encoding="utf-8"
    )
    (paths.backups / f"{stem}.xmp.original").write_bytes(backup)


def test_repair_scan_requires_every_legacy_backup_to_match_current_xmp(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    raw = tmp_path / "A.ARW"
    raw.write_bytes(b"raw")
    raw.with_suffix(".xmp").write_bytes(b"original-xmp")
    _legacy_result(data_dir, raw, "style-run--one", b"original-xmp")
    _legacy_result(data_dir, raw, "style-run--two", b"original-xmp")

    report = find_preview_metadata_repair_candidates(data_dir)

    assert report["eligible_count"] == 1
    assert report["unsafe_count"] == 0
    assert report["legacy_preview_result_count"] == 2
    assert report["eligible"][0]["preview_task_count"] == 2
    assert report["eligible"][0]["matching_backup_count"] == 2


def test_repair_scan_refuses_changed_or_missing_metadata(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    changed = tmp_path / "changed.ARW"
    changed.write_bytes(b"raw")
    changed.with_suffix(".xmp").write_bytes(b"later-external-edit")
    _legacy_result(data_dir, changed, "style-run--changed", b"original-xmp")

    missing = tmp_path / "missing.ARW"
    missing.write_bytes(b"raw")
    _legacy_result(data_dir, missing, "style-run--missing", b"original-xmp")

    report = find_preview_metadata_repair_candidates(data_dir)

    assert report["eligible_count"] == 0
    assert report["unsafe_count"] == 2
    reasons = " ".join(reason for row in report["unsafe"] for reason in row["reasons"])
    assert "当前 XMP 与预览前备份不同" in reasons
    assert "当前 XMP 不存在" in reasons


def test_repair_scan_ignores_new_noop_preview_results(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    raw = tmp_path / "A.ARW"
    raw.write_bytes(b"raw")
    paths = bridge_paths(data_dir, create=True)
    fields = {
        "batch_id": "style-new",
        "task_id": "one",
        "photo_path": str(raw),
        "status": "done",
        "finished_at": "2026-09-01T12:00:00Z",
        "message": "rollback=restored-from-settings; xmp_unchanged=preserved-in-place",
        "task_type": "preview",
        "output_mode": "jpeg",
        "restore_status": "done",
    }
    (paths.done / "style-new--one.result").write_text(
        _line(RESULT_PROTOCOL, fields), encoding="utf-8"
    )

    report = find_preview_metadata_repair_candidates(data_dir)

    assert report["eligible_count"] == 0
    assert report["legacy_preview_result_count"] == 0


def _completed_repair_status(
    raw: Path,
    *,
    batch_id: str = "controlled-repair",
    xmp_status: str = "done",
    cleanup_count: object = 2,
) -> dict[str, object]:
    result: dict[str, object] = {
        "batch_id": batch_id,
        "task_id": "repair-000001",
        "photo_path": str(raw.resolve()),
        "status": "done",
        "task_type": "repair_preview_metadata",
        "output_mode": "xmp",
        "xmp_status": xmp_status,
    }
    if cleanup_count is not None:
        result["cleanup_count"] = cleanup_count
    return {
        "batch_id": batch_id,
        "status": "complete",
        "counts": {"done": 1},
        "tasks": [
            {
                "task_id": "repair-000001",
                "photo_path": str(raw.resolve()),
                "status": "done",
                "result": result,
            }
        ],
    }


@pytest.mark.parametrize("xmp_status", ["done", "committed"])
def test_controlled_repair_audits_full_raw_and_xmp_content(
    tmp_path: Path,
    xmp_status: str,
) -> None:
    data_dir = tmp_path / "data"
    raw = tmp_path / "A.ARW"
    raw.write_bytes(b"raw-complete-file-content")
    xmp = raw.with_suffix(".xmp")
    xmp.write_bytes(b"original-xmp")
    _legacy_result(data_dir, raw, "style-run--one", b"original-xmp")
    approved = find_preview_metadata_repair_candidates(data_dir)
    calls: list[tuple[str, object]] = []

    def create(
        _data_dir: Path, photos: list[str], *, batch_id: str | None
    ) -> dict[str, object]:
        calls.append(("create", (photos, batch_id)))
        return {
            "batch_id": "controlled-repair",
            "task_count": 1,
            "tasks": [
                {
                    "task_id": "repair-000001",
                    "photo_path": str(raw.resolve()),
                    "status": "pending",
                }
            ],
        }

    def wait(_data_dir: Path, batch_id: str, timeout: float) -> dict[str, object]:
        calls.append(("wait", (batch_id, timeout)))
        xmp.write_bytes(b"catalog-metadata-after-repair")
        return _completed_repair_status(raw, xmp_status=xmp_status)

    report = execute_preview_metadata_repair(
        data_dir,
        approved,
        timeout=45,
        batch_creator=create,
        batch_waiter=wait,
    )

    assert [call[0] for call in calls] == ["create", "wait"]
    assert report["status"] == "complete"
    assert report["candidate_count"] == 1
    assert report["published_count"] == 1
    assert report["total_cleanup_count"] == 2
    assert report["accepted_xmp_statuses"] == ["committed", "done"]
    photo = report["photos"][0]
    assert photo["raw_before"] == photo["raw_after"]
    assert photo["raw_unchanged"] is True
    assert photo["raw_before"]["size"] == len(b"raw-complete-file-content")
    assert len(photo["raw_before"]["sha256"]) == 64
    assert photo["xmp_before"]["size"] == len(b"original-xmp")
    assert photo["xmp_after"]["size"] == len(b"catalog-metadata-after-repair")
    assert photo["xmp_status"] == xmp_status
    assert photo["cleanup_count"] == 2


def test_controlled_repair_rejects_unsafe_rescan_before_publication(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    raw = tmp_path / "A.ARW"
    raw.write_bytes(b"raw")
    raw.with_suffix(".xmp").write_bytes(b"original-xmp")
    _legacy_result(data_dir, raw, "style-run--one", b"original-xmp")
    approved = find_preview_metadata_repair_candidates(data_dir)
    raw.with_suffix(".xmp").write_bytes(b"later-edit")

    def should_not_create(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("unsafe repair reached queue publication")

    with pytest.raises(LightroomRepairError, match="不安全照片"):
        execute_preview_metadata_repair(
            data_dir, approved, batch_creator=should_not_create
        )


def test_controlled_repair_rejects_candidate_drift_before_publication(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    first = tmp_path / "A.ARW"
    first.write_bytes(b"raw-a")
    first.with_suffix(".xmp").write_bytes(b"xmp-a")
    _legacy_result(data_dir, first, "style-run--one", b"xmp-a")
    approved = find_preview_metadata_repair_candidates(data_dir)

    second = tmp_path / "B.ARW"
    second.write_bytes(b"raw-b")
    second.with_suffix(".xmp").write_bytes(b"xmp-b")
    _legacy_result(data_dir, second, "style-run--two", b"xmp-b")

    def should_not_create(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("drifted repair reached queue publication")

    with pytest.raises(LightroomRepairError, match="候选集合已漂移.*新增 1"):
        execute_preview_metadata_repair(
            data_dir, approved, batch_creator=should_not_create
        )


def test_controlled_repair_rescans_again_after_full_raw_hashing(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    first = tmp_path / "A.ARW"
    first.write_bytes(b"raw-a")
    first.with_suffix(".xmp").write_bytes(b"xmp-a")
    _legacy_result(data_dir, first, "style-run--one", b"xmp-a")
    approved = find_preview_metadata_repair_candidates(data_dir)
    scans = 0

    def drift_after_first_scan(data: Path | str) -> dict[str, object]:
        nonlocal scans
        scans += 1
        report = find_preview_metadata_repair_candidates(data)
        if scans == 1:
            second = tmp_path / "B.ARW"
            second.write_bytes(b"raw-b")
            second.with_suffix(".xmp").write_bytes(b"xmp-b")
            _legacy_result(data_dir, second, "style-run--two", b"xmp-b")
        return report

    def should_not_create(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("post-hash drift reached queue publication")

    with pytest.raises(LightroomRepairError, match="发布前再次漂移.*新增 1"):
        execute_preview_metadata_repair(
            data_dir,
            approved,
            scanner=drift_after_first_scan,
            batch_creator=should_not_create,
        )
    assert scans == 2


def test_controlled_repair_closes_xmp_rescan_to_publish_race(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    raw = tmp_path / "A.ARW"
    raw.write_bytes(b"raw")
    xmp = raw.with_suffix(".xmp")
    xmp.write_bytes(b"original-xmp")
    _legacy_result(data_dir, raw, "style-run--one", b"original-xmp")
    approved = find_preview_metadata_repair_candidates(data_dir)

    def scan_then_mutate(data: Path | str) -> dict[str, object]:
        report = find_preview_metadata_repair_candidates(data)
        xmp.write_bytes(b"changed-after-rescan")
        return report

    def should_not_create(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("changed XMP reached queue publication")

    with pytest.raises(LightroomRepairError, match="即时重扫后再次发生变化"):
        execute_preview_metadata_repair(
            data_dir,
            approved,
            scanner=scan_then_mutate,
            batch_creator=should_not_create,
        )


def test_controlled_repair_detects_raw_mutation_even_after_done_result(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    raw = tmp_path / "A.ARW"
    raw.write_bytes(b"raw-before")
    raw.with_suffix(".xmp").write_bytes(b"xmp")
    _legacy_result(data_dir, raw, "style-run--one", b"xmp")
    approved = find_preview_metadata_repair_candidates(data_dir)

    def create(
        _data_dir: Path, _photos: list[str], *, batch_id: str | None
    ) -> dict[str, object]:
        return {
            "batch_id": batch_id or "controlled-repair",
            "task_count": 1,
            "tasks": [{"photo_path": str(raw.resolve())}],
        }

    def wait(_data_dir: Path, _batch_id: str, _timeout: float) -> dict[str, object]:
        raw.write_bytes(b"raw-after")
        return _completed_repair_status(raw)

    with pytest.raises(LightroomRepairError, match="RAW 在修复期间发生变化"):
        execute_preview_metadata_repair(
            data_dir,
            approved,
            batch_creator=create,
            batch_waiter=wait,
        )


@pytest.mark.parametrize(
    ("xmp_status", "cleanup_count", "message"),
    [
        ("running", 1, "未确认 XMP 已提交"),
        ("done", None, "cleanup_count"),
        ("done", True, "cleanup_count"),
    ],
)
def test_controlled_repair_rejects_incomplete_protocol_results(
    tmp_path: Path,
    xmp_status: str,
    cleanup_count: object,
    message: str,
) -> None:
    data_dir = tmp_path / "data"
    raw = tmp_path / "A.ARW"
    raw.write_bytes(b"raw")
    raw.with_suffix(".xmp").write_bytes(b"xmp")
    _legacy_result(data_dir, raw, "style-run--one", b"xmp")
    approved = find_preview_metadata_repair_candidates(data_dir)

    def create(
        _data_dir: Path, _photos: list[str], *, batch_id: str | None
    ) -> dict[str, object]:
        return {
            "batch_id": batch_id or "controlled-repair",
            "task_count": 1,
            "tasks": [{"photo_path": str(raw.resolve())}],
        }

    def wait(_data_dir: Path, _batch_id: str, _timeout: float) -> dict[str, object]:
        return _completed_repair_status(
            raw,
            xmp_status=xmp_status,
            cleanup_count=cleanup_count,
        )

    with pytest.raises(LightroomRepairError, match=message):
        execute_preview_metadata_repair(
            data_dir,
            approved,
            batch_creator=create,
            batch_waiter=wait,
        )
