from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from .constants import READABLE_RAW_EXTENSIONS
from .progress import emit_progress, phase_end, phase_start
from .util import quick_fingerprint, read_json, write_json


RAW_JPEG_SCHEMA_VERSION = 1
DEFAULT_RAW_EXTENSIONS = tuple(sorted(READABLE_RAW_EXTENSIONS | {".raw", ".3fr", ".mef", ".mrw", ".srw", ".x3f"}))
DEFAULT_JPEG_EXTENSIONS = (".jpg", ".jpeg", ".heic", ".heif", ".hif")
TRASH_DIRECTORY = ".photo-ai-trash"
PLAN_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
EXTENSION_RE = re.compile(r"^\.?[A-Za-z0-9]{2,8}$")
MAX_SCANNED_FILES = 100_000
MAX_ACTION_FILES = 50_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _normalize_extensions(values: Iterable[str], label: str) -> set[str]:
    normalized: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        if not EXTENSION_RE.fullmatch(value):
            raise ValueError(f"{label}包含无效扩展名：{value or '空值'}")
        normalized.add(f".{value.lstrip('.').casefold()}")
    if not normalized:
        raise ValueError(f"{label}不能为空。")
    return normalized


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", lambda _value: False)
    try:
        return path.is_symlink() or bool(is_junction(path))
    except OSError:
        return True


def _lexists(path: Path) -> bool:
    try:
        return os.path.lexists(path)
    except OSError:
        return True


def _validate_root(path: Path, label: str) -> Path:
    if _is_link_like(path):
        raise ValueError(f"{label}属于链接目录，已拒绝扫描。")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label}不存在或无法访问：{path}") from exc
    if not resolved.is_dir():
        raise ValueError(f"{label}不是文件夹：{resolved}")
    if resolved.parent == resolved:
        raise ValueError(f"{label}范围过大或属于链接目录，已拒绝扫描。")
    if any(part.casefold() == TRASH_DIRECTORY.casefold() for part in resolved.parts):
        raise ValueError(f"{label}不能位于 {TRASH_DIRECTORY} 回收区内。")
    return resolved


def _iter_files(root: Path, extensions: set[str], recursive: bool) -> tuple[list[Path], list[str]]:
    found: list[Path] = []
    warnings: list[str] = []

    def on_error(error: OSError) -> None:
        warnings.append(f"无法读取：{error.filename or root}")

    if recursive:
        iterator = os.walk(root, topdown=True, onerror=on_error, followlinks=False)
        for current, directories, filenames in iterator:
            current_path = Path(current)
            kept: list[str] = []
            for name in directories:
                child = current_path / name
                if name.casefold() == TRASH_DIRECTORY.casefold() or _is_link_like(child):
                    continue
                kept.append(name)
            directories[:] = kept
            for name in filenames:
                path = current_path / name
                if path.suffix.casefold() not in extensions or path.is_symlink():
                    continue
                try:
                    if path.is_file():
                        found.append(path.resolve(strict=True))
                except OSError:
                    warnings.append(f"无法读取：{path}")
                if len(found) > MAX_SCANNED_FILES:
                    raise ValueError(f"单侧照片超过 {MAX_SCANNED_FILES} 个，请缩小扫描范围。")
    else:
        try:
            entries = list(os.scandir(root))
        except OSError as exc:
            raise ValueError(f"无法读取文件夹：{root}") from exc
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                warnings.append(f"无法读取：{entry.path}")
                continue
            path = Path(entry.path)
            if path.suffix.casefold() in extensions:
                found.append(path.resolve(strict=True))
            if len(found) > MAX_SCANNED_FILES:
                raise ValueError(f"单侧照片超过 {MAX_SCANNED_FILES} 个，请缩小扫描范围。")
    found.sort(key=lambda item: str(item).casefold())
    return found, warnings


def _pair_key(path: Path, root: Path) -> str:
    relative = path.relative_to(root)
    return relative.with_suffix("").as_posix().casefold()


def _sidecar_pair_key(path: Path, root: Path, media_extensions: set[str]) -> str:
    relative = path.relative_to(root).with_suffix("")
    if relative.suffix.casefold() in media_extensions:
        relative = relative.with_suffix("")
    return relative.as_posix().casefold()


def _file_snapshot(path: Path) -> dict[str, Any]:
    return quick_fingerprint(path)


def _content_matches(path: Path, snapshot: dict[str, Any]) -> bool:
    try:
        current = quick_fingerprint(path)
    except OSError:
        return False
    return (
        int(current.get("size", -1)) == int(snapshot.get("size", -2))
        and current.get("quick_sha256") == snapshot.get("quick_sha256")
    )


def _scan(
    *,
    layout: Literal["mixed", "separate"],
    mixed_root: Path | None,
    raw_root: Path | None,
    jpeg_root: Path | None,
    direction: Literal["jpeg", "raw", "both"],
    raw_extensions: set[str],
    jpeg_extensions: set[str],
    recursive: bool,
) -> dict[str, Any]:
    if raw_extensions & jpeg_extensions:
        raise ValueError("RAW 与成片扩展名不能重复。")
    if layout == "mixed":
        if mixed_root is None:
            raise ValueError("请选择混合照片目录。")
        roots = {"mixed": _validate_root(mixed_root, "混合照片目录")}
        raw_scan_root = jpeg_scan_root = roots["mixed"]
    else:
        if raw_root is None or jpeg_root is None:
            raise ValueError("请选择 RAW 与成片文件夹。")
        roots = {
            "raw": _validate_root(raw_root, "RAW 文件夹"),
            "jpeg": _validate_root(jpeg_root, "成片文件夹"),
        }
        if roots["raw"] == roots["jpeg"] or _inside(roots["raw"], roots["jpeg"]) or _inside(roots["jpeg"], roots["raw"]):
            raise ValueError("分离模式的 RAW 与成片文件夹不能相同或互相嵌套。")
        raw_scan_root = roots["raw"]
        jpeg_scan_root = roots["jpeg"]

    raw_files, raw_warnings = _iter_files(raw_scan_root, raw_extensions, recursive)
    jpeg_files, jpeg_warnings = _iter_files(jpeg_scan_root, jpeg_extensions, recursive)
    media_extensions = raw_extensions | jpeg_extensions
    sidecars_by_key: dict[str, list[dict[str, Any]]] = {}
    sidecar_warnings: list[str] = []
    for root in {raw_scan_root, jpeg_scan_root}:
        sidecars, warnings_for_root = _iter_files(root, {".xmp"}, recursive)
        for path in sidecars:
            key = _sidecar_pair_key(path, root, media_extensions)
            sidecars_by_key.setdefault(key, []).append({
                "path": str(path),
                "relative_path": path.relative_to(root).as_posix(),
                "root": str(root),
                "snapshot": _file_snapshot(path),
            })
        sidecar_warnings.extend(warnings_for_root)
    total_files = len(raw_files) + len(jpeg_files)
    if total_files > MAX_SCANNED_FILES:
        raise ValueError(f"照片总数超过 {MAX_SCANNED_FILES} 个，请缩小扫描范围。")

    raw_by_key: dict[str, list[Path]] = {}
    jpeg_by_key: dict[str, list[Path]] = {}
    for path in raw_files:
        raw_by_key.setdefault(_pair_key(path, raw_scan_root), []).append(path)
    for path in jpeg_files:
        jpeg_by_key.setdefault(_pair_key(path, jpeg_scan_root), []).append(path)

    candidates: list[dict[str, Any]] = []
    scan_errors = [*raw_warnings, *jpeg_warnings, *sidecar_warnings]
    warnings: list[str] = []
    paired_keys = 0
    raw_only_files = 0
    jpeg_only_files = 0
    ambiguous_keys = 0
    for key in sorted(set(raw_by_key) | set(jpeg_by_key)):
        raws = raw_by_key.get(key, [])
        jpegs = jpeg_by_key.get(key, [])
        if raws and jpegs:
            paired_keys += 1
            if len(raws) > 1 or len(jpegs) > 1:
                warnings.append(f"同名多文件已保留：{key}")
            continue
        side = "raw" if raws else "jpeg"
        files = raws or jpegs
        if side == "raw":
            raw_only_files += len(files)
        else:
            jpeg_only_files += len(files)
        should_move = direction == "both" or direction != side
        if not should_move:
            continue
        if len(files) != 1:
            ambiguous_keys += 1
            warnings.append(f"孤片存在同名多格式，未加入操作：{key}")
            continue
        path = files[0]
        side_root = raw_scan_root if side == "raw" else jpeg_scan_root
        related_sidecars = sorted(
            sidecars_by_key.get(key, []),
            key=lambda item: str(item["path"]).casefold(),
        )
        candidates.append({
            "path": str(path),
            "side": side,
            "relative_path": path.relative_to(side_root).as_posix(),
            "root": str(side_root),
            "snapshot": _file_snapshot(path),
            "sidecars": related_sidecars,
        })

    operation_count = sum(1 + len(item["sidecars"]) for item in candidates)
    if operation_count > MAX_ACTION_FILES:
        raise ValueError(f"待处理孤片超过 {MAX_ACTION_FILES} 个，请缩小扫描范围。")
    candidates.sort(key=lambda item: str(item["path"]).casefold())
    return {
        "layout": layout,
        "direction": direction,
        "recursive": recursive,
        "roots": {key: str(value) for key, value in roots.items()},
        "raw_extensions": sorted(raw_extensions),
        "jpeg_extensions": sorted(jpeg_extensions),
        "raw_count": len(raw_files),
        "jpeg_count": len(jpeg_files),
        "paired_count": paired_keys,
        "raw_only_count": raw_only_files,
        "jpeg_only_count": jpeg_only_files,
        "ambiguous_count": ambiguous_keys,
        "sidecar_count": sum(len(item["sidecars"]) for item in candidates),
        "operation_count": operation_count,
        "candidate_bytes": sum(
            int(item["snapshot"]["size"])
            + sum(int(sidecar["snapshot"]["size"]) for sidecar in item["sidecars"])
            for item in candidates
        ),
        "candidates": candidates,
        "warnings": warnings,
        "scan_errors": scan_errors,
        "complete": not scan_errors,
    }


def _plans_root(data_dir: Path) -> Path:
    return data_dir / "toolbox" / "raw-jpeg" / "plans"


def transactions_root(data_dir: Path) -> Path:
    return data_dir / "toolbox" / "raw-jpeg" / "transactions"


def plan_path(data_dir: Path, plan_id: str) -> Path:
    if not PLAN_ID_RE.fullmatch(plan_id):
        raise ValueError("操作计划编号无效。")
    return _plans_root(data_dir) / f"{plan_id}.json"


def transaction_path(root: Path, transaction_id: str) -> Path:
    if not PLAN_ID_RE.fullmatch(transaction_id):
        raise ValueError("操作记录编号无效。")
    return root / f"{transaction_id}.json"


def create_raw_jpeg_plan(
    data_dir: Path,
    *,
    layout: Literal["mixed", "separate"],
    direction: Literal["jpeg", "raw", "both"],
    mixed_root: Path | None,
    raw_root: Path | None,
    jpeg_root: Path | None,
    raw_extensions: Iterable[str],
    jpeg_extensions: Iterable[str],
    recursive: bool,
) -> dict[str, Any]:
    raw_set = _normalize_extensions(raw_extensions, "RAW 格式")
    jpeg_set = _normalize_extensions(jpeg_extensions, "成片格式")
    scanned = _scan(
        layout=layout,
        mixed_root=mixed_root,
        raw_root=raw_root,
        jpeg_root=jpeg_root,
        direction=direction,
        raw_extensions=raw_set,
        jpeg_extensions=jpeg_set,
        recursive=recursive,
    )
    plan_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    payload = {
        "schema_version": RAW_JPEG_SCHEMA_VERSION,
        "plan_id": plan_id,
        "created_at": _now(),
        "status": "planned",
        **scanned,
    }
    root = _plans_root(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / f"{plan_id}.json", payload)
    return public_plan(payload)


def public_plan(plan: dict[str, Any], preview_limit: int = 500) -> dict[str, Any]:
    candidates = list(plan.get("candidates", []))

    def public_candidate(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: (
                [{nested_key: nested_value for nested_key, nested_value in sidecar.items() if nested_key != "snapshot"} for sidecar in value]
                if key == "sidecars"
                else value
            )
            for key, value in item.items()
            if key != "snapshot"
        }

    return {
        key: value
        for key, value in plan.items()
        if key != "candidates"
    } | {
        "candidate_count": len(candidates),
        "candidates": [public_candidate(item) for item in candidates[:preview_limit]],
        "candidates_truncated": len(candidates) > preview_limit,
    }


def load_plan(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("操作计划不存在，请重新扫描。")
    payload = read_json(path)
    if payload.get("schema_version") != RAW_JPEG_SCHEMA_VERSION or payload.get("plan_id") != path.stem:
        raise ValueError("操作计划格式无效，请重新扫描。")
    return payload


def _rescan_plan(plan: dict[str, Any]) -> dict[str, Any]:
    roots = plan.get("roots", {})
    return _scan(
        layout=plan["layout"],
        mixed_root=Path(roots["mixed"]) if roots.get("mixed") else None,
        raw_root=Path(roots["raw"]) if roots.get("raw") else None,
        jpeg_root=Path(roots["jpeg"]) if roots.get("jpeg") else None,
        direction=plan["direction"],
        raw_extensions=set(plan["raw_extensions"]),
        jpeg_extensions=set(plan["jpeg_extensions"]),
        recursive=bool(plan.get("recursive")),
    )


def _validate_plan_is_current(plan: dict[str, Any]) -> None:
    if not plan.get("complete", True):
        raise ValueError("上次扫描不完整，请处理无法读取的目录后重新扫描。")
    current = _rescan_plan(plan)
    if not current.get("complete", True):
        raise ValueError("当前扫描不完整，未执行任何文件操作。")
    planned_candidates = {str(item["path"]).casefold(): item for item in plan.get("candidates", [])}
    current_candidates = {str(item["path"]).casefold(): item for item in current.get("candidates", [])}
    if set(planned_candidates) != set(current_candidates):
        raise ValueError("目录内容已变化，请重新扫描后再执行。")
    for key, planned in planned_candidates.items():
        current_item = current_candidates[key]
        if any(planned.get(field) != current_item.get(field) for field in ("root", "relative_path", "side", "snapshot")):
            raise ValueError(f"文件已变化，请重新扫描：{planned['path']}")
        planned_sidecars = {str(item["path"]).casefold(): item for item in planned.get("sidecars", [])}
        current_sidecars = {str(item["path"]).casefold(): item for item in current_item.get("sidecars", [])}
        if set(planned_sidecars) != set(current_sidecars):
            raise ValueError(f"对应 XMP 已变化，请重新扫描：{planned['path']}")
        for sidecar_key, planned_sidecar in planned_sidecars.items():
            current_sidecar = current_sidecars[sidecar_key]
            if any(planned_sidecar.get(field) != current_sidecar.get(field) for field in ("root", "relative_path", "snapshot")):
                raise ValueError(f"对应 XMP 已变化，请重新扫描：{planned_sidecar['path']}")


def _trash_target(item: dict[str, Any], transaction_id: str) -> Path:
    root = Path(item["root"]).resolve()
    relative = Path(item["relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("计划中包含异常相对路径。")
    side = str(item.get("side", ""))
    if side not in {"raw", "jpeg", "xmp"}:
        raise ValueError("计划中包含异常文件类型。")
    target = root / TRASH_DIRECTORY / "raw-jpeg" / transaction_id / side / relative
    if not _inside(target, root):
        raise ValueError("回收路径超出照片目录。")
    return target


def _candidate_operations(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for candidate_index, candidate in enumerate(candidates):
        operations.append({
            "path": candidate["path"],
            "root": candidate["root"],
            "relative_path": candidate["relative_path"],
            "snapshot": candidate["snapshot"],
            "side": candidate["side"],
            "role": "photo",
            "candidate_index": candidate_index,
        })
        for sidecar in candidate.get("sidecars", []):
            operations.append({
                "path": sidecar["path"],
                "root": sidecar["root"],
                "relative_path": sidecar["relative_path"],
                "snapshot": sidecar["snapshot"],
                "side": "xmp",
                "role": "xmp",
                "candidate_index": candidate_index,
            })
    return operations


@contextmanager
def _operation_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".operation.lock"
    handle = lock_path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise ValueError("工具箱文件操作锁已占用，已有整理或恢复任务正在运行。") from exc
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def toolbox_lock_root(transaction_root: Path) -> Path:
    """Return the shared lock directory for toolbox file mutations.

    Toolbox features keep their manifests in separate subdirectories, but they
    can still target the same photo or sidecar.  Resolve every standard
    ``.../toolbox/<feature>/transactions`` directory to the common toolbox root
    so direct CLI invocations cannot bypass the Web job manager's serialization.
    Non-standard callers retain the previous per-directory locking behavior.
    """

    resolved = transaction_root.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name.casefold() == "toolbox":
            return candidate
    return resolved


@contextmanager
def toolbox_operation_lock(transaction_root: Path):
    with _operation_lock(toolbox_lock_root(transaction_root)):
        yield


def _journal_path(manifest_file: Path) -> Path:
    return manifest_file.with_suffix(".journal.jsonl")


def _append_record_event(handle: Any, index: int, record: dict[str, Any]) -> None:
    payload = {
        "index": index,
        "at": _now(),
        "status": record.get("status"),
        "error": record.get("error"),
        "rollback_error": record.get("rollback_error"),
        "restored_at": record.get("restored_at"),
    }
    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    handle.flush()


def _load_manifest(manifest_file: Path) -> dict[str, Any]:
    if not manifest_file.is_file():
        raise ValueError("操作记录不存在。")
    manifest = read_json(manifest_file)
    if manifest.get("schema_version") != RAW_JPEG_SCHEMA_VERSION or manifest.get("transaction_id") != manifest_file.stem:
        raise ValueError("操作记录格式无效。")
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
            for key in ("status", "error", "rollback_error", "restored_at"):
                if key in event:
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
    source = Path(str(record.get("original_path", "")))
    trash = Path(str(record.get("trash_path", "")))
    source_exists = _lexists(source)
    trash_exists = _lexists(trash)
    if source_exists and trash_exists:
        return "conflict"
    if not source_exists and not trash_exists:
        return "lost"
    present = source if source_exists else trash
    if _is_link_like(present) or not present.is_file() or not _content_matches(present, record.get("snapshot", {})):
        return "modified"
    return "at_source" if source_exists else "in_trash"


def _reconcile_records(records: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in ("at_source", "in_trash", "conflict", "modified", "lost")}
    for record in records:
        state = _record_state(record)
        record["location_state"] = state
        counts[state] += 1
    return counts


def _summary_counts(manifest: dict[str, Any], reconcile: bool) -> dict[str, int]:
    records = list(manifest.get("records", []))
    if reconcile:
        locations = _reconcile_records(records)
    else:
        remaining = manifest.get("remaining_count")
        if remaining is None:
            remaining = sum(record.get("status") in {"moved", "moving"} for record in records)
        locations = {
            "at_source": max(0, len(records) - int(remaining)),
            "in_trash": int(remaining),
            "conflict": int(manifest.get("conflict_count", 0)),
            "modified": int(manifest.get("modified_count", 0)),
            "lost": int(manifest.get("lost_count", 0)),
        }
    return {
        **locations,
        "restored": int(manifest.get("restored_count", sum(record.get("status") == "restored" for record in records))),
        "failed": int(manifest.get("failed_count", sum(record.get("status") == "failed" for record in records))),
    }


def transaction_summary(manifest: dict[str, Any], manifest_file: Path, *, reconcile: bool = False) -> dict[str, Any]:
    records = list(manifest.get("records", []))
    counts = _summary_counts(manifest, reconcile)
    status = manifest.get("status")
    if reconcile and status in {"running", "rolling_back"}:
        status = "interrupted_with_issues" if counts["conflict"] + counts["modified"] + counts["lost"] else "interrupted"
    return {
        "transaction_id": manifest.get("transaction_id", manifest_file.stem),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "status": status,
        "direction": manifest.get("direction"),
        "photo_count": sum(record.get("role", "photo") == "photo" for record in records),
        "xmp_count": sum(record.get("role") == "xmp" for record in records),
        "moved_count": counts["in_trash"],
        "restored_count": counts["restored"],
        "failed_count": counts["failed"],
        "remaining_count": counts["in_trash"],
        "conflict_count": counts["conflict"],
        "modified_count": counts["modified"],
        "lost_count": counts["lost"],
        "needs_attention": bool(counts["conflict"] + counts["modified"] + counts["lost"]),
        "rollbackable": bool(counts["in_trash"] + counts["conflict"]),
        "roots": manifest.get("roots", {}),
        "manifest_path": str(manifest_file),
    }


def _validate_operation_source(record: dict[str, Any]) -> tuple[Path, Path]:
    source = Path(record["original_path"])
    root = Path(record["root"])
    target = Path(record["trash_path"])
    if _is_link_like(source) or _is_link_like(root):
        raise ValueError(f"源文件或目录属于链接：{source}")
    try:
        resolved_source = source.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"源文件不存在或无法读取：{source}") from exc
    if not resolved_source.is_file() or not _inside(resolved_source, resolved_root):
        raise ValueError(f"候选文件范围异常：{source}")
    if not _inside(target, resolved_root):
        raise ValueError(f"回收路径超出照片目录：{target}")
    for parent in (target.parent, *target.parents):
        if parent == resolved_root or parent.parent == parent:
            break
        if _lexists(parent) and _is_link_like(parent):
            raise ValueError(f"回收路径经过链接或目录联接点：{parent}")
    if not _content_matches(source, record.get("snapshot", {})):
        raise ValueError(f"文件内容已变化：{source}")
    if _lexists(target):
        raise ValueError(f"回收区已存在同名文件：{target}")
    return source, target


def _validate_live_candidate(plan: dict[str, Any], candidate: dict[str, Any]) -> None:
    photo_record = {
        "original_path": candidate["path"],
        "root": candidate["root"],
        "trash_path": str(_trash_target(candidate, plan["plan_id"])),
        "snapshot": candidate["snapshot"],
    }
    _validate_operation_source(photo_record)
    for sidecar in candidate.get("sidecars", []):
        sidecar_record = {
            "original_path": sidecar["path"],
            "root": sidecar["root"],
            "trash_path": str(_trash_target({**sidecar, "side": "xmp"}, plan["plan_id"])),
            "snapshot": sidecar["snapshot"],
        }
        _validate_operation_source(sidecar_record)

    roots = plan.get("roots", {})
    raw_root = Path(roots.get("mixed") or roots.get("raw"))
    jpeg_root = Path(roots.get("mixed") or roots.get("jpeg"))
    relative = Path(candidate["relative_path"])
    stem = relative.stem
    counterpart_root = jpeg_root if candidate["side"] == "raw" else raw_root
    counterpart_extensions = plan["jpeg_extensions"] if candidate["side"] == "raw" else plan["raw_extensions"]
    for extension in counterpart_extensions:
        counterpart = counterpart_root / relative.parent / f"{stem}{extension}"
        if _lexists(counterpart):
            raise ValueError(f"执行前发现新配对文件，请重新扫描：{counterpart}")

    expected_sidecars = {str(Path(item["path"]).resolve()).casefold() for item in candidate.get("sidecars", [])}
    possible_names = {f"{stem}.xmp", f"{relative.name}.xmp"}
    for root in {raw_root, jpeg_root}:
        for name in possible_names:
            sidecar = root / relative.parent / name
            if _lexists(sidecar) and str(sidecar.resolve()).casefold() not in expected_sidecars:
                raise ValueError(f"执行前发现新 XMP，请重新扫描：{sidecar}")


def execute_raw_jpeg_plan(plan_file: Path, manifest_root: Path) -> dict[str, Any]:
    manifest_root.mkdir(parents=True, exist_ok=True)
    with toolbox_operation_lock(manifest_root):
        return _execute_raw_jpeg_plan_locked(plan_file, manifest_root)


def _execute_raw_jpeg_plan_locked(plan_file: Path, manifest_root: Path) -> dict[str, Any]:
    plan = load_plan(plan_file)
    if plan.get("status") != "planned":
        raise ValueError("这个操作计划已经执行或正在执行。")
    candidates = list(plan.get("candidates", []))
    operations = _candidate_operations(candidates)
    phase_start("verify", "复核目录", 1, unit="项")
    _validate_plan_is_current(plan)

    records: list[dict[str, Any]] = []
    for operation in operations:
        target = _trash_target(operation, plan["plan_id"])
        record = {
            "original_path": operation["path"],
            "trash_path": str(target),
            "root": operation["root"],
            "relative_path": operation["relative_path"],
            "side": operation["side"],
            "role": operation["role"],
            "candidate_index": operation["candidate_index"],
            "snapshot": operation["snapshot"],
            "status": "planned",
            "error": None,
        }
        _validate_operation_source(record)
        records.append(record)
    phase_end("verify", "复核目录", 1, unit="项")

    manifest_file = transaction_path(manifest_root, plan["plan_id"])
    if _lexists(manifest_file):
        raise ValueError("操作记录已存在，已拒绝重复执行。")
    manifest = {
        "schema_version": RAW_JPEG_SCHEMA_VERSION,
        "transaction_id": plan["plan_id"],
        "plan_id": plan["plan_id"],
        "created_at": _now(),
        "updated_at": _now(),
        "status": "running",
        "layout": plan["layout"],
        "direction": plan["direction"],
        "roots": plan["roots"],
        "records": records,
    }
    write_json(manifest_file, manifest)
    plan["status"] = "running"
    plan["transaction_id"] = plan["plan_id"]
    write_json(plan_file, plan)

    total = len(records)
    completed = 0
    record_bundles: list[list[tuple[int, dict[str, Any]]]] = [[] for _candidate in candidates]
    for record_index, record in enumerate(records):
        record_bundles[int(record["candidate_index"])].append((record_index, record))
    phase_start("recycle", "移入同盘回收区", total, unit="个文件")
    with _journal_path(manifest_file).open("a", encoding="utf-8") as journal:
        for candidate_index, candidate in enumerate(candidates):
            indexed_bundle = record_bundles[candidate_index]
            moved_bundle: list[tuple[int, dict[str, Any]]] = []
            try:
                _validate_live_candidate(plan, candidate)
                for index, record in indexed_bundle:
                    source, target = _validate_operation_source(record)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if _lexists(target):
                        raise OSError(f"回收区已存在同名文件：{target}")
                    source.rename(target)
                    moved_bundle.append((index, record))
                    if not _content_matches(target, record["snapshot"]):
                        raise OSError(f"移动后的文件指纹不一致：{target}")
                    record["status"] = "moved"
                    record.pop("error", None)
                    _append_record_event(journal, index, record)
            except (OSError, ValueError) as exc:
                message = str(exc)
                for index, record in reversed(moved_bundle):
                    source = Path(record["original_path"])
                    target = Path(record["trash_path"])
                    try:
                        if _lexists(source) or _record_state(record) != "in_trash":
                            raise OSError("无法安全恢复已移动文件")
                        source.parent.mkdir(parents=True, exist_ok=True)
                        target.rename(source)
                        record["status"] = "failed"
                        record["error"] = f"同组移动失败，已恢复原位：{message}"
                    except OSError as restore_exc:
                        record["status"] = "moved"
                        record["error"] = f"同组移动失败且自动恢复失败：{restore_exc}"
                    _append_record_event(journal, index, record)
                moved_ids = {index for index, _record in moved_bundle}
                for index, record in indexed_bundle:
                    if index in moved_ids:
                        continue
                    record["status"] = "failed"
                    record["error"] = message
                    _append_record_event(journal, index, record)
            for _index, _record in indexed_bundle:
                completed += 1
                emit_progress("recycle", "移入同盘回收区", completed, total, unit="个文件")
    phase_end("recycle", "移入同盘回收区", total, unit="个文件")

    phase_start("finalize", "保存操作记录", 1, unit="项")
    counts = _reconcile_records(records)
    failed_count = sum(record.get("status") == "failed" or bool(record.get("error")) for record in records)
    manifest["status"] = "completed_with_errors" if failed_count or counts["conflict"] + counts["modified"] + counts["lost"] else "completed"
    manifest["updated_at"] = _now()
    manifest["moved_count"] = counts["in_trash"]
    manifest["remaining_count"] = counts["in_trash"]
    manifest["restored_count"] = 0
    manifest["failed_count"] = failed_count
    manifest["conflict_count"] = counts["conflict"]
    manifest["modified_count"] = counts["modified"]
    manifest["lost_count"] = counts["lost"]
    _compact_manifest(manifest_file, manifest)
    plan["status"] = manifest["status"]
    plan["finished_at"] = _now()
    write_json(plan_file, plan)
    phase_end("finalize", "保存操作记录", 1, unit="项")
    return transaction_summary(manifest, manifest_file)


def rollback_raw_jpeg_manifest(manifest_file: Path) -> dict[str, Any]:
    with toolbox_operation_lock(manifest_file.parent):
        return _rollback_raw_jpeg_manifest_locked(manifest_file)


def _rollback_raw_jpeg_manifest_locked(manifest_file: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_file)
    records = list(manifest.get("records", []))
    manifest["status"] = "rolling_back"
    manifest["updated_at"] = _now()
    _compact_manifest(manifest_file, manifest)

    locations = _reconcile_records(records)
    location_errors = {
        "conflict": "原位置与回收区同时存在文件，未覆盖",
        "modified": "原位置或回收文件已被修改",
        "lost": "原位置与回收区均未找到文件",
    }
    for record in records:
        state = record.get("location_state")
        if state in location_errors:
            record["rollback_error"] = location_errors[state]
    pending = [(index, record) for index, record in enumerate(records) if record.get("location_state") == "in_trash"]
    allowed_roots: set[str] = set()
    for value in manifest.get("roots", {}).values():
        try:
            allowed_roots.add(str(Path(value).resolve(strict=True)).casefold())
        except OSError:
            continue
    phase_start("verify", "复核回收文件", len(pending), unit="个文件")
    valid: list[tuple[int, dict[str, Any], Path, Path]] = []
    for position, (record_index, record) in enumerate(pending, start=1):
        root = Path(record["root"])
        original = Path(record["original_path"])
        trash = Path(record["trash_path"])
        expected_trash = _trash_target({
            "root": record["root"],
            "relative_path": record["relative_path"],
            "side": record["side"],
        }, manifest["transaction_id"])
        try:
            resolved_root = root.resolve(strict=True)
        except OSError:
            record["rollback_error"] = "照片目录当前无法访问"
        else:
            if str(resolved_root).casefold() not in allowed_roots:
                record["rollback_error"] = "记录中的照片根目录不属于原操作"
            elif trash.resolve() != expected_trash.resolve() or not _inside(original, resolved_root):
                record["rollback_error"] = "路径范围异常"
            elif _record_state(record) != "in_trash":
                record["rollback_error"] = "文件状态已经变化，请重新检查"
            else:
                record.pop("rollback_error", None)
                valid.append((record_index, record, original, trash))
        emit_progress("verify", "复核回收文件", position, len(pending), unit="个文件")
    phase_end("verify", "复核回收文件", len(pending), unit="个文件")

    phase_start("restore", "恢复原位置", len(valid), unit="个文件")
    with _journal_path(manifest_file).open("a", encoding="utf-8") as journal:
        for position, (record_index, record, original, trash) in enumerate(valid, start=1):
            try:
                if _lexists(original) or _record_state(record) != "in_trash":
                    raise OSError("原位置已有文件或回收文件已变化，未覆盖")
                original.parent.mkdir(parents=True, exist_ok=True)
                trash.rename(original)
                if not _content_matches(original, record.get("snapshot", {})):
                    raise OSError("恢复后的文件指纹不一致")
                record["status"] = "restored"
                record["restored_at"] = _now()
                record.pop("error", None)
                record.pop("rollback_error", None)
            except OSError as exc:
                record["rollback_error"] = str(exc)
            _append_record_event(journal, record_index, record)
            emit_progress("restore", "恢复原位置", position, len(valid), unit="个文件")
    phase_end("restore", "恢复原位置", len(valid), unit="个文件")

    phase_start("finalize", "更新操作记录", 1, unit="项")
    locations = _reconcile_records(records)
    restored = sum(record.get("status") == "restored" for record in records)
    unresolved = locations["in_trash"] + locations["conflict"] + locations["modified"] + locations["lost"]
    manifest["status"] = "rolled_back" if unresolved == 0 else "partially_rolled_back"
    manifest["updated_at"] = _now()
    manifest["restored_count"] = restored
    manifest["remaining_count"] = locations["in_trash"]
    manifest["conflict_count"] = locations["conflict"]
    manifest["modified_count"] = locations["modified"]
    manifest["lost_count"] = locations["lost"]
    _compact_manifest(manifest_file, manifest)
    phase_end("finalize", "更新操作记录", 1, unit="项")
    return transaction_summary(manifest, manifest_file)


def load_raw_jpeg_transaction(data_dir: Path, transaction_id: str, *, reconcile: bool = True) -> tuple[dict[str, Any], Path]:
    manifest_file = transaction_path(transactions_root(data_dir), transaction_id)
    manifest = _load_manifest(manifest_file)
    return transaction_summary(manifest, manifest_file, reconcile=reconcile), manifest_file


def list_raw_jpeg_transactions(data_dir: Path, limit: int = 20) -> tuple[list[dict[str, Any]], dict[str, Path]]:
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
    paths = [path for _mtime, path in sorted(candidates, key=lambda item: item[0], reverse=True)[:max(0, limit)]]
    items: list[dict[str, Any]] = []
    mapping: dict[str, Path] = {}
    for path in paths:
        try:
            manifest = _load_manifest(path)
            reconcile = manifest.get("status") in {"running", "rolling_back"}
            summary = transaction_summary(manifest, path, reconcile=reconcile)
        except (OSError, ValueError, TypeError):
            continue
        mapping[path.stem] = path
        items.append(summary)
    return items, mapping
