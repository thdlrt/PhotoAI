from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .progress import emit_progress, phase_end, phase_start
from .toolbox import (
    MAX_ACTION_FILES,
    PLAN_ID_RE,
    TRASH_DIRECTORY,
    _inside,
    _is_link_like,
    _iter_files,
    _lexists,
    _validate_root,
    toolbox_operation_lock,
)
from .util import full_fingerprint, read_json, write_json
from .xmp import assert_lightroom_not_running


XMP_CLEANUP_SCHEMA_VERSION = 1
XMP_CLEANUP_KIND = "xmp_cleanup"
PUBLIC_CANDIDATE_LIMIT = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plans_root(data_dir: Path) -> Path:
    return data_dir / "toolbox" / "xmp-cleanup" / "plans"


def transactions_root(data_dir: Path) -> Path:
    return data_dir / "toolbox" / "xmp-cleanup" / "transactions"


def plan_path(data_dir: Path, plan_id: str) -> Path:
    if not PLAN_ID_RE.fullmatch(plan_id):
        raise ValueError("XMP 清理计划编号无效。")
    return plans_root(data_dir) / f"{plan_id}.json"


def transaction_path(root: Path, transaction_id: str) -> Path:
    if not PLAN_ID_RE.fullmatch(transaction_id):
        raise ValueError("XMP 清理记录编号无效。")
    return root / f"{transaction_id}.json"


def _new_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _stable_fingerprint(path: Path) -> dict[str, Any]:
    before = path.stat()
    fingerprint = full_fingerprint(path)
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or int(fingerprint.get("size", -1)) != after.st_size
    ):
        raise ValueError(f"读取时文件发生变化：{path}")
    return {
        "size": int(fingerprint["size"]),
        "mtime_ns": int(after.st_mtime_ns),
        "sha256": str(fingerprint["sha256"]),
    }


def _same_snapshot(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        int(left.get("size", -1)) == int(right.get("size", -2))
        and int(left.get("mtime_ns", -1)) == int(right.get("mtime_ns", -2))
        and str(left.get("sha256", "")) == str(right.get("sha256", "missing"))
    )


def _content_matches(path: Path, snapshot: dict[str, Any]) -> bool:
    try:
        current = _stable_fingerprint(path)
    except (OSError, ValueError, TypeError):
        return False
    return (
        int(current.get("size", -1)) == int(snapshot.get("size", -2))
        and str(current.get("sha256", "")) == str(snapshot.get("sha256", "missing"))
    )


def _scan(root: Path, recursive: bool) -> tuple[list[dict[str, Any]], list[str]]:
    validated_root = _validate_root(root, "XMP 照片目录")
    paths, scan_errors = _iter_files(validated_root, {".xmp"}, recursive)
    if len(paths) > MAX_ACTION_FILES:
        raise ValueError(f"XMP 数量超过 {MAX_ACTION_FILES} 个，请缩小扫描范围。")

    candidates: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for path in paths:
        relative = path.relative_to(validated_root).as_posix()
        folded = relative.casefold()
        previous = seen.get(folded)
        if previous is not None and previous != relative:
            raise ValueError(f"发现仅大小写不同的 XMP 路径，已拒绝操作：{previous} / {relative}")
        seen[folded] = relative
        try:
            snapshot = _stable_fingerprint(path)
        except (OSError, ValueError) as exc:
            scan_errors.append(str(exc))
            continue
        candidates.append(
            {
                "path": str(path),
                "relative_path": relative,
                "size": snapshot["size"],
                "snapshot": snapshot,
            }
        )
    candidates.sort(key=lambda item: str(item["relative_path"]).casefold())
    return candidates, scan_errors


def public_xmp_cleanup_plan(plan: dict[str, Any]) -> dict[str, Any]:
    candidates = []
    for item in list(plan.get("candidates", []))[:PUBLIC_CANDIDATE_LIMIT]:
        candidates.append(
            {
                "path": item.get("path"),
                "relative_path": item.get("relative_path"),
                "size": int(item.get("size", item.get("snapshot", {}).get("size", 0))),
            }
        )
    return {
        "schema_version": plan.get("schema_version"),
        "kind": plan.get("kind"),
        "operation": plan.get("operation") or "delete",
        "plan_id": plan.get("plan_id"),
        "created_at": plan.get("created_at"),
        "status": plan.get("status"),
        "root_path": plan.get("root_path"),
        "recursive": bool(plan.get("recursive")),
        "complete": bool(plan.get("complete")),
        "xmp_count": int(plan.get("xmp_count", 0)),
        "xmp_bytes": int(plan.get("xmp_bytes", 0)),
        "scan_errors": list(plan.get("scan_errors", [])),
        "candidates": candidates,
        "candidates_truncated": len(plan.get("candidates", [])) > PUBLIC_CANDIDATE_LIMIT,
    }


def create_xmp_cleanup_plan(data_dir: Path, *, root: Path, recursive: bool = False) -> dict[str, Any]:
    validated_root = _validate_root(root, "XMP 照片目录")
    candidates, scan_errors = _scan(validated_root, recursive)
    plan_id = _new_id()
    plan = {
        "schema_version": XMP_CLEANUP_SCHEMA_VERSION,
        "kind": XMP_CLEANUP_KIND,
        "operation": "delete",
        "plan_id": plan_id,
        "created_at": _now(),
        "status": "planned",
        "root_path": str(validated_root),
        "recursive": bool(recursive),
        "complete": not scan_errors,
        "xmp_count": len(candidates),
        "xmp_bytes": sum(int(item["size"]) for item in candidates),
        "scan_errors": scan_errors,
        "candidates": candidates,
    }
    destination = plan_path(data_dir, plan_id)
    if _lexists(destination):
        raise ValueError("XMP 清理计划编号冲突，请重试。")
    write_json(destination, plan)
    return public_xmp_cleanup_plan(plan)


def load_xmp_cleanup_plan(path: Path) -> dict[str, Any]:
    if not path.is_file() or not PLAN_ID_RE.fullmatch(path.stem):
        raise ValueError("XMP 清理计划不存在。")
    plan = read_json(path)
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != XMP_CLEANUP_SCHEMA_VERSION
        or plan.get("kind") != XMP_CLEANUP_KIND
        or plan.get("plan_id") != path.stem
    ):
        raise ValueError("XMP 清理计划格式无效。")
    # Plans created before direct deletion was introduced did not carry an
    # operation. They were only previews, so adopt the current behavior
    # without forcing the user to scan the directory again.
    plan.setdefault("operation", "delete")
    return plan


def _trash_target(root: Path, transaction_id: str, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("XMP 相对路径无效。")
    if relative.suffix.casefold() != ".xmp":
        raise ValueError("清理记录包含非 XMP 文件。")
    target = root / TRASH_DIRECTORY / "xmp-cleanup" / transaction_id / relative
    if not _inside(target, root):
        raise ValueError("XMP 回收路径超出照片目录。")
    return target


def _validate_parent_chain(target: Path, root: Path) -> None:
    current = target.parent
    while current != root and current.parent != current:
        if _lexists(current) and _is_link_like(current):
            raise ValueError(f"XMP 回收路径经过链接或目录联接点：{current}")
        current = current.parent


def _validate_source(record: dict[str, Any], *, require_mtime: bool = True) -> tuple[Path, Path]:
    root = _validate_root(Path(str(record["root_path"])), "XMP 照片目录")
    source = Path(str(record["original_path"] or ""))
    target = Path(str(record["trash_path"] or ""))
    relative = str(record["relative_path"] or "")
    if _is_link_like(source):
        raise ValueError(f"XMP 属于链接文件，已拒绝操作：{source}")
    try:
        resolved_source = source.resolve(strict=True)
        expected_source = (root / Path(relative)).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"XMP 不存在或无法读取：{source}") from exc
    if (
        not resolved_source.is_file()
        or resolved_source.suffix.casefold() != ".xmp"
        or not _inside(resolved_source, root)
        or resolved_source != expected_source
    ):
        raise ValueError(f"XMP 文件范围异常：{source}")
    expected = _trash_target(root, str(record["transaction_id"]), relative)
    if target.resolve(strict=False) != expected.resolve(strict=False):
        raise ValueError("XMP 回收路径与事务记录不一致。")
    _validate_parent_chain(target, root)
    current = _stable_fingerprint(resolved_source)
    snapshot = dict(record.get("snapshot") or {})
    if require_mtime:
        matches = _same_snapshot(current, snapshot)
    else:
        matches = (
            int(current.get("size", -1)) == int(snapshot.get("size", -2))
            and current.get("sha256") == snapshot.get("sha256")
        )
    if not matches:
        raise ValueError(f"XMP 已变化，请重新扫描：{source}")
    if _lexists(target):
        raise ValueError(f"XMP 回收区已存在同名文件：{target}")
    return resolved_source, target


def _validate_delete_source(record: dict[str, Any], *, require_mtime: bool = True) -> Path:
    root = _validate_root(Path(str(record["root_path"])), "XMP 照片目录")
    source = Path(str(record.get("original_path") or ""))
    relative = Path(str(record.get("relative_path") or ""))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("XMP 相对路径无效。")
    if relative.suffix.casefold() != ".xmp":
        raise ValueError("清理记录包含非 XMP 文件。")
    if _is_link_like(source):
        raise ValueError(f"XMP 属于链接文件，已拒绝操作：{source}")
    try:
        resolved_source = source.resolve(strict=True)
        expected_source = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"XMP 不存在或无法读取：{source}") from exc
    if (
        not resolved_source.is_file()
        or resolved_source.suffix.casefold() != ".xmp"
        or not _inside(resolved_source, root)
        or resolved_source != expected_source
    ):
        raise ValueError(f"XMP 文件范围异常：{source}")
    current = _stable_fingerprint(resolved_source)
    snapshot = dict(record.get("snapshot") or {})
    matches = _same_snapshot(current, snapshot) if require_mtime else (
        int(current.get("size", -1)) == int(snapshot.get("size", -2))
        and current.get("sha256") == snapshot.get("sha256")
    )
    if not matches:
        raise ValueError(f"XMP 已变化，请重新扫描：{source}")
    return resolved_source


def _validate_plan_is_current(plan: dict[str, Any]) -> None:
    root = _validate_root(Path(str(plan.get("root_path") or "")), "XMP 照片目录")
    current, scan_errors = _scan(root, bool(plan.get("recursive")))
    if scan_errors:
        raise ValueError("目录无法完整读取，请排除读取错误后重新扫描。")
    expected = {
        str(item["relative_path"]).casefold(): item
        for item in list(plan.get("candidates", []))
    }
    actual = {str(item["relative_path"]).casefold(): item for item in current}
    if set(expected) != set(actual):
        raise ValueError("XMP 文件集合已变化，请重新扫描。")
    for key, expected_item in expected.items():
        if not _same_snapshot(
            dict(expected_item.get("snapshot") or {}),
            dict(actual[key].get("snapshot") or {}),
        ):
            raise ValueError(f"XMP 已变化，请重新扫描：{expected_item.get('path')}")


def _journal_path(manifest_file: Path) -> Path:
    return manifest_file.with_suffix(".journal.jsonl")


def _append_event(handle: Any, index: int, record: dict[str, Any]) -> None:
    payload = {
        "index": index,
        "at": _now(),
        "status": record.get("status"),
        "error": record.get("error"),
        "rollback_error": record.get("rollback_error"),
        "restored_at": record.get("restored_at"),
        "deleted_at": record.get("deleted_at"),
    }
    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _load_manifest(manifest_file: Path) -> dict[str, Any]:
    if not manifest_file.is_file() or not PLAN_ID_RE.fullmatch(manifest_file.stem):
        raise ValueError("XMP 清理记录不存在。")
    manifest = read_json(manifest_file)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != XMP_CLEANUP_SCHEMA_VERSION
        or manifest.get("kind") != XMP_CLEANUP_KIND
        or manifest.get("transaction_id") != manifest_file.stem
    ):
        raise ValueError("XMP 清理记录格式无效。")
    records = list(manifest.get("records", []))
    journal = _journal_path(manifest_file)
    if journal.is_file():
        try:
            lines = journal.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
                index = int(event["index"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if not 0 <= index < len(records):
                continue
            for key in ("status", "error", "rollback_error", "restored_at", "deleted_at"):
                if key not in event:
                    continue
                if event[key] is None:
                    records[index].pop(key, None)
                else:
                    records[index][key] = event[key]
    manifest["records"] = records
    return manifest


def _compact_manifest(manifest_file: Path, manifest: dict[str, Any]) -> None:
    write_json(manifest_file, manifest)
    try:
        _journal_path(manifest_file).unlink(missing_ok=True)
    except OSError:
        pass


def _record_state(record: dict[str, Any]) -> str:
    source = Path(str(record.get("original_path") or ""))
    trash = Path(str(record.get("trash_path") or ""))
    source_exists = _lexists(source)
    trash_exists = _lexists(trash)
    if source_exists and trash_exists:
        return "conflict"
    if not source_exists and not trash_exists:
        return "lost"
    present = source if source_exists else trash
    if _is_link_like(present) or not present.is_file() or not _content_matches(present, dict(record.get("snapshot") or {})):
        return "modified"
    return "at_source" if source_exists else "in_trash"


def _reconcile(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in ("at_source", "in_trash", "conflict", "modified", "lost")}
    for record in records:
        state = _record_state(record)
        record["location_state"] = state
        counts[state] += 1
    return counts


def _delete_record_state(record: dict[str, Any]) -> str:
    source = Path(str(record.get("original_path") or ""))
    if not _lexists(source):
        return "deleted" if record.get("status") == "deleted" else "lost"
    if (
        _is_link_like(source)
        or not source.is_file()
        or source.suffix.casefold() != ".xmp"
        or not _content_matches(source, dict(record.get("snapshot") or {}))
    ):
        return "modified"
    return "at_source"


def _reconcile_delete(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in ("deleted", "at_source", "modified", "lost")}
    for record in records:
        state = _delete_record_state(record)
        record["location_state"] = state
        counts[state] += 1
    return counts


def transaction_summary(manifest: dict[str, Any], manifest_file: Path, *, reconcile: bool = False) -> dict[str, Any]:
    records = list(manifest.get("records", []))
    operation = str(manifest.get("operation") or "recycle")
    if operation == "delete":
        locations = _reconcile_delete(records) if reconcile else {
            "deleted": int(manifest.get("deleted_count", manifest.get("removed_count", 0))),
            "at_source": int(manifest.get("remaining_count", 0)),
            "modified": int(manifest.get("modified_count", 0)),
            "lost": int(manifest.get("lost_count", 0)),
        }
        failed = int(manifest.get("failed_count", sum(bool(item.get("error")) for item in records)))
        status = str(manifest.get("status") or "unknown")
        if reconcile and status == "running":
            status = "interrupted_with_issues" if locations["modified"] + locations["lost"] else "interrupted"
        return {
            "transaction_id": str(manifest.get("transaction_id") or manifest_file.stem),
            "operation": "delete",
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "status": status,
            "root_path": manifest.get("root_path"),
            "recursive": bool(manifest.get("recursive")),
            "xmp_count": len(records),
            "removed_count": locations["deleted"],
            "deleted_count": locations["deleted"],
            "moved_count": 0,
            "remaining_count": locations["at_source"],
            "restored_count": 0,
            "failed_count": failed,
            "conflict_count": 0,
            "modified_count": locations["modified"],
            "lost_count": locations["lost"],
            "needs_attention": bool(locations["modified"] + locations["lost"]),
            "rollbackable": False,
            "manifest_path": str(manifest_file),
        }
    locations = _reconcile(records) if reconcile else {
        "at_source": int(manifest.get("at_source_count", 0)),
        "in_trash": int(manifest.get("remaining_count", 0)),
        "conflict": int(manifest.get("conflict_count", 0)),
        "modified": int(manifest.get("modified_count", 0)),
        "lost": int(manifest.get("lost_count", 0)),
    }
    restored = int(manifest.get("restored_count", sum(item.get("status") == "restored" for item in records)))
    failed = int(manifest.get("failed_count", sum(bool(item.get("error")) for item in records)))
    removed = int(manifest.get("removed_count", sum(item.get("status") in {"moved", "restored"} for item in records)))
    status = str(manifest.get("status") or "unknown")
    if reconcile and status in {"running", "rolling_back"}:
        status = "interrupted_with_issues" if locations["conflict"] + locations["modified"] + locations["lost"] else "interrupted"
    return {
        "transaction_id": str(manifest.get("transaction_id") or manifest_file.stem),
        "operation": "recycle",
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "status": status,
        "root_path": manifest.get("root_path"),
        "recursive": bool(manifest.get("recursive")),
        "xmp_count": len(records),
        "removed_count": removed,
        "moved_count": locations["in_trash"],
        "remaining_count": locations["in_trash"],
        "restored_count": restored,
        "failed_count": failed,
        "conflict_count": locations["conflict"],
        "modified_count": locations["modified"],
        "lost_count": locations["lost"],
        "needs_attention": bool(locations["conflict"] + locations["modified"] + locations["lost"]),
        "rollbackable": bool(locations["in_trash"] + locations["conflict"]),
        "manifest_path": str(manifest_file),
    }


def _update_counts(manifest: dict[str, Any], locations: dict[str, int]) -> None:
    manifest["at_source_count"] = locations["at_source"]
    manifest["remaining_count"] = locations["in_trash"]
    manifest["conflict_count"] = locations["conflict"]
    manifest["modified_count"] = locations["modified"]
    manifest["lost_count"] = locations["lost"]


def _lightroom_guard(action: str) -> None:
    try:
        assert_lightroom_not_running()
    except RuntimeError as exc:
        raise RuntimeError(f"{exc} 已拒绝{action}文件夹 XMP。") from exc


def execute_xmp_cleanup_plan(plan_file: Path, manifest_root: Path) -> dict[str, Any]:
    manifest_root.mkdir(parents=True, exist_ok=True)
    with toolbox_operation_lock(manifest_root):
        return _execute_locked(plan_file, manifest_root)


def _execute_locked(plan_file: Path, manifest_root: Path) -> dict[str, Any]:
    plan = load_xmp_cleanup_plan(plan_file)
    if plan.get("operation") != "delete":
        raise ValueError("这个 XMP 清理计划不是永久删除计划，请重新扫描。")
    if plan.get("status") != "planned":
        raise ValueError("这个 XMP 清理计划已经执行或正在执行。")
    if not plan.get("complete", False):
        raise ValueError("扫描结果不完整，请排除读取错误后重新扫描。")
    candidates = list(plan.get("candidates", []))
    if not candidates:
        raise ValueError("这个计划没有需要清理的 XMP。")

    phase_start("verify", "复核 XMP", len(candidates), unit="个文件")
    _validate_plan_is_current(plan)
    root = _validate_root(Path(str(plan["root_path"])), "XMP 照片目录")
    records: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        record = {
            "transaction_id": plan["plan_id"],
            "operation": "delete",
            "root_path": str(root),
            "original_path": candidate["path"],
            "relative_path": candidate["relative_path"],
            "snapshot": candidate["snapshot"],
            "status": "planned",
        }
        _validate_delete_source(record)
        records.append(record)
        emit_progress("verify", "复核 XMP", index, len(candidates), unit="个文件")
    phase_end("verify", "复核 XMP", len(candidates), unit="个文件")

    manifest_file = transaction_path(manifest_root, plan["plan_id"])
    if _lexists(manifest_file):
        raise ValueError("XMP 清理记录已存在，已拒绝重复执行。")
    manifest = {
        "schema_version": XMP_CLEANUP_SCHEMA_VERSION,
        "kind": XMP_CLEANUP_KIND,
        "operation": "delete",
        "transaction_id": plan["plan_id"],
        "plan_id": plan["plan_id"],
        "created_at": _now(),
        "updated_at": _now(),
        "status": "running",
        "root_path": str(root),
        "recursive": bool(plan.get("recursive")),
        "records": records,
    }
    write_json(manifest_file, manifest)
    plan["status"] = "running"
    plan["transaction_id"] = plan["plan_id"]
    write_json(plan_file, plan)

    phase_start("delete", "永久删除 XMP", len(records), unit="个文件")
    with _journal_path(manifest_file).open("a", encoding="utf-8") as journal:
        for index, record in enumerate(records):
            try:
                source = _validate_delete_source(record)
                source.unlink()
                if _lexists(source):
                    raise OSError(f"XMP 删除后仍然存在：{source}")
                record["status"] = "deleted"
                record["deleted_at"] = _now()
                record.pop("error", None)
            except (OSError, ValueError) as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
            _append_event(journal, index, record)
            emit_progress("delete", "永久删除 XMP", index + 1, len(records), unit="个文件")
    phase_end("delete", "永久删除 XMP", len(records), unit="个文件")

    phase_start("finalize", "保存记录", 1, unit="项")
    locations = _reconcile_delete(records)
    failed = sum(bool(item.get("error")) for item in records)
    manifest["status"] = "completed_with_errors" if failed or locations["at_source"] + locations["modified"] + locations["lost"] else "completed"
    manifest["updated_at"] = _now()
    manifest["removed_count"] = locations["deleted"]
    manifest["deleted_count"] = locations["deleted"]
    manifest["remaining_count"] = locations["at_source"]
    manifest["restored_count"] = 0
    manifest["failed_count"] = failed
    manifest["modified_count"] = locations["modified"]
    manifest["lost_count"] = locations["lost"]
    _compact_manifest(manifest_file, manifest)
    plan["status"] = manifest["status"]
    plan["finished_at"] = _now()
    write_json(plan_file, plan)
    phase_end("finalize", "保存记录", 1, unit="项")
    return transaction_summary(manifest, manifest_file)


def rollback_xmp_cleanup_manifest(manifest_file: Path) -> dict[str, Any]:
    with toolbox_operation_lock(manifest_file.parent):
        return _rollback_locked(manifest_file)


def _rollback_locked(manifest_file: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_file)
    if str(manifest.get("operation") or "recycle") == "delete":
        raise ValueError("永久删除记录无法恢复。")
    _lightroom_guard("恢复")
    records = list(manifest.get("records", []))
    manifest["status"] = "rolling_back"
    manifest["updated_at"] = _now()
    _compact_manifest(manifest_file, manifest)

    locations = _reconcile(records)
    errors = {
        "conflict": "原位置与回收区同时存在 XMP，未覆盖",
        "modified": "原位置或回收区 XMP 已被修改",
        "lost": "原位置与回收区均未找到 XMP",
    }
    pending: list[tuple[int, dict[str, Any], Path, Path]] = []
    phase_start("verify", "复核回收文件", locations["in_trash"], unit="个文件")
    progress = 0
    for index, record in enumerate(records):
        state = str(record.get("location_state"))
        if state in errors:
            record["rollback_error"] = errors[state]
        if state != "in_trash":
            continue
        progress += 1
        root = _validate_root(Path(str(record.get("root_path") or "")), "XMP 照片目录")
        original = Path(str(record.get("original_path") or ""))
        trash = Path(str(record.get("trash_path") or ""))
        try:
            expected = _trash_target(root, str(manifest["transaction_id"]), str(record.get("relative_path") or ""))
            expected_original = root / Path(str(record.get("relative_path") or ""))
            if (
                trash.resolve(strict=False) != expected.resolve(strict=False)
                or original.resolve(strict=False) != expected_original.resolve(strict=False)
                or not _inside(original, root)
            ):
                raise ValueError("XMP 恢复路径超出原操作范围。")
            _validate_parent_chain(trash, root)
            if _record_state(record) != "in_trash":
                raise ValueError("XMP 文件状态已经变化，请重新检查。")
            record.pop("rollback_error", None)
            pending.append((index, record, original, trash))
        except (OSError, ValueError) as exc:
            record["rollback_error"] = str(exc)
        emit_progress("verify", "复核回收文件", progress, locations["in_trash"], unit="个文件")
    _lightroom_guard("恢复")
    phase_end("verify", "复核回收文件", locations["in_trash"], unit="个文件")

    phase_start("restore", "恢复 XMP", len(pending), unit="个文件")
    with _journal_path(manifest_file).open("a", encoding="utf-8") as journal:
        for position, (index, record, original, trash) in enumerate(pending, start=1):
            try:
                if position > 1 and position % 100 == 0:
                    _lightroom_guard("恢复")
                if _lexists(original) or _record_state(record) != "in_trash":
                    raise OSError("原位置已有 XMP 或回收文件已变化，未覆盖。")
                original.parent.mkdir(parents=True, exist_ok=True)
                trash.rename(original)
                if not _content_matches(original, dict(record.get("snapshot") or {})):
                    raise OSError("恢复后的 XMP 指纹不一致。")
                record["status"] = "restored"
                record["restored_at"] = _now()
                record.pop("error", None)
                record.pop("rollback_error", None)
            except (OSError, RuntimeError) as exc:
                record["rollback_error"] = str(exc)
            _append_event(journal, index, record)
            emit_progress("restore", "恢复 XMP", position, len(pending), unit="个文件")
    phase_end("restore", "恢复 XMP", len(pending), unit="个文件")

    phase_start("finalize", "更新记录", 1, unit="项")
    locations = _reconcile(records)
    restored = sum(item.get("status") == "restored" for item in records)
    unresolved = locations["in_trash"] + locations["conflict"] + locations["modified"] + locations["lost"]
    manifest["status"] = "rolled_back" if unresolved == 0 else "partially_rolled_back"
    manifest["updated_at"] = _now()
    manifest["restored_count"] = restored
    manifest["failed_count"] = sum(bool(item.get("error")) for item in records)
    _update_counts(manifest, locations)
    _compact_manifest(manifest_file, manifest)
    phase_end("finalize", "更新记录", 1, unit="项")
    return transaction_summary(manifest, manifest_file)


def load_xmp_cleanup_transaction(data_dir: Path, transaction_id: str, *, reconcile: bool = True) -> tuple[dict[str, Any], Path]:
    manifest_file = transaction_path(transactions_root(data_dir), transaction_id)
    manifest = _load_manifest(manifest_file)
    return transaction_summary(manifest, manifest_file, reconcile=reconcile), manifest_file


def list_xmp_cleanup_transactions(data_dir: Path, limit: int = 20) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    root = transactions_root(data_dir)
    if not root.is_dir():
        return [], {}
    candidates: list[tuple[int, Path]] = []
    for path in root.glob("*.json"):
        if not PLAN_ID_RE.fullmatch(path.stem):
            continue
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    paths = [path for _mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True)[: max(0, limit)]]
    items: list[dict[str, Any]] = []
    mapping: dict[str, Path] = {}
    for path in paths:
        try:
            manifest = _load_manifest(path)
            summary = transaction_summary(
                manifest,
                path,
                reconcile=manifest.get("status") in {"running", "rolling_back"},
            )
        except (OSError, ValueError, TypeError):
            continue
        items.append(summary)
        mapping[path.stem] = path
    return items, mapping
