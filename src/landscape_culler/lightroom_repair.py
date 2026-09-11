from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .lightroom_apply import wait_for_lightroom_batch
from .lightroom_bridge import (
    RESULT_PROTOCOL,
    _parse_line,
    bridge_paths,
    create_lightroom_preview_metadata_repair,
)
from .util import full_fingerprint

_LEGACY_PREVIEW_RESTORE = "xmp_restore=byte-identical-backup"
_MAX_SIDECAR_BYTES = 16 * 1024 * 1024
_COMMITTED_XMP_STATUSES = frozenset({"committed", "done"})


class LightroomRepairError(RuntimeError):
    """Raised before or after a controlled repair violates a safety invariant."""


RepairScanner = Callable[[Path | str], dict[str, Any]]
RepairBatchCreator = Callable[..., dict[str, Any]]
RepairBatchWaiter = Callable[[Path | str, str, float], dict[str, Any]]


def _digest(path: Path) -> tuple[str, int]:
    size = path.stat().st_size
    if size > _MAX_SIDECAR_BYTES:
        raise ValueError(f"XMP 过大，拒绝自动核对：{path}")
    return hashlib.sha256(path.read_bytes()).hexdigest(), size


def _content_summary(path: Path) -> dict[str, Any]:
    """Return a stable, complete content digest without trusting timestamps."""

    try:
        before = path.stat()
        fingerprint = full_fingerprint(path)
        after = path.stat()
    except OSError as exc:
        raise LightroomRepairError(f"无法完整读取文件：{path}：{exc}") from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise LightroomRepairError(f"文件在计算完整摘要时发生变化：{path}")
    if int(fingerprint["size"]) != after.st_size:
        raise LightroomRepairError(f"完整摘要的文件大小不一致：{path}")
    return {"sha256": str(fingerprint["sha256"]), "size": int(fingerprint["size"])}


def _canonical_path(value: Path | str) -> str:
    return str(Path(value).expanduser().resolve()).casefold()


def _candidate_snapshot(
    report: Mapping[str, Any], *, label: str
) -> dict[str, dict[str, Any]]:
    rows = report.get("eligible")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise LightroomRepairError(f"{label}缺少有效的 eligible 候选列表。")
    snapshot: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise LightroomRepairError(f"{label}包含格式无效的候选记录。")
        photo_path = row.get("photo_path")
        xmp_sha256 = row.get("xmp_sha256")
        xmp_size = row.get("xmp_size")
        task_stems = row.get("task_stems")
        if (
            not isinstance(photo_path, str)
            or not photo_path
            or not isinstance(xmp_sha256, str)
            or len(xmp_sha256) != 64
            or isinstance(xmp_size, bool)
            or not isinstance(xmp_size, int)
            or xmp_size < 0
            or not isinstance(task_stems, Sequence)
            or isinstance(task_stems, (str, bytes))
            or not all(isinstance(stem, str) and stem for stem in task_stems)
        ):
            raise LightroomRepairError(
                f"{label}候选记录缺少完整的路径、XMP 摘要或来源任务。"
            )
        key = _canonical_path(photo_path)
        if key in snapshot:
            raise LightroomRepairError(f"{label}包含重复照片：{photo_path}")
        snapshot[key] = {
            "photo_path": str(Path(photo_path).expanduser().resolve()),
            "xmp_sha256": xmp_sha256,
            "xmp_size": xmp_size,
            "task_stems": tuple(sorted(task_stems)),
        }
    return snapshot


def _require_clean_scan(report: Mapping[str, Any], *, label: str) -> None:
    unsafe = report.get("unsafe", [])
    invalid_results = report.get("invalid_results", [])
    if not isinstance(unsafe, Sequence) or isinstance(unsafe, (str, bytes)):
        raise LightroomRepairError(f"{label}的 unsafe 记录格式无效。")
    if unsafe:
        raise LightroomRepairError(
            f"{label}发现 {len(unsafe)} 张不安全照片，拒绝执行修复。"
        )
    if not isinstance(invalid_results, Sequence) or isinstance(
        invalid_results, (str, bytes)
    ):
        raise LightroomRepairError(f"{label}的 invalid_results 记录格式无效。")
    if invalid_results:
        raise LightroomRepairError(
            f"{label}发现 {len(invalid_results)} 条无法解析的历史结果，拒绝执行修复。"
        )


def _describe_candidate_drift(
    approved: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
) -> str:
    approved_keys = set(approved)
    current_keys = set(current)
    added = current_keys - approved_keys
    removed = approved_keys - current_keys
    changed = {
        key for key in approved_keys & current_keys if approved[key] != current[key]
    }
    return f"新增 {len(added)}、移除 {len(removed)}、摘要或来源变化 {len(changed)}"


def _validate_completed_batch(
    status: Mapping[str, Any],
    expected_paths: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if status.get("status") != "complete":
        raise LightroomRepairError(
            f"Lightroom 修复批次未完整成功：{status.get('status')!r}"
        )
    tasks = status.get("tasks")
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        raise LightroomRepairError("Lightroom 修复批次缺少任务结果。")
    if len(tasks) != len(expected_paths):
        raise LightroomRepairError(
            f"Lightroom 修复批次任务数不一致：预期 {len(expected_paths)}，实际 {len(tasks)}。"
        )

    validated: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, Mapping):
            raise LightroomRepairError("Lightroom 修复批次包含格式无效的任务结果。")
        result = task.get("result")
        if task.get("status") != "done" or not isinstance(result, Mapping):
            raise LightroomRepairError(
                f"Lightroom 修复任务未完成：{task.get('photo_path', '未知照片')}"
            )
        if result.get("status") != "done":
            raise LightroomRepairError(
                f"Lightroom 修复结果不是 done：{task.get('photo_path', '未知照片')}"
            )
        if result.get("task_type") != "repair_preview_metadata":
            raise LightroomRepairError(
                "Lightroom 返回了非 repair_preview_metadata 任务结果。"
            )
        if result.get("xmp_status") not in _COMMITTED_XMP_STATUSES:
            raise LightroomRepairError(
                f"Lightroom 未确认 XMP 已提交：{result.get('xmp_status')!r}"
            )
        cleanup_count = result.get("cleanup_count")
        if (
            isinstance(cleanup_count, bool)
            or not isinstance(cleanup_count, int)
            or cleanup_count < 0
        ):
            raise LightroomRepairError("Lightroom 修复结果缺少有效的 cleanup_count。")

        photo_path = result.get("photo_path") or task.get("photo_path")
        if not isinstance(photo_path, str) or not photo_path:
            raise LightroomRepairError("Lightroom 修复结果缺少照片路径。")
        key = _canonical_path(photo_path)
        if key not in expected_paths:
            raise LightroomRepairError(f"Lightroom 返回了未批准的照片：{photo_path}")
        if key in validated:
            raise LightroomRepairError(f"Lightroom 重复返回照片：{photo_path}")
        validated[key] = {
            "task_id": result.get("task_id") or task.get("task_id"),
            "task_status": "done",
            "result_status": "done",
            "xmp_status": str(result["xmp_status"]),
            "cleanup_count": cleanup_count,
        }

    missing = set(expected_paths).difference(validated)
    if missing:
        first = expected_paths[next(iter(missing))]["photo_path"]
        raise LightroomRepairError(f"Lightroom 未返回已批准照片的结果：{first}")
    return validated


def find_preview_metadata_repair_candidates(data_dir: Path | str) -> dict[str, Any]:
    """Find photos affected by the legacy transient-preview XMP replacement.

    This is deliberately read-only. A photo is eligible only when every retained
    immutable backup for its legacy preview has the same bytes as the current
    sidecar. Any missing or differing artifact moves the photo to ``unsafe`` so a
    repair caller cannot silently choose between Lightroom and disk metadata.
    """

    paths = bridge_paths(data_dir)
    grouped: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    invalid_results: list[dict[str, str]] = []
    for directory in (paths.done, paths.failed, paths.cancelled):
        if not directory.is_dir():
            continue
        for result_path in sorted(directory.glob("*.result")):
            try:
                fields = _parse_line(
                    result_path.read_text(encoding="utf-8"), RESULT_PROTOCOL
                )
            except (OSError, TypeError, ValueError) as exc:
                invalid_results.append(
                    {"result_path": str(result_path), "reason": str(exc)}
                )
                continue
            if (
                fields.get("task_type") != "preview"
                or fields.get("restore_status") != "done"
                or _LEGACY_PREVIEW_RESTORE not in str(fields.get("message", ""))
            ):
                continue
            photo_path = str(fields.get("photo_path", ""))
            if not photo_path:
                continue
            grouped[photo_path.casefold()].append((result_path, fields))

    eligible: list[dict[str, Any]] = []
    unsafe: list[dict[str, Any]] = []
    for rows in grouped.values():
        photo_path = Path(str(rows[0][1]["photo_path"]))
        sidecar_path = photo_path.with_suffix(".xmp")
        reasons: list[str] = []
        current_digest: str | None = None
        current_size: int | None = None
        if not photo_path.is_file():
            reasons.append("RAW 当前不可访问")
        if not sidecar_path.is_file():
            reasons.append("当前 XMP 不存在")
        else:
            try:
                current_digest, current_size = _digest(sidecar_path)
            except (OSError, ValueError) as exc:
                reasons.append(str(exc))

        task_stems: list[str] = []
        matching_backups = 0
        for result_path, _fields in rows:
            stem = result_path.stem
            task_stems.append(stem)
            backup_path = paths.backups / f"{stem}.xmp.original"
            if not backup_path.is_file():
                reasons.append(f"缺少预览前备份：{stem}")
                continue
            try:
                backup_digest, backup_size = _digest(backup_path)
            except (OSError, ValueError) as exc:
                reasons.append(str(exc))
                continue
            if (
                current_digest is None
                or backup_digest != current_digest
                or backup_size != current_size
            ):
                reasons.append(f"当前 XMP 与预览前备份不同：{stem}")
            else:
                matching_backups += 1

        record: dict[str, Any] = {
            "photo_path": str(photo_path),
            "xmp_path": str(sidecar_path),
            "preview_task_count": len(rows),
            "matching_backup_count": matching_backups,
            "task_stems": sorted(task_stems),
        }
        if current_digest is not None:
            record["xmp_sha256"] = current_digest
            record["xmp_size"] = current_size
        if reasons:
            record["reasons"] = sorted(set(reasons))
            unsafe.append(record)
        else:
            eligible.append(record)

    eligible.sort(key=lambda row: str(row["photo_path"]).casefold())
    unsafe.sort(key=lambda row: str(row["photo_path"]).casefold())
    return {
        "eligible_count": len(eligible),
        "unsafe_count": len(unsafe),
        "legacy_preview_result_count": sum(len(rows) for rows in grouped.values()),
        "eligible": eligible,
        "unsafe": unsafe,
        "invalid_results": invalid_results,
    }


def execute_preview_metadata_repair(
    data_dir: Path | str,
    approved_scan: Mapping[str, Any],
    *,
    batch_id: str | None = None,
    timeout: float = 7200.0,
    scanner: RepairScanner = find_preview_metadata_repair_candidates,
    batch_creator: RepairBatchCreator = create_lightroom_preview_metadata_repair,
    batch_waiter: RepairBatchWaiter = wait_for_lightroom_batch,
) -> dict[str, Any]:
    """Execute a previously approved metadata repair under strict audit controls.

    ``approved_scan`` must be the unmodified result of
    :func:`find_preview_metadata_repair_candidates` shown to the caller.  The
    function rescans immediately before publication and rejects unsafe rows,
    invalid history, or any change to the candidate paths, XMP digests, or
    legacy task provenance.  The injected creator and waiter make the safety
    contract testable without publishing a real Lightroom task.
    """

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise LightroomRepairError("Lightroom 修复等待时间必须大于 0 秒。")
    if not isinstance(approved_scan, Mapping):
        raise LightroomRepairError("批准快照格式无效。")

    _require_clean_scan(approved_scan, label="批准快照")
    approved = _candidate_snapshot(approved_scan, label="批准快照")
    if not approved:
        raise LightroomRepairError("批准快照中没有可修复照片。")

    rescanned = scanner(data_dir)
    if not isinstance(rescanned, Mapping):
        raise LightroomRepairError("即时重扫结果格式无效。")
    _require_clean_scan(rescanned, label="即时重扫")
    current = _candidate_snapshot(rescanned, label="即时重扫")
    if approved != current:
        raise LightroomRepairError(
            "修复候选集合已漂移，拒绝发布 Lightroom 队列："
            + _describe_candidate_drift(approved, current)
            + "。"
        )

    started_at = datetime.now(UTC).isoformat()
    ordered_keys = sorted(current)
    preflight: dict[str, dict[str, Any]] = {}
    for key in ordered_keys:
        candidate = current[key]
        photo_path = Path(str(candidate["photo_path"]))
        xmp_path = photo_path.with_suffix(".xmp")
        raw_summary = _content_summary(photo_path)
        xmp_summary = _content_summary(xmp_path)
        expected_xmp = {
            "sha256": candidate["xmp_sha256"],
            "size": candidate["xmp_size"],
        }
        if xmp_summary != expected_xmp:
            raise LightroomRepairError(
                f"XMP 在即时重扫后再次发生变化，拒绝发布 Lightroom 队列：{xmp_path}"
            )
        preflight[key] = {
            "photo_path": str(photo_path),
            "xmp_path": str(xmp_path),
            "raw_before": raw_summary,
            "xmp_before": xmp_summary,
            "task_stems": list(candidate["task_stems"]),
        }

    # Full RAW hashing can take long enough for another preview result or XMP
    # edit to arrive.  Close that scan-to-publish window with one final scan
    # after all content summaries have been captured.
    publish_scan = scanner(data_dir)
    if not isinstance(publish_scan, Mapping):
        raise LightroomRepairError("发布前重扫结果格式无效。")
    _require_clean_scan(publish_scan, label="发布前重扫")
    publish_snapshot = _candidate_snapshot(publish_scan, label="发布前重扫")
    if current != publish_snapshot:
        raise LightroomRepairError(
            "修复候选集合在发布前再次漂移，拒绝发布 Lightroom 队列："
            + _describe_candidate_drift(current, publish_snapshot)
            + "。"
        )

    published: Mapping[str, Any] | None = None
    completed: Mapping[str, Any] | None = None
    validated: dict[str, dict[str, Any]] = {}
    xmp_after: dict[str, dict[str, Any]] = {}
    raw_after: dict[str, dict[str, Any]] = {}
    processing_error: Exception | None = None
    resolved_batch_id = ""
    try:
        photo_paths = [preflight[key]["photo_path"] for key in ordered_keys]
        published = batch_creator(data_dir, photo_paths, batch_id=batch_id)
        if not isinstance(published, Mapping):
            raise LightroomRepairError("Lightroom 修复批次发布结果格式无效。")
        resolved_batch_id = str(published.get("batch_id", ""))
        if not resolved_batch_id:
            raise LightroomRepairError("Lightroom 修复批次发布结果缺少 batch_id。")
        if batch_id is not None and resolved_batch_id != batch_id:
            raise LightroomRepairError(
                f"Lightroom 修复批次 ID 不一致：预期 {batch_id}，实际 {resolved_batch_id}。"
            )
        published_count = published.get("task_count")
        if (
            isinstance(published_count, bool)
            or not isinstance(published_count, int)
            or published_count != len(ordered_keys)
        ):
            raise LightroomRepairError(
                "Lightroom 修复批次发布数量与批准候选数量不一致。"
            )
        published_tasks = published.get("tasks")
        if not isinstance(published_tasks, Sequence) or isinstance(
            published_tasks, (str, bytes)
        ):
            raise LightroomRepairError("Lightroom 修复批次发布结果缺少任务清单。")
        published_keys: set[str] = set()
        for task in published_tasks:
            if not isinstance(task, Mapping) or not isinstance(
                task.get("photo_path"), str
            ):
                raise LightroomRepairError("Lightroom 修复批次发布了格式无效的任务。")
            key = _canonical_path(str(task["photo_path"]))
            if key in published_keys:
                raise LightroomRepairError("Lightroom 修复批次发布了重复照片。")
            published_keys.add(key)
        if published_keys != set(current):
            raise LightroomRepairError("Lightroom 修复批次发布了未批准或缺失的照片。")

        completed = batch_waiter(data_dir, resolved_batch_id, float(timeout))
        if not isinstance(completed, Mapping):
            raise LightroomRepairError("Lightroom 修复批次等待结果格式无效。")
        if str(completed.get("batch_id", "")) != resolved_batch_id:
            raise LightroomRepairError("Lightroom 修复批次等待结果的 batch_id 不一致。")
        validated = _validate_completed_batch(completed, current)
        for key in ordered_keys:
            xmp_after[key] = _content_summary(Path(preflight[key]["xmp_path"]))
    except Exception as exc:  # noqa: BLE001 - all bridge failures must defer to RAW verification
        processing_error = exc
    finally:
        raw_problems: list[str] = []
        for key in ordered_keys:
            photo_path = Path(preflight[key]["photo_path"])
            try:
                raw_after[key] = _content_summary(photo_path)
            except LightroomRepairError as exc:
                raw_problems.append(f"{photo_path}（{exc}）")
                continue
            if raw_after[key] != preflight[key]["raw_before"]:
                raw_problems.append(str(photo_path))
        if raw_problems:
            raise LightroomRepairError(
                "检测到 RAW 在修复期间发生变化或无法完成复核：" + raw_problems[0]
            ) from processing_error

    if processing_error is not None:
        if isinstance(processing_error, LightroomRepairError):
            raise processing_error
        raise LightroomRepairError(
            f"Lightroom 受控修复未完成：{processing_error}"
        ) from processing_error
    assert published is not None and completed is not None

    photos: list[dict[str, Any]] = []
    for key in ordered_keys:
        outcome = validated[key]
        photos.append(
            {
                **preflight[key],
                "raw_after": raw_after[key],
                "raw_unchanged": True,
                "xmp_after": xmp_after[key],
                **outcome,
            }
        )
    return {
        "status": "complete",
        "batch_id": resolved_batch_id,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "candidate_count": len(photos),
        "published_count": published["task_count"],
        "total_cleanup_count": sum(int(row["cleanup_count"]) for row in photos),
        "accepted_xmp_statuses": sorted(_COMMITTED_XMP_STATUSES),
        "photos": photos,
        "batch_counts": dict(completed.get("counts", {})),
    }
