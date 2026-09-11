from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .progress import emit_progress, phase_end, phase_start
from .util import full_fingerprint, read_json

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


def _atomic_replace_from_cache(cache_path: Path, destination: Path) -> None:
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
            _atomic_replace_from_cache(rendered.cache_path, jpeg_path)
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
