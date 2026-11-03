from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .progress import emit_progress, phase_end, phase_start
from .util import full_fingerprint, read_json, write_json

LUT_EXPORT_PROTOCOL = "PHOTO_AI_LUT_EXPORT/1"
_MAX_TASK_BYTES = 8 * 1024 * 1024
_ITEM_ID = re.compile(r"^[^\x00\r\n]{1,160}$")


class LutExportTaskError(ValueError):
    """A malformed or unsafe creative-LUT export task."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_task(path: Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise LutExportTaskError(f"无法读取创意 LUT 导出任务：{exc}") from exc
    if size <= 0 or size > _MAX_TASK_BYTES:
        raise LutExportTaskError("创意 LUT 导出任务大小无效。")
    data_dir = os.environ.get("PHOTO_AI_DATA_DIR")
    if data_dir and not _inside(
        source, Path(data_dir).expanduser().resolve() / "exports"
    ):
        raise LutExportTaskError("创意 LUT 导出任务必须位于受管 state/exports 目录。")
    try:
        payload = read_json(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LutExportTaskError(f"创意 LUT 导出任务不是有效 JSON：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("protocol") != LUT_EXPORT_PROTOCOL:
        raise LutExportTaskError(f"创意 LUT 导出任务必须声明 {LUT_EXPORT_PROTOCOL}。")
    items = payload.get("items")
    if not isinstance(items, list) or not items or len(items) > 10_000:
        raise LutExportTaskError("创意 LUT 导出任务没有有效的照片列表。")
    ids = [str(item.get("item_id") or "") for item in items if isinstance(item, Mapping)]
    if len(ids) != len(items) or len(set(ids)) != len(ids) or any(not _ITEM_ID.fullmatch(value) for value in ids):
        raise LutExportTaskError("创意 LUT 项目编号缺失或重复。")
    return payload


def _engine(project_root: Path | None) -> Any:
    # Heavy imaging bindings remain behind this worker-only command boundary.
    from .creative_lut import CreativeLutEngine

    content_root = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    if content_root:
        from .content_root import resolve_content_root

        layout = resolve_content_root(content_root, apply_environment=True)
        return CreativeLutEngine.for_content_root(layout)
    if project_root is None:
        raise LutExportTaskError("开发模式缺少 project_root，无法定位创意 LUT 资源。")
    return CreativeLutEngine.for_project(Path(project_root).expanduser().resolve())


def _expected_fingerprint(value: Any) -> tuple[int, str]:
    if not isinstance(value, Mapping):
        raise LutExportTaskError("基础 JPEG 缺少冻结指纹。")
    try:
        size = int(value["size"])
        digest = str(value["sha256"]).casefold()
    except (KeyError, TypeError, ValueError) as exc:
        raise LutExportTaskError("基础 JPEG 冻结指纹无效。") from exc
    if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise LutExportTaskError("基础 JPEG 冻结指纹无效。")
    return size, digest


def _task_digest(task: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(task, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _checkpoint_path(task_path: Path, task_digest: str, item_id: str) -> Path:
    key = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return Path(task_path).parent / "lut-export-checkpoints" / task_digest / f"{key}.json"


def _fingerprint_matches(path: Path, expected: Any) -> bool:
    try:
        size, digest = _expected_fingerprint(expected)
        current = full_fingerprint(path)
        return current["size"] == size and current["sha256"].casefold() == digest
    except (OSError, TypeError, ValueError):
        return False


def verified_export_lut_results(task_path: Path, result: Any = None) -> dict[str, Any]:
    """Recover completed files, including a crash after replace but before ACK.

    This read-only verifier has no image/model imports and is safe in Service.
    Neither an old checkpoint nor a worker success flag proves completion:
    the exact task identity, recipe, input and current output bytes must match.
    """
    task = _read_task(task_path)
    digest = _task_digest(task)
    candidates: list[dict[str, Any]] = []
    for item in task["items"]:
        checkpoint = _checkpoint_path(task_path, digest, str(item["item_id"]))
        try:
            saved = read_json(checkpoint) if checkpoint.stat().st_size <= 128 * 1024 else {}
            if isinstance(saved, dict) and saved.get("task_sha256") == digest and isinstance(saved.get("item"), dict):
                candidates.append(saved["item"])
        except (OSError, ValueError, TypeError):
            pass
    if (isinstance(result, dict) and result.get("protocol") == LUT_EXPORT_PROTOCOL
        and result.get("export_spec_id") == task.get("export_spec_id")
        and result.get("attempt_id") == task.get("attempt_id")):
        candidates.extend(result.get("items", []))
    originals = {str(item["item_id"]): item for item in task["items"] if isinstance(item, dict) and item.get("item_id")}
    verified = {}
    for row in candidates:
        try:
            original = originals[str(row["item_id"])]
            target = Path(original["jpeg_path"]).resolve()
            if task.get("output_root") and not _inside(target, Path(task["output_root"])):
                continue
            if (Path(row["output"]).resolve() != target
                or row.get("lut_id") != original.get("lut_id")
                or str(row.get("lut_hash")).casefold() != str(original.get("lut_hash")).casefold()
                or row.get("strength") != original.get("strength", 100)
                or _expected_fingerprint(row.get("input_fingerprint")) != _expected_fingerprint(original.get("input_fingerprint"))
                or not _fingerprint_matches(target, row.get("output_fingerprint"))):
                continue
            verified[str(row["item_id"])] = {**row, "succeeded": True, "recovered": True}
        except (KeyError, OSError, TypeError, ValueError):
            continue
    return {"protocol": LUT_EXPORT_PROTOCOL, "export_spec_id": task.get("export_spec_id"),
            "attempt_id": task.get("attempt_id"), "items": list(verified.values())}


def reusable_export_lut_inputs(task_path: Path) -> dict[str, Any]:
    """Return only unmodified Lightroom bases eligible for a LUT-only retry."""
    task = _read_task(task_path)
    return {**task, "items": [item for item in task["items"]
        if isinstance(item, dict) and item.get("jpeg_path")
        and (not task.get("output_root") or _inside(Path(item["jpeg_path"]), Path(task["output_root"])))
        and _fingerprint_matches(Path(item["jpeg_path"]), item.get("input_fingerprint"))]}


def _atomic_replace_from_cache(cache_path: Path, destination: Path, expected_input: Any) -> None:
    source = Path(cache_path).resolve()
    target = Path(destination).resolve()
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.lut.tmp"
    )
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=4 * 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(source, temporary)
        if not _fingerprint_matches(target, expected_input):
            raise LutExportTaskError("基础 JPEG 在渲染期间已变化，已拒绝覆盖。")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def render_export_luts(
    task_path: Path,
    *,
    project_root: Path | None = None,
    engine_factory: Callable[[Path | None], Any] = _engine,
) -> dict[str, Any]:
    """Render frozen Lightroom JPEGs through the managed heavy worker.

    Every item is independent.  A bad LUT or changed JPEG becomes a normal
    failed export row, while successful rows remain retry-safe and cacheable.
    The caller's PHOTO_AI_WORKER/1 envelope publishes this return value with an
    fsync + atomic replace, so the lightweight Service never imports an image
    stack or guesses whether a partially written result is complete.
    """

    task = _read_task(task_path)
    recovered = {str(row["item_id"]): row for row in verified_export_lut_results(task_path)["items"]}
    receipts = dict(recovered)
    task_digest = _task_digest(task)
    def save_receipt(item_id: str) -> None:
        write_json(_checkpoint_path(task_path, task_digest, item_id),
                   {"task_sha256": task_digest, "item": receipts[item_id]})
    engine = engine_factory(project_root)
    raw_items = task["items"]
    total = len(raw_items)
    quality = int(task.get("jpeg_quality", 90))
    if not 1 <= quality <= 100:
        raise LutExportTaskError("JPEG 质量必须位于 1–100。")
    output_root_value = task.get("output_root")
    output_root = (
        Path(str(output_root_value)).expanduser().resolve()
        if output_root_value
        else None
    )
    results: list[dict[str, Any]] = []
    cache_hits = 0
    phase_start("verify", "核对基础 JPEG 与 LUT", total, unit="张")
    phase_start("render", "渲染创意 LUT", total, unit="张")
    for position, raw in enumerate(raw_items, start=1):
        item_id = str(raw.get("item_id") or "") if isinstance(raw, Mapping) else ""
        row: dict[str, Any] = {"item_id": item_id, "succeeded": False}
        try:
            if item_id in recovered:
                results.append(recovered[item_id])
                cache_hits += 1
                emit_progress("render", "复用已完成创意 LUT 成片", position, total, unit="张", cached=cache_hits)
                continue
            if not isinstance(raw, Mapping) or not _ITEM_ID.fullmatch(item_id):
                raise LutExportTaskError("创意 LUT 项目编号无效。")
            jpeg_path = Path(str(raw.get("jpeg_path") or "")).expanduser().resolve()
            if (
                jpeg_path.suffix.casefold() not in {".jpg", ".jpeg"}
                or not jpeg_path.is_file()
            ):
                raise LutExportTaskError("Lightroom 基础 JPEG 不存在或格式无效。")
            if output_root is not None and not _inside(jpeg_path, output_root):
                raise LutExportTaskError("Lightroom 基础 JPEG 超出冻结输出目录。")
            expected_size, expected_sha256 = _expected_fingerprint(
                raw.get("input_fingerprint")
            )
            observed = full_fingerprint(jpeg_path)
            if (
                int(observed["size"]) != expected_size
                or str(observed["sha256"]).casefold() != expected_sha256
            ):
                raise LutExportTaskError(
                    "Lightroom 基础 JPEG 已变化，已阻止重复或错位套用 LUT。"
                )
            lut_id = str(raw.get("lut_id") or "")
            lut_hash = str(raw.get("lut_hash") or "").casefold()
            descriptor = engine.get_lut(lut_id)
            if not re.fullmatch(r"[0-9a-f]{64}", lut_hash):
                raise LutExportTaskError("冻结 LUT 哈希无效。")
            if str(descriptor.lut_hash).casefold() != lut_hash:
                raise LutExportTaskError("LUT 版本已经变化，已拒绝渲染。")
            strength_value = raw.get("strength", 100)
            if (
                isinstance(strength_value, bool)
                or not isinstance(strength_value, (int, float))
                or int(strength_value) != strength_value
                or not 0 <= int(strength_value) <= 200
            ):
                raise LutExportTaskError("LUT 强度必须是 0 到 200 的整数。")
            rendered = engine.render_jpeg(
                jpeg_path,
                lut_id=lut_id,
                strength=int(strength_value),
                jpeg_quality=quality,
            )
            if (str(rendered.lut_id) != lut_id or str(rendered.lut_hash).casefold() != lut_hash
                or rendered.strength != int(strength_value) or bool(rendered.xmp_compatible)):
                raise LutExportTaskError("LUT 渲染结果与冻结外观或强度不一致。")
            # Persist the expected rendered bytes before committing them. This
            # receipt also covers process termination immediately after replace.
            receipts[item_id] = {
                "item_id": item_id, "output": str(jpeg_path), "lut_id": lut_id,
                "lut_hash": lut_hash, "strength": int(strength_value),
                "input_fingerprint": observed, "output_fingerprint": full_fingerprint(rendered.cache_path),
                "cache_key": str(rendered.cache_key), "cache_hit": bool(rendered.cache_hit),
            }
            save_receipt(item_id)
            _atomic_replace_from_cache(rendered.cache_path, jpeg_path, observed)
            output_fingerprint = full_fingerprint(jpeg_path)
            cache_hits += int(bool(rendered.cache_hit))
            row.update(
                succeeded=True,
                output=str(jpeg_path),
                cache_key=str(rendered.cache_key),
                cache_hit=bool(rendered.cache_hit),
                lut_id=str(rendered.lut_id),
                lut_hash=str(rendered.lut_hash),
                strength=float(rendered.strength),
                input_fingerprint=observed,
                output_fingerprint=output_fingerprint,
            )
            receipts[item_id] = dict(row)
            save_receipt(item_id)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            row["error"] = str(exc)[:1000] or type(exc).__name__
        results.append(row)
        emit_progress(
            "render",
            "渲染创意 LUT",
            position,
            total,
            unit="张",
            cached=cache_hits,
        )
    phase_end("verify", "核对基础 JPEG 与 LUT", total, unit="张")
    phase_end("render", "渲染创意 LUT", total, unit="张", cached=cache_hits)
    succeeded = sum(1 for row in results if row.get("succeeded") is True)
    return {
        "protocol": LUT_EXPORT_PROTOCOL,
        "export_spec_id": str(task.get("export_spec_id") or ""),
        "attempt_id": str(task.get("attempt_id") or ""),
        "status": (
            "completed" if succeeded == total else "partial" if succeeded else "failed"
        ),
        "succeeded": succeeded,
        "failed": total - succeeded,
        "cache_hits": cache_hits,
        "items": results,
    }
