from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .constants import PROPRIETARY_RAW_EXTENSIONS
from .lightroom_apply import (
    DEFAULT_BATCH_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    LightroomApplyError,
    _as_bool,
    _creative_look_fields,
    _creative_profile_style,
    lightroom_tasks_from_payload,
    resolve_lightroom_executable,
    start_lightroom,
    wait_for_lightroom_batch,
    wait_for_lightroom_online,
)
from .lightroom_bridge import (
    LightroomTask,
    create_lightroom_batch,
    read_lightroom_bridge_status,
    write_lightroom_plugin_config,
)
from .progress import emit_progress, phase_end, phase_start
from .util import full_fingerprint, read_json


def _absolute_existing_raw(value: Any, *, label: str) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise LightroomApplyError(f"{label} 必须是绝对路径。")
    if not path.is_file():
        raise LightroomApplyError(f"照片不存在：{path}")
    if path.suffix.casefold() not in PROPRIETARY_RAW_EXTENSIONS:
        raise LightroomApplyError(f"Lightroom 导出只接受使用独立 XMP 的相机 RAW：{path.name}")
    return path.resolve()


def _output_mode(spec: Mapping[str, Any]) -> str:
    targets = spec.get("targets")
    if not isinstance(targets, Mapping):
        raise LightroomApplyError("导出 spec 缺少 targets。")
    xmp = targets.get("xmp") is True
    jpeg = targets.get("jpeg") is True
    if not xmp and not jpeg:
        raise LightroomApplyError("导出 spec 至少要选择 XMP 或 JPEG。")
    return "both" if xmp and jpeg else "xmp" if xmp else "jpeg"


def _jpeg_output_dir(spec: Mapping[str, Any], *, enabled: bool) -> Path | None:
    settings = spec.get("jpeg_settings") or {}
    if not isinstance(settings, Mapping):
        raise LightroomApplyError("jpeg_settings 必须是对象。")
    if enabled:
        checks = (
            ("quality", settings.get("quality", 90), {90}),
            ("color_space", settings.get("color_space", "sRGB"), {"sRGB"}),
            ("size", settings.get("size", "original"), {"original"}),
            ("sharpening", settings.get("sharpening", "screen_standard"), {"screen_standard"}),
            ("collision", settings.get("collision", "suffix"), {"suffix", "rename"}),
        )
        for key, value, supported in checks:
            if value not in supported:
                raise LightroomApplyError(f"首版不支持 jpeg_settings.{key}={value!r}。")
    value = spec.get("output_dir", settings.get("output_dir"))
    if value is None or value == "":
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise LightroomApplyError("output_dir 必须是绝对路径。")
    return path.resolve()


def _preset_fields(item: Mapping[str, Any]) -> tuple[str | None, str, int]:
    source: Mapping[str, Any] = item
    develop = item.get("develop")
    creative = item.get("creative_style")
    if not isinstance(creative, Mapping) and isinstance(develop, Mapping):
        creative = develop.get("creative_style")
    if isinstance(creative, Mapping):
        status = str(creative.get("status", "confirmed")).strip().lower()
        if status in {"skipped", "disabled"}:
            return None, "catalog", 100
        if status not in {"confirmed", "enabled"}:
            raise LightroomApplyError("creative_style 必须先确认或明确跳过，才能冻结导出。")
        source = creative
        if (
            str(source.get("look_kind") or "").strip().casefold()
            == "lightroom_profile"
            or source.get("profile_name")
        ):
            return None, "catalog", 100
    preset_uuid = source.get("preset_uuid") or source.get("preset_id")
    if preset_uuid in {None, ""}:
        return None, "catalog", 100
    scope = str(source.get("preset_scope", source.get("scope", "catalog"))).strip().lower()
    amount_value = source.get("preset_amount", source.get("amount", 100))
    if isinstance(amount_value, bool):
        raise LightroomApplyError("preset_amount 必须是 0 到 200 的整数。")
    try:
        amount = int(amount_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LightroomApplyError("preset_amount 必须是 0 到 200 的整数。") from exc
    if amount != amount_value or not 0 <= amount <= 200:
        raise LightroomApplyError("preset_amount 必须是 0 到 200 的整数。")
    return str(preset_uuid), scope, amount


def _direct_tasks(
    items: list[Any],
    *,
    output_mode: str,
    output_dir: Path | None,
) -> list[LightroomTask]:
    tasks: list[LightroomTask] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, Mapping) or raw_item.get("excluded") is True:
            continue
        try:
            rating = int(raw_item.get("rating", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if rating < 3:
            continue
        photo_path = _absolute_existing_raw(
            raw_item.get("photo_path", raw_item.get("path")),
            label=f"第 {index + 1} 张照片",
        )
        crop = raw_item.get("crop")
        style = raw_item.get("style")
        if crop is not None and not isinstance(crop, Mapping):
            raise LightroomApplyError(f"{photo_path.name} 的 crop 必须是对象。")
        if style is not None and not isinstance(style, Mapping):
            raise LightroomApplyError(f"{photo_path.name} 的 style 必须是对象。")
        merged_style = dict(style or {})
        develop = raw_item.get("develop")
        profile_recipe: Mapping[str, Any] = raw_item
        if isinstance(develop, Mapping):
            profile_recipe = develop
        merged_style.update(_creative_profile_style(profile_recipe))
        look_fields = _creative_look_fields(profile_recipe)
        preset_uuid, preset_scope, preset_amount = _preset_fields(raw_item)
        tasks.append(
            LightroomTask(
                photo_path=photo_path,
                rating=rating,
                auto_tone=_as_bool(raw_item.get("auto_tone", True)),
                auto_white_balance=_as_bool(raw_item.get("auto_white_balance", True)),
                lens_profile=_as_bool(raw_item.get("lens_profile", True)),
                remove_chromatic_aberration=_as_bool(
                    raw_item.get("remove_chromatic_aberration", raw_item.get("remove_ca", True))
                ),
                crop=crop,
                style=merged_style,
                task_id=str(raw_item.get("task_id") or f"photo-{index:06d}"),
                output_mode=output_mode,
                preset_uuid=preset_uuid,
                preset_scope=preset_scope,
                preset_amount=preset_amount,
                jpeg_output_dir=output_dir,
                **look_fields,
            )
        )
    return tasks


def _tasks_from_frozen_results(
    payload: Mapping[str, Any],
    *,
    output_mode: str,
    output_dir: Path | None,
) -> list[LightroomTask]:
    base_tasks = lightroom_tasks_from_payload(payload)
    records = payload.get("results")
    by_path: dict[str, Mapping[str, Any]] = {}
    if isinstance(records, list):
        for record in records:
            if isinstance(record, Mapping):
                by_path[str(Path(str(record.get("path", ""))).expanduser().resolve())] = record
    tasks: list[LightroomTask] = []
    for task in base_tasks:
        record = by_path.get(str(Path(task.photo_path).resolve()), {})
        preset_uuid, preset_scope, preset_amount = _preset_fields(record)
        tasks.append(
            replace(
                task,
                output_mode=output_mode,
                preset_uuid=preset_uuid,
                preset_scope=preset_scope,
                preset_amount=preset_amount,
                jpeg_output_dir=output_dir,
            )
        )
    return tasks


def export_tasks_from_spec(
    spec: Mapping[str, Any],
    *,
    spec_path: Path | str | None = None,
) -> list[LightroomTask]:
    """Validate a frozen export spec and build independent Lightroom tasks.

    ``items`` is the preferred, fully frozen representation.  For migration,
    omitting it loads the legacy reviewed ``results_path`` and enriches those
    recipes with an optional ``creative_style`` preset selection.
    """

    if not isinstance(spec, Mapping):
        raise LightroomApplyError("导出 spec 格式无效。")
    mode = _output_mode(spec)
    output_dir = _jpeg_output_dir(spec, enabled=mode in {"jpeg", "both"})
    items = spec.get("items")
    if items is not None:
        if not isinstance(items, list):
            raise LightroomApplyError("导出 spec 的 items 必须是列表。")
        if any(isinstance(item, Mapping) and isinstance(item.get("develop"), Mapping) for item in items):
            tasks = _tasks_from_frozen_results(
                {"results": items}, output_mode=mode, output_dir=output_dir
            )
        else:
            tasks = _direct_tasks(items, output_mode=mode, output_dir=output_dir)
    else:
        results_value = spec.get("results_path")
        if not isinstance(results_value, (str, Path)) or not str(results_value):
            raise LightroomApplyError("导出 spec 缺少冻结的 results_path 或 items。")
        results_path = Path(results_value).expanduser()
        if not results_path.is_absolute() and spec_path is not None:
            results_path = Path(spec_path).expanduser().resolve().parent / results_path
        if not results_path.is_absolute() or not results_path.is_file():
            raise LightroomApplyError(f"找不到冻结的 results_path：{results_path}")
        payload = read_json(results_path)
        if not isinstance(payload, Mapping):
            raise LightroomApplyError("冻结的 results_path 格式无效。")
        tasks = _tasks_from_frozen_results(payload, output_mode=mode, output_dir=output_dir)
    if not tasks:
        raise LightroomApplyError("导出 spec 没有可处理的 3 星以上照片。")
    return tasks


def execute_lightroom_export(
    spec_path: Path | str,
    data_dir: Path | str,
    batch_id: str,
    *,
    lightroom_exe: Path | str | None = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    batch_timeout: float = DEFAULT_BATCH_TIMEOUT,
) -> dict[str, Any]:
    """Execute a frozen XMP/JPEG export spec without modifying RAW bytes."""

    resolved_spec = Path(spec_path).expanduser().resolve()
    try:
        spec = read_json(resolved_spec)
    except (OSError, ValueError, TypeError) as exc:
        raise LightroomApplyError(f"无法读取导出 spec：{resolved_spec}") from exc
    tasks = export_tasks_from_spec(spec, spec_path=resolved_spec)
    raw_before = {str(Path(task.photo_path)): full_fingerprint(Path(task.photo_path)) for task in tasks}
    phase_start("launch", "准备 Lightroom 导出", 3)
    config: dict[str, Any] = {}
    executable: Path | None = None
    launched = False
    batch: Mapping[str, Any] = {}
    result: dict[str, Any] | None = None
    processing_error: Exception | None = None
    try:
        config = write_lightroom_plugin_config(data_dir)
        executable = resolve_lightroom_executable(lightroom_exe)
        emit_progress("launch", "准备 Lightroom 导出", 1, 3)
        current = read_lightroom_bridge_status(data_dir)
        heartbeat = current.get("heartbeat") if isinstance(current, Mapping) else None
        online = isinstance(heartbeat, Mapping) and heartbeat.get("state") == "online"
        if not online:
            start_lightroom(executable)
            launched = True
            emit_progress("launch", "启动 Lightroom", 2, 3)
            wait_for_lightroom_online(data_dir, startup_timeout)
        phase_end("launch", "Lightroom 已连接", 3)
        phase_start("queue", "提交导出队列", len(tasks), unit="张")
        batch = create_lightroom_batch(data_dir, tasks, batch_id=batch_id)
        phase_end("queue", "提交导出队列", len(tasks), unit="张")
        result = wait_for_lightroom_batch(data_dir, batch_id, batch_timeout)
    except Exception as exc:  # noqa: BLE001 - RAW verification must run after every bridge failure.
        processing_error = exc
    finally:
        phase_start("finalize", "确认 RAW 未改变", len(tasks), unit="张")
        changed: list[str] = []
        for position, task in enumerate(tasks, start=1):
            photo_path = Path(task.photo_path)
            if full_fingerprint(photo_path) != raw_before[str(photo_path)]:
                changed.append(str(photo_path))
            emit_progress("finalize", "确认 RAW 未改变", position, len(tasks), unit="张")
        if changed:
            raise LightroomApplyError(f"检测到 RAW 文件在导出期间发生变化：{changed[0]}") from processing_error
        phase_end("finalize", "确认 RAW 未改变", len(tasks), unit="张")
    if processing_error is not None:
        raise processing_error
    assert result is not None and executable is not None
    targets = spec.get("targets") if isinstance(spec, Mapping) else {}
    return {
        **result,
        "spec_path": str(resolved_spec),
        "results_path": str(spec.get("results_path", "")),
        "targets": dict(targets) if isinstance(targets, Mapping) else {},
        "bridge_root": config["root"],
        "lightroom_executable": str(executable),
        "lightroom_launched": launched,
        "published_count": int(batch.get("task_count", len(tasks))),
    }
