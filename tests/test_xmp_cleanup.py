from __future__ import annotations

import os
from pathlib import Path

import pytest

from landscape_culler import xmp_cleanup
from landscape_culler.toolbox import TRASH_DIRECTORY
from landscape_culler.util import read_json, write_json
from landscape_culler.xmp_cleanup import (
    create_xmp_cleanup_plan,
    execute_xmp_cleanup_plan,
    list_xmp_cleanup_transactions,
    plan_path,
    rollback_xmp_cleanup_manifest,
    transactions_root,
)


@pytest.fixture(autouse=True)
def _lightroom_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xmp_cleanup, "assert_lightroom_not_running", lambda: None)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _plan(data_dir: Path, root: Path, *, recursive: bool = True) -> dict:
    return create_xmp_cleanup_plan(data_dir, root=root, recursive=recursive)


def _execute(data_dir: Path, plan: dict) -> dict:
    return execute_xmp_cleanup_plan(
        plan_path(data_dir, plan["plan_id"]),
        transactions_root(data_dir),
    )


def _manifest_path(data_dir: Path, plan: dict) -> Path:
    return transactions_root(data_dir) / f"{plan['plan_id']}.json"


def _trash_path(root: Path, plan: dict, relative_path: str) -> Path:
    return (
        root
        / TRASH_DIRECTORY
        / "xmp-cleanup"
        / plan["plan_id"]
        / Path(relative_path)
    )


def _make_legacy_recycle_transaction(
    data_dir: Path, root: Path, plan: dict, relative_path: str
) -> Path:
    stored = read_json(plan_path(data_dir, plan["plan_id"]))
    candidate = next(
        item for item in stored["candidates"] if item["relative_path"] == relative_path
    )
    source = Path(candidate["path"])
    target = _trash_path(root, plan, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    manifest = {
        "schema_version": 1,
        "kind": "xmp_cleanup",
        # Legacy manifests intentionally have no operation field.
        "transaction_id": plan["plan_id"],
        "plan_id": plan["plan_id"],
        "created_at": "2026-09-02T00:00:00+00:00",
        "updated_at": "2026-09-02T00:00:00+00:00",
        "status": "completed",
        "root_path": str(root),
        "recursive": True,
        "removed_count": 1,
        "remaining_count": 1,
        "records": [
            {
                "transaction_id": plan["plan_id"],
                "root_path": str(root),
                "original_path": str(source),
                "trash_path": str(target),
                "relative_path": relative_path,
                "snapshot": candidate["snapshot"],
                "status": "moved",
            }
        ],
    }
    write_json(_manifest_path(data_dir, plan), manifest)
    return target


def test_scan_only_xmp_respects_recursion_and_skips_toolbox_trash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    top = _write(root / "top.xmp", b"top-sidecar")
    nested = _write(root / "day-1" / "nested.XMP", b"nested-sidecar")
    _write(root / "metadata.xml", b"not-an-xmp")
    _write(root / "photo.ARW", b"raw-must-not-move")
    _write(
        root / TRASH_DIRECTORY / "xmp-cleanup" / "old" / "ignored.xmp",
        b"already-recycled",
    )

    shallow = _plan(data_dir, root, recursive=False)
    recursive = _plan(data_dir, root, recursive=True)

    assert shallow["complete"] is True
    assert shallow["xmp_count"] == 1
    assert [Path(item["path"]) for item in shallow["candidates"]] == [top]
    assert recursive["complete"] is True
    assert recursive["xmp_count"] == 2
    assert {Path(item["path"]) for item in recursive["candidates"]} == {
        top,
        nested,
    }
    assert recursive["xmp_bytes"] == len(b"top-sidecar") + len(b"nested-sidecar")
    assert recursive["scan_errors"] == []
    assert recursive["candidates_truncated"] is False

    persisted = read_json(plan_path(data_dir, recursive["plan_id"]))
    assert all(len(item["snapshot"]["sha256"]) == 64 for item in persisted["candidates"])
    assert all("snapshot" not in item for item in recursive["candidates"])


def test_execute_permanently_deletes_only_xmp_and_records_result(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    raw = _write(root / "photo.ARW", b"raw-content")
    first_bytes = b"first-sidecar\x00\xff"
    second_bytes = "第二份 sidecar".encode()
    first = _write(root / "photo.xmp", first_bytes)
    second = _write(root / "nested" / "other.XMP", second_bytes)
    raw_before = raw.read_bytes()
    plan = _plan(data_dir, root)

    executed = _execute(data_dir, plan)

    assert executed["status"] == "completed"
    assert executed["operation"] == "delete"
    assert executed["xmp_count"] == 2
    assert executed["deleted_count"] == executed["removed_count"] == 2
    assert executed["moved_count"] == executed["remaining_count"] == 0
    assert executed["rollbackable"] is False
    assert not first.exists() and not second.exists()
    assert not _trash_path(root, plan, "photo.xmp").exists()
    assert not _trash_path(root, plan, "nested/other.XMP").exists()
    assert raw.read_bytes() == raw_before

    manifest_file = _manifest_path(data_dir, plan)
    manifest = read_json(manifest_file)
    assert manifest["kind"] == "xmp_cleanup"
    assert manifest["operation"] == "delete"
    assert all(len(item["snapshot"]["sha256"]) == 64 for item in manifest["records"])
    transactions, mapping = list_xmp_cleanup_transactions(data_dir)
    assert transactions[0]["transaction_id"] == plan["plan_id"]
    assert mapping[plan["plan_id"]] == manifest_file

    with pytest.raises(ValueError, match="永久删除记录无法恢复"):
        rollback_xmp_cleanup_manifest(manifest_file)


@pytest.mark.parametrize("change", ["added", "deleted", "modified"])
def test_plan_drift_is_rejected_before_any_xmp_moves(
    tmp_path: Path,
    change: str,
) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    first = _write(root / "first.xmp", b"first-original")
    second = _write(root / "second.xmp", b"second-original")
    plan = _plan(data_dir, root)

    if change == "added":
        _write(root / "added.xmp", b"added-after-plan")
    elif change == "deleted":
        second.unlink()
    else:
        first.write_bytes(b"first-modified")

    with pytest.raises(ValueError, match="集合已变化|XMP 已变化"):
        _execute(data_dir, plan)

    assert first.exists()
    if change != "deleted":
        assert second.exists()
    assert not _trash_path(root, plan, "first.xmp").exists()
    assert not _trash_path(root, plan, "second.xmp").exists()
    assert not _manifest_path(data_dir, plan).exists()


def test_full_sha_detects_same_size_middle_replacement_over_two_mebibytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    mebibyte = 1024 * 1024
    original = b"A" * mebibyte + b"B" * mebibyte + b"C" * mebibyte
    replacement = b"A" * mebibyte + b"D" * mebibyte + b"C" * mebibyte
    sidecar = _write(root / "large.xmp", original)
    plan = _plan(data_dir, root)
    old_stat = sidecar.stat()
    sidecar.write_bytes(replacement)
    os.utime(sidecar, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

    with pytest.raises(ValueError, match="XMP 已变化"):
        _execute(data_dir, plan)

    assert sidecar.read_bytes() == replacement
    assert not _trash_path(root, plan, "large.xmp").exists()
    assert not _manifest_path(data_dir, plan).exists()


def test_rollback_never_overwrites_new_xmp_at_original_path(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    original = _write(root / "photo.xmp", b"original-sidecar")
    plan = _plan(data_dir, root)
    trash = _make_legacy_recycle_transaction(data_dir, root, plan, "photo.xmp")
    original.write_bytes(b"new-external-sidecar")

    result = rollback_xmp_cleanup_manifest(_manifest_path(data_dir, plan))

    assert result["status"] == "partially_rolled_back"
    assert result["restored_count"] == 0
    assert result["conflict_count"] == 1
    assert original.read_bytes() == b"new-external-sidecar"
    assert trash.read_bytes() == b"original-sidecar"


def test_rollback_refuses_tampered_recycled_xmp(tmp_path: Path) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    original = _write(root / "photo.xmp", b"original-sidecar")
    plan = _plan(data_dir, root)
    trash = _make_legacy_recycle_transaction(data_dir, root, plan, "photo.xmp")
    trash.write_bytes(b"tampered-sidecar")

    result = rollback_xmp_cleanup_manifest(_manifest_path(data_dir, plan))

    assert result["status"] == "partially_rolled_back"
    assert result["restored_count"] == 0
    assert result["modified_count"] == 1
    assert not original.exists()
    assert trash.read_bytes() == b"tampered-sidecar"


def test_direct_delete_is_allowed_while_lightroom_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    sidecar = _write(root / "photo.xmp", b"sidecar")
    plan = _plan(data_dir, root)

    def running() -> None:
        raise RuntimeError("Lightroom 正在运行")

    monkeypatch.setattr(xmp_cleanup, "assert_lightroom_not_running", running)
    result = _execute(data_dir, plan)

    assert result["deleted_count"] == 1
    assert not sidecar.exists()
    assert not _trash_path(root, plan, "photo.xmp").exists()
    assert _manifest_path(data_dir, plan).exists()


def test_pre_delete_preview_plan_adopts_direct_delete_without_rescan(
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    sidecar = _write(root / "photo.xmp", b"sidecar")
    plan = _plan(data_dir, root)
    stored_path = plan_path(data_dir, plan["plan_id"])
    stored = read_json(stored_path)
    stored.pop("operation")
    write_json(stored_path, stored)

    result = _execute(data_dir, plan)

    assert result["operation"] == "delete"
    assert result["deleted_count"] == 1
    assert not sidecar.exists()


def test_plan_path_cannot_be_retargeted_to_another_equal_content_xmp(
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    data_dir = tmp_path / "data"
    first = _write(root / "first.xmp", b"same-content")
    second = _write(root / "second.xmp", b"same-content")
    plan = _plan(data_dir, root)
    stored_path = plan_path(data_dir, plan["plan_id"])
    stored = read_json(stored_path)
    first_record = next(
        item for item in stored["candidates"] if item["relative_path"] == "first.xmp"
    )
    first_record["path"] = str(second)
    write_json(stored_path, stored)

    with pytest.raises(ValueError, match="范围异常"):
        _execute(data_dir, plan)

    assert first.read_bytes() == b"same-content"
    assert second.read_bytes() == b"same-content"
    assert not _trash_path(root, plan, "first.xmp").exists()
    assert not _manifest_path(data_dir, plan).exists()
