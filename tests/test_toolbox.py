from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import pytest

import landscape_culler.toolbox as toolbox
from landscape_culler.toolbox import (
    TRASH_DIRECTORY,
    create_raw_jpeg_plan,
    execute_raw_jpeg_plan,
    list_raw_jpeg_transactions,
    rollback_raw_jpeg_manifest,
)
from landscape_culler.util import read_json


RAW_EXTENSIONS = ["arw", "nef"]
JPEG_EXTENSIONS = ["jpg", "jpeg"]


def _write(path: Path, content: bytes = b"photo") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _plan(data_dir: Path, root: Path, direction: str = "both", recursive: bool = False) -> dict:
    return create_raw_jpeg_plan(
        data_dir,
        layout="mixed",
        direction=direction,
        mixed_root=root,
        raw_root=None,
        jpeg_root=None,
        raw_extensions=RAW_EXTENSIONS,
        jpeg_extensions=JPEG_EXTENSIONS,
        recursive=recursive,
    )


def test_mixed_pairing_directions_and_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    root.mkdir()
    _write(root / "A.ARW", b"a-raw")
    _write(root / "a.JPG", b"a-jpeg")
    _write(root / "B.ARW", b"b-raw")
    _write(root / "C.JPG", b"c-jpeg")
    _write(root / "D.JPG", b"d-jpg")
    _write(root / "D.JPEG", b"d-jpeg")

    jpeg_reference = _plan(data_dir, root, "jpeg")
    assert jpeg_reference["paired_count"] == 1
    assert jpeg_reference["raw_only_count"] == 1
    assert jpeg_reference["jpeg_only_count"] == 3
    assert [Path(item["path"]).name for item in jpeg_reference["candidates"]] == ["B.ARW"]

    raw_reference = _plan(data_dir, root, "raw")
    assert [Path(item["path"]).name for item in raw_reference["candidates"]] == ["C.JPG"]
    assert any("同名多格式" in warning for warning in raw_reference["warnings"])

    both = _plan(data_dir, root, "both")
    assert {Path(item["path"]).name for item in both["candidates"]} == {"B.ARW", "C.JPG"}
    assert both["ambiguous_count"] == 1
    assert both["complete"] is True


def test_recursive_pairing_uses_relative_directory(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    _write(root / "one" / "same.ARW")
    _write(root / "two" / "same.JPG")

    shallow = _plan(data_dir, root, "both", recursive=False)
    assert shallow["candidate_count"] == 0

    recursive = _plan(data_dir, root, "both", recursive=True)
    assert recursive["paired_count"] == 0
    assert recursive["candidate_count"] == 2


def test_separate_mode_pairs_matching_relative_paths(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    jpeg_root = tmp_path / "jpeg"
    data_dir = tmp_path / "data"
    _write(raw_root / "day1" / "A.ARW")
    _write(jpeg_root / "day1" / "a.JPG")
    _write(raw_root / "day2" / "B.ARW")

    plan = create_raw_jpeg_plan(
        data_dir,
        layout="separate",
        direction="jpeg",
        mixed_root=None,
        raw_root=raw_root,
        jpeg_root=jpeg_root,
        raw_extensions=RAW_EXTENSIONS,
        jpeg_extensions=JPEG_EXTENSIONS,
        recursive=True,
    )
    assert plan["paired_count"] == 1
    assert [item["relative_path"] for item in plan["candidates"]] == ["day2/B.ARW"]


def test_compound_raw_xmp_sidecar_moves_and_rolls_back_with_photo(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    raw = _write(root / "A.NEF", b"raw")
    sidecar = _write(root / "A.NEF.xmp", b"develop settings")

    plan = _plan(data_dir, root, "jpeg")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    transaction_root = data_dir / "toolbox" / "raw-jpeg" / "transactions"
    result = execute_raw_jpeg_plan(plan_file, transaction_root)
    manifest_file = transaction_root / f"{plan['plan_id']}.json"
    manifest = read_json(manifest_file)

    assert result["moved_count"] == 2
    assert {Path(record["original_path"]) for record in manifest["records"]} == {raw, sidecar}
    assert not raw.exists()
    assert not sidecar.exists()
    assert all(Path(record["trash_path"]).is_file() for record in manifest["records"])

    restored = rollback_raw_jpeg_manifest(manifest_file)
    assert restored["status"] == "rolled_back"
    assert restored["restored_count"] == 2
    assert raw.read_bytes() == b"raw"
    assert sidecar.read_bytes() == b"develop settings"


def test_separate_mode_cross_root_xmp_moves_and_rolls_back_with_photo(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    jpeg_root = tmp_path / "jpeg"
    data_dir = tmp_path / "data"
    raw = _write(raw_root / "day1" / "A.NEF", b"raw")
    sidecar = _write(jpeg_root / "day1" / "A.XMP", b"cross-root settings")

    plan = create_raw_jpeg_plan(
        data_dir,
        layout="separate",
        direction="jpeg",
        mixed_root=None,
        raw_root=raw_root,
        jpeg_root=jpeg_root,
        raw_extensions=RAW_EXTENSIONS,
        jpeg_extensions=JPEG_EXTENSIONS,
        recursive=True,
    )
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    transaction_root = data_dir / "toolbox" / "raw-jpeg" / "transactions"
    result = execute_raw_jpeg_plan(plan_file, transaction_root)
    manifest_file = transaction_root / f"{plan['plan_id']}.json"
    manifest = read_json(manifest_file)

    assert result["moved_count"] == 2
    records = {Path(record["original_path"]): record for record in manifest["records"]}
    assert set(records) == {raw, sidecar}
    assert Path(records[raw]["trash_path"]).is_relative_to(raw_root)
    assert Path(records[sidecar]["trash_path"]).is_relative_to(jpeg_root)
    assert not raw.exists()
    assert not sidecar.exists()

    restored = rollback_raw_jpeg_manifest(manifest_file)
    assert restored["status"] == "rolled_back"
    assert restored["restored_count"] == 2
    assert raw.read_bytes() == b"raw"
    assert sidecar.read_bytes() == b"cross-root settings"


def test_heic_and_heif_pair_with_raw_when_finished_photo_is_reference(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    root.mkdir()
    _write(root / "A.NEF", b"a-raw")
    _write(root / "a.HEIC", b"a-final")
    _write(root / "B.ARW", b"b-raw")
    _write(root / "B.HEIF", b"b-final")
    _write(root / "C.NEF", b"orphan")

    assert {".heic", ".heif"} <= set(toolbox.DEFAULT_JPEG_EXTENSIONS)
    plan = create_raw_jpeg_plan(
        data_dir,
        layout="mixed",
        direction="jpeg",
        mixed_root=root,
        raw_root=None,
        jpeg_root=None,
        raw_extensions=RAW_EXTENSIONS,
        jpeg_extensions=toolbox.DEFAULT_JPEG_EXTENSIONS,
        recursive=False,
    )

    assert plan["paired_count"] == 2
    assert plan["raw_only_count"] == 1
    assert plan["jpeg_only_count"] == 0
    assert [Path(item["path"]).name for item in plan["candidates"]] == ["C.NEF"]


def test_execute_moves_to_same_root_trash_and_rollback_restores(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    orphan = _write(root / "B.ARW", b"raw-content")
    plan = _plan(data_dir, root, "jpeg")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    transaction_root = data_dir / "toolbox" / "raw-jpeg" / "transactions"

    result = execute_raw_jpeg_plan(plan_file, transaction_root)
    trash = root / TRASH_DIRECTORY / "raw-jpeg" / plan["plan_id"] / "raw" / "B.ARW"
    assert result["moved_count"] == 1
    assert result["rollbackable"] is True
    assert not orphan.exists()
    assert trash.read_bytes() == b"raw-content"

    restored = rollback_raw_jpeg_manifest(transaction_root / f"{plan['plan_id']}.json")
    assert restored["rollbackable"] is False
    assert restored["restored_count"] == 1
    assert orphan.read_bytes() == b"raw-content"
    assert not trash.exists()


def test_execute_rejects_stale_plan_before_any_move(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    orphan = _write(root / "B.ARW", b"raw-content")
    plan = _plan(data_dir, root, "jpeg")
    _write(root / "B.JPG", b"new-counterpart")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"

    with pytest.raises(ValueError, match="目录内容已变化"):
        execute_raw_jpeg_plan(plan_file, data_dir / "toolbox" / "raw-jpeg" / "transactions")
    assert orphan.read_bytes() == b"raw-content"
    assert not (root / TRASH_DIRECTORY).exists()


def test_execute_detects_same_size_content_replacement(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    orphan = _write(root / "B.ARW", b"original")
    plan = _plan(data_dir, root, "jpeg")
    original_stat = orphan.stat()
    orphan.write_bytes(b"replaced")
    os.utime(orphan, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"

    with pytest.raises(ValueError, match="文件已变化"):
        execute_raw_jpeg_plan(plan_file, data_dir / "toolbox" / "raw-jpeg" / "transactions")
    assert orphan.read_bytes() == b"replaced"
    assert not (root / TRASH_DIRECTORY).exists()


def test_execute_rejects_sidecar_added_after_preview(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    orphan = _write(root / "B.ARW", b"raw-content")
    plan = _plan(data_dir, root, "jpeg")
    _write(root / "B.ARW.xmp", b"new-sidecar")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"

    with pytest.raises(ValueError, match="XMP"):
        execute_raw_jpeg_plan(plan_file, data_dir / "toolbox" / "raw-jpeg" / "transactions")
    assert orphan.read_bytes() == b"raw-content"
    assert not (root / TRASH_DIRECTORY).exists()


def test_recursive_scan_never_reimports_toolbox_trash(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    _write(root / "live.ARW")
    _write(root / TRASH_DIRECTORY / "raw-jpeg" / "old" / "raw" / "ignored.ARW")

    plan = _plan(data_dir, root, "jpeg", recursive=True)
    assert [Path(item["path"]).name for item in plan["candidates"]] == ["live.ARW"]


def test_selecting_toolbox_trash_as_scan_root_is_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    trash_root = tmp_path / "photos" / TRASH_DIRECTORY
    trash_root.mkdir(parents=True)
    _write(trash_root / "orphan.ARW")

    with pytest.raises(ValueError, match="回收|photo-ai-trash"):
        _plan(data_dir, trash_root, "jpeg", recursive=True)


def test_rollback_never_overwrites_original_path(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    original = _write(root / "B.ARW", b"old")
    plan = _plan(data_dir, root, "jpeg")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    transaction_root = data_dir / "toolbox" / "raw-jpeg" / "transactions"
    execute_raw_jpeg_plan(plan_file, transaction_root)
    _write(original, b"replacement")

    result = rollback_raw_jpeg_manifest(transaction_root / f"{plan['plan_id']}.json")
    assert result["status"] != "rolled_back"
    assert result["restored_count"] == 0
    assert result["rollbackable"] is True
    assert original.read_bytes() == b"replacement"
    assert (root / TRASH_DIRECTORY / "raw-jpeg" / plan["plan_id"] / "raw" / "B.ARW").read_bytes() == b"old"


@pytest.mark.parametrize("conflict", ["both_missing", "both_present", "trash_modified"])
def test_rollback_truth_table_never_reports_conflicts_as_rolled_back(tmp_path: Path, conflict: str) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    original = _write(root / "B.ARW", b"original")
    plan = _plan(data_dir, root, "jpeg")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    transaction_root = data_dir / "toolbox" / "raw-jpeg" / "transactions"
    execute_raw_jpeg_plan(plan_file, transaction_root)
    manifest_file = transaction_root / f"{plan['plan_id']}.json"
    manifest = read_json(manifest_file)
    record = next(item for item in manifest["records"] if Path(item["original_path"]) == original)
    trash = Path(record["trash_path"])

    if conflict == "both_missing":
        trash.unlink()
    elif conflict == "both_present":
        original.write_bytes(b"external replacement")
    else:
        trash.write_bytes(b"tampered trash content")

    result = rollback_raw_jpeg_manifest(manifest_file)
    persisted = read_json(manifest_file)
    persisted_record = next(item for item in persisted["records"] if Path(item["original_path"]) == original)

    assert result["status"] != "rolled_back"
    assert result["restored_count"] == 0
    assert persisted["status"] != "rolled_back"
    assert persisted_record["location_state"] == {
        "both_missing": "lost",
        "both_present": "conflict",
        "trash_modified": "modified",
    }[conflict]


def test_same_plan_concurrent_execution_has_one_clean_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    original = _write(root / "B.ARW", b"raw-content")
    plan = _plan(data_dir, root, "jpeg")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    transaction_root = data_dir / "toolbox" / "raw-jpeg" / "transactions"
    manifest_file = transaction_root / f"{plan['plan_id']}.json"
    load_barrier = Barrier(2)
    exists_barrier = Barrier(2)
    real_load_plan = toolbox.load_plan
    real_exists = Path.exists

    def rendezvous(barrier: Barrier) -> None:
        try:
            barrier.wait(timeout=0.75)
        except BrokenBarrierError:
            pass

    def synchronized_load(path: Path) -> dict:
        payload = real_load_plan(path)
        rendezvous(load_barrier)
        return payload

    def synchronized_exists(path: Path) -> bool:
        result = real_exists(path)
        if path == manifest_file:
            rendezvous(exists_barrier)
        return result

    monkeypatch.setattr(toolbox, "load_plan", synchronized_load)
    monkeypatch.setattr(Path, "exists", synchronized_exists)

    def execute() -> tuple[str, object]:
        try:
            return "ok", execute_raw_jpeg_plan(plan_file, transaction_root)
        except Exception as exc:  # The losing request must fail closed, not corrupt the transaction.
            return "error", exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: execute(), range(2)))

    successes = [value for status, value in outcomes if status == "ok"]
    failures = [value for status, value in outcomes if status == "error"]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], (ValueError, RuntimeError))
    assert any(word in str(failures[0]) for word in ("执行", "计划", "操作记录", "锁", "运行"))
    assert not original.exists()
    manifest = read_json(manifest_file)
    assert manifest["status"] == "completed"
    assert manifest["moved_count"] == 1
    assert manifest["failed_count"] == 0
    assert len(manifest["records"]) == 1


def test_partial_move_failure_keeps_manifest_and_can_restore_moved_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    first = _write(root / "A.ARW", b"first")
    second = _write(root / "B.ARW", b"second")
    plan = _plan(data_dir, root, "jpeg")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    transaction_root = data_dir / "toolbox" / "raw-jpeg" / "transactions"
    real_rename = Path.rename
    calls = 0

    def flaky_rename(source: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated network failure")
        return real_rename(source, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    result = execute_raw_jpeg_plan(plan_file, transaction_root)
    assert result["moved_count"] == 1
    assert result["failed_count"] == 1
    assert not first.exists()
    assert second.read_bytes() == b"second"

    monkeypatch.setattr(Path, "rename", real_rename)
    restored = rollback_raw_jpeg_manifest(transaction_root / f"{plan['plan_id']}.json")
    assert restored["remaining_count"] == 0
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_manifest_write_failure_happens_before_any_photo_move(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    orphan = _write(root / "B.ARW", b"raw-content")
    plan = _plan(data_dir, root, "jpeg")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    transaction_root = data_dir / "toolbox" / "raw-jpeg" / "transactions"
    real_write_json = toolbox.write_json

    def fail_manifest(path: Path, payload: object) -> None:
        if path.parent == transaction_root:
            raise OSError("simulated manifest failure")
        real_write_json(path, payload)

    monkeypatch.setattr(toolbox, "write_json", fail_manifest)
    with pytest.raises(OSError, match="manifest failure"):
        execute_raw_jpeg_plan(plan_file, transaction_root)
    assert orphan.read_bytes() == b"raw-content"
    assert not (root / TRASH_DIRECTORY).exists()


def test_transaction_manifests_stay_in_data_directory(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    _write(root / "B.ARW")
    plan = _plan(data_dir, root, "jpeg")
    plan_file = data_dir / "toolbox" / "raw-jpeg" / "plans" / f"{plan['plan_id']}.json"
    execute_raw_jpeg_plan(plan_file, data_dir / "toolbox" / "raw-jpeg" / "transactions")

    transactions, mapping = list_raw_jpeg_transactions(data_dir)
    assert transactions[0]["transaction_id"] == plan["plan_id"]
    assert mapping[plan["plan_id"]].is_relative_to(data_dir)
