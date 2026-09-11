from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .constants import PROPRIETARY_RAW_EXTENSIONS
from .lightroom_bridge import (
    LightroomTask,
    create_lightroom_batch,
    detect_lightroom_classic_15_3,
    read_lightroom_batch_status,
    read_lightroom_bridge_status,
    write_lightroom_plugin_config,
)
from .progress import emit_progress, phase_end, phase_start
from .util import full_fingerprint, read_json

DEFAULT_STARTUP_TIMEOUT = 120.0
DEFAULT_BATCH_TIMEOUT = 7200.0

# Only ordinary, scalar develop controls are forwarded to
# photo:applyDevelopSettings().  Queue metadata, process-version selection and
# the controls handled by dedicated bridge fields never enter this table.
SAFE_STYLE_SETTINGS = frozenset(
    {
        "EnableTransform",
        "PerspectiveUpright",
        "ConstrainToWarp",
        "Exposure2012",
        "Contrast2012",
        "Highlights2012",
        "Shadows2012",
        "Whites2012",
        "Blacks2012",
        "Texture",
        "Clarity2012",
        "Dehaze",
        "Vibrance",
        "Saturation",
        "Sharpness",
        "SharpenRadius",
        "SharpenDetail",
        "SharpenEdgeMasking",
        "LuminanceSmoothing",
        "LuminanceNoiseReductionDetail",
        "LuminanceNoiseReductionContrast",
        "ColorNoiseReduction",
        "ColorNoiseReductionDetail",
        "ColorNoiseReductionSmoothness",
        "HueAdjustmentRed",
        "HueAdjustmentOrange",
        "HueAdjustmentYellow",
        "HueAdjustmentGreen",
        "HueAdjustmentAqua",
        "HueAdjustmentBlue",
        "HueAdjustmentPurple",
        "HueAdjustmentMagenta",
        "SaturationAdjustmentRed",
        "SaturationAdjustmentOrange",
        "SaturationAdjustmentYellow",
        "SaturationAdjustmentGreen",
        "SaturationAdjustmentAqua",
        "SaturationAdjustmentBlue",
        "SaturationAdjustmentPurple",
        "SaturationAdjustmentMagenta",
        "LuminanceAdjustmentRed",
        "LuminanceAdjustmentOrange",
        "LuminanceAdjustmentYellow",
        "LuminanceAdjustmentGreen",
        "LuminanceAdjustmentAqua",
        "LuminanceAdjustmentBlue",
        "LuminanceAdjustmentPurple",
        "LuminanceAdjustmentMagenta",
        "SplitToningHighlightHue",
        "SplitToningHighlightSaturation",
        "SplitToningShadowHue",
        "SplitToningShadowSaturation",
        "SplitToningBalance",
    }
)


class LightroomApplyError(RuntimeError):
    """An actionable failure while handing a reviewed run to Lightroom."""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "auto", "enabled"}
    return False


def _auto_white_balance(base: Mapping[str, Any]) -> bool:
    explicit = base.get("AutoWhiteBalance")
    if explicit is not None:
        return _as_bool(explicit)
    white_balance = base.get("WhiteBalance")
    return isinstance(white_balance, str) and white_balance.strip().casefold() == "auto"


def _selected_crop(recipe: Mapping[str, Any], *, photo_name: str) -> dict[str, float]:
    crop_id = str(recipe.get("crop_id", "")).strip()
    candidates = recipe.get("crop_candidates")
    if not crop_id or not isinstance(candidates, list):
        raise LightroomApplyError(f"{photo_name} 的裁切方案不完整，请回到裁切页重新确认。")
    selected = next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, Mapping) and str(candidate.get("id", "")) == crop_id
        ),
        None,
    )
    if selected is None or not isinstance(selected.get("bounds"), Mapping):
        raise LightroomApplyError(f"{photo_name} 找不到已确认的裁切方案 {crop_id!r}。")
    bounds = selected["bounds"]
    try:
        crop = {
            "left": float(bounds["left"]),
            "top": float(bounds["top"]),
            "right": float(bounds["right"]),
            "bottom": float(bounds["bottom"]),
            "angle": float(recipe.get("angle", 0.0)),
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise LightroomApplyError(f"{photo_name} 的裁切坐标无效。") from exc
    # LightroomTask performs the final range and finiteness validation.
    return crop


def _safe_style(base: Mapping[str, Any]) -> dict[str, bool | int | float | str]:
    style: dict[str, bool | int | float | str] = {}
    for key, value in base.items():
        name = str(key)
        if name not in SAFE_STYLE_SETTINGS:
            continue
        if isinstance(value, (bool, int, float, str)):
            style[name] = value
    return style


def _creative_profile_style(recipe: Mapping[str, Any]) -> dict[str, str | float]:
    creative = recipe.get("creative_style")
    if not isinstance(creative, Mapping):
        return {}
    status = str(creative.get("status") or "skipped").strip().casefold()
    if status in {"skipped", "disabled"}:
        return {}
    if status not in {"confirmed", "enabled"}:
        raise LightroomApplyError("创意外观必须先确认或明确跳过。")
    # A Creative Look is a nested Lightroom ``Look`` table.  Its display name
    # is not a CameraProfile and its UUID is not an ordinary preset UUID.
    # Dedicated descriptor fields are validated by _creative_look_fields().
    return {}


def _creative_look_fields(recipe: Mapping[str, Any]) -> dict[str, Any]:
    creative = recipe.get("creative_style")
    if not isinstance(creative, Mapping):
        return {}
    status = str(creative.get("status") or "skipped").strip().casefold()
    if status in {"skipped", "disabled"}:
        return {}
    if status not in {"confirmed", "enabled"}:
        raise LightroomApplyError("创意外观必须先确认或明确跳过。")
    look_kind = str(creative.get("look_kind") or "").strip().casefold()
    look_values = (
        creative.get("look_descriptor_path"),
        creative.get("look_descriptor_hash"),
        creative.get("look_uuid"),
    )
    if look_kind != "lightroom_profile" and not any(value not in {None, ""} for value in look_values):
        return {}
    if creative.get("preset_uuid") not in {None, ""}:
        raise LightroomApplyError("Creative Look 不能作为普通 Lightroom 预设导出，请重新准备导出。")
    if not all(value not in {None, ""} for value in look_values):
        raise LightroomApplyError("Creative Look 缺少已冻结的安全描述文件，请重新准备导出。")
    amount_value = creative.get("look_amount", creative.get("amount", 100))
    if isinstance(amount_value, bool):
        raise LightroomApplyError("创意外观强度必须是 0 到 200 的整数。")
    try:
        amount = int(amount_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LightroomApplyError("创意外观强度必须是 0 到 200 的整数。") from exc
    if amount != amount_value or not 0 <= amount <= 200:
        raise LightroomApplyError("创意外观强度必须是 0 到 200 的整数。")
    return {
        "look_descriptor_path": look_values[0],
        "look_descriptor_hash": str(look_values[1]),
        "look_uuid": str(look_values[2]),
        "look_amount": amount,
    }


def lightroom_tasks_from_payload(payload: Mapping[str, Any]) -> list[LightroomTask]:
    """Translate confirmed reviewed results into a read-only Lightroom queue.

    The function never opens a photo.  It only validates paths and serializes
    the already-confirmed recipe for the Lightroom plug-in.
    """

    results = payload.get("results")
    if not isinstance(results, list):
        raise LightroomApplyError("选片结果缺少 results 列表。")
    tasks: list[LightroomTask] = []
    for index, item in enumerate(results):
        if not isinstance(item, Mapping):
            continue
        try:
            rating = int(item.get("rating", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        recipe = item.get("develop")
        if (
            item.get("excluded")
            or rating < 3
            or not isinstance(recipe, Mapping)
            or recipe.get("confirmed") is not True
        ):
            continue
        photo_path = Path(str(item.get("path", ""))).expanduser()
        if not photo_path.is_absolute():
            raise LightroomApplyError(f"第 {index + 1} 张照片不是绝对路径，已停止提交。")
        if not photo_path.is_file():
            raise LightroomApplyError(f"照片不存在：{photo_path}")
        if photo_path.suffix.casefold() not in PROPRIETARY_RAW_EXTENSIONS:
            raise LightroomApplyError(
                f"为保证 RAW 文件本身不被修改，Lightroom 自动处理只接受独立 XMP 的相机 RAW：{photo_path.name}"
            )
        base = recipe.get("base")
        if not isinstance(base, Mapping):
            raise LightroomApplyError(f"{photo_path.name} 缺少已确认的 Lightroom 基础方案。")
        style = _safe_style(base)
        style.update(_creative_profile_style(recipe))
        look_fields = _creative_look_fields(recipe)
        task = LightroomTask(
            photo_path=photo_path.resolve(),
            rating=rating,
            auto_tone=_as_bool(base.get("AutoTone")),
            auto_white_balance=_auto_white_balance(base),
            lens_profile=_as_bool(base.get("LensProfileEnable")),
            remove_chromatic_aberration=_as_bool(base.get("AutoLateralCA")),
            crop=_selected_crop(recipe, photo_name=photo_path.name),
            style=style,
            task_id=f"photo-{index:06d}",
            **look_fields,
        )
        tasks.append(task)
    if not tasks:
        raise LightroomApplyError("没有可交给 Lightroom 的照片：请先确认至少一张 3 星以上照片的裁切与基础调整。")
    return tasks


def load_lightroom_tasks(results_path: Path | str) -> list[LightroomTask]:
    path = Path(results_path).expanduser()
    try:
        payload = read_json(path)
    except (OSError, ValueError, TypeError) as exc:
        raise LightroomApplyError(f"无法读取选片结果：{path}") from exc
    if not isinstance(payload, Mapping):
        raise LightroomApplyError("选片结果格式无效。")
    return lightroom_tasks_from_payload(payload)


def resolve_lightroom_executable(lightroom_exe: Path | str | None = None) -> Path:
    if lightroom_exe is not None:
        executable = Path(lightroom_exe).expanduser().resolve()
        if not executable.is_file():
            raise LightroomApplyError(f"找不到 Lightroom：{executable}")
        return executable
    detected = detect_lightroom_classic_15_3()
    if detected is None:
        raise LightroomApplyError(
            "未检测到兼容的 Lightroom Classic（需要 14.3 或更高版本）。"
            "可在设置中选择 Lightroom.exe，或使用 --lightroom-exe 指定位置。"
        )
    return detected


def start_lightroom(executable: Path | str) -> subprocess.Popen[bytes]:
    path = Path(executable).resolve()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    try:
        return subprocess.Popen(
            [str(path)],
            cwd=str(path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise LightroomApplyError(f"无法启动 Lightroom：{path}（{exc}）") from exc


def wait_for_lightroom_online(
    data_dir: Path | str,
    timeout: float,
    *,
    poll_interval: float = 0.5,
    status_reader: Callable[..., dict[str, Any]] = read_lightroom_bridge_status,
) -> dict[str, Any]:
    if timeout <= 0:
        raise LightroomApplyError("Lightroom 启动等待时间必须大于 0 秒。")
    deadline = time.monotonic() + timeout
    total = max(1, int(round(timeout)))
    phase_start("launch", "等待 Lightroom 插件", total, unit="秒")
    while True:
        status = status_reader(data_dir)
        heartbeat = status.get("heartbeat") if isinstance(status, Mapping) else None
        if isinstance(heartbeat, Mapping) and heartbeat.get("state") == "online":
            phase_end("launch", "等待 Lightroom 插件", total, unit="秒")
            return status
        remaining = deadline - time.monotonic()
        elapsed = min(total, max(0, int(round(timeout - max(0.0, remaining)))))
        emit_progress("launch", "等待 Lightroom 插件", elapsed, total, unit="秒")
        if remaining <= 0:
            root = status.get("root", "") if isinstance(status, Mapping) else ""
            raise LightroomApplyError(
                "Lightroom 已启动，但桥接插件没有上线。请在 Lightroom 的“文件 → 插件管理器”中添加"
                " photo-ai-lightroom.lrplugin 并启用；桥接目录："
                f"{root}"
            )
        time.sleep(min(poll_interval, remaining))


def wait_for_lightroom_batch(
    data_dir: Path | str,
    batch_id: str,
    timeout: float,
    *,
    poll_interval: float = 0.5,
    status_reader: Callable[[Path | str, str], dict[str, Any]] = read_lightroom_batch_status,
    progress_phase: str = "process",
    progress_label: str = "Lightroom 自动处理",
    progress_unit: str = "张",
    progress_offset: int = 0,
    progress_total: int | None = None,
) -> dict[str, Any]:
    if timeout <= 0:
        raise LightroomApplyError("Lightroom 批次等待时间必须大于 0 秒。")
    deadline = time.monotonic() + timeout
    initial = status_reader(data_dir, batch_id)
    batch_total = max(
        0, int(initial.get("task_count", len(initial.get("tasks", []))))
    )
    offset = max(0, int(progress_offset))
    total = (
        offset + batch_total
        if progress_total is None
        else max(offset + batch_total, int(progress_total))
    )
    phase_start(
        progress_phase,
        progress_label,
        total,
        current=offset,
        unit=progress_unit,
        cached=offset,
    )
    status = initial
    while True:
        batch_completed = max(0, int(status.get("completed_count", 0)))
        completed = min(total, offset + batch_completed)
        emit_progress(
            progress_phase,
            progress_label,
            completed,
            total,
            unit=progress_unit,
            cached=offset,
        )
        state = str(status.get("status", ""))
        if state == "complete":
            phase_end(
                progress_phase,
                progress_label,
                total,
                unit=progress_unit,
                cached=offset,
            )
            return status
        if state in {"cancelling", "cancelled"}:
            raise LightroomApplyError(f"Lightroom 批次 {batch_id} 已取消。")
        if state in {"failed", "incomplete"}:
            failures: list[str] = []
            for task in status.get("tasks", []):
                if task.get("status") not in {"failed", "missing"}:
                    continue
                result = task.get("result") if isinstance(task.get("result"), Mapping) else {}
                message = result.get("message") or result.get("error") or task.get("status")
                failures.append(f"{Path(str(task.get('photo_path', '未知照片'))).name}: {message}")
            detail = "；".join(failures[:5]) or "插件返回了失败状态"
            raise LightroomApplyError(f"Lightroom 批次 {batch_id} 失败：{detail}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            counts = status.get("counts", {})
            raise LightroomApplyError(
                f"等待 Lightroom 批次 {batch_id} 超时；已完成 {batch_completed}/{batch_total}，当前状态 {counts}。"
            )
        time.sleep(min(poll_interval, remaining))
        status = status_reader(data_dir, batch_id)


def apply_results_in_lightroom(
    results_path: Path | str,
    data_dir: Path | str,
    batch_id: str,
    *,
    lightroom_exe: Path | str | None = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    batch_timeout: float = DEFAULT_BATCH_TIMEOUT,
) -> dict[str, Any]:
    """Run the confirmed edit plan through Lightroom without editing RAW bytes."""

    phase_start("launch", "准备 Lightroom", 3)
    tasks = load_lightroom_tasks(results_path)
    raw_before = {str(Path(task.photo_path)): full_fingerprint(Path(task.photo_path)) for task in tasks}
    config: dict[str, Any] = {}
    executable: Path | None = None
    launched = False
    batch: Mapping[str, Any] = {}
    result: dict[str, Any] | None = None
    processing_error: Exception | None = None
    try:
        config = write_lightroom_plugin_config(data_dir)
        executable = resolve_lightroom_executable(lightroom_exe)
        emit_progress("launch", "准备 Lightroom", 1, 3)

        current = read_lightroom_bridge_status(data_dir)
        heartbeat = current.get("heartbeat") if isinstance(current, Mapping) else None
        online = isinstance(heartbeat, Mapping) and heartbeat.get("state") == "online"
        if not online:
            start_lightroom(executable)
            launched = True
            emit_progress("launch", "启动 Lightroom", 2, 3)
            wait_for_lightroom_online(data_dir, startup_timeout)
        phase_end("launch", "Lightroom 已连接", 3)

        phase_start("queue", "提交处理队列", len(tasks), unit="张")
        batch = create_lightroom_batch(data_dir, tasks, batch_id=batch_id)
        phase_end("queue", "提交处理队列", len(tasks), unit="张")
        result = wait_for_lightroom_batch(data_dir, batch_id, batch_timeout)
    except Exception as exc:
        processing_error = exc
    finally:
        # Verify every source even when the bridge reports failure or
        # cancellation; a failed job is not evidence that RAW bytes stayed put.
        phase_start("finalize", "确认 RAW 未改变", len(tasks), unit="张")
        changed: list[str] = []
        for position, task in enumerate(tasks, start=1):
            photo_path = Path(task.photo_path)
            if full_fingerprint(photo_path) != raw_before[str(photo_path)]:
                changed.append(str(photo_path))
            emit_progress("finalize", "确认 RAW 未改变", position, len(tasks), unit="张")
        if changed:
            raise LightroomApplyError(f"检测到 RAW 文件在处理期间发生变化：{changed[0]}") from processing_error
        phase_end("finalize", "确认 RAW 未改变", len(tasks), unit="张")
    if processing_error is not None:
        raise processing_error
    assert result is not None and executable is not None
    return {
        **result,
        "results_path": str(Path(results_path).expanduser().resolve()),
        "bridge_root": config["root"],
        "lightroom_executable": str(executable),
        "lightroom_launched": launched,
        "published_count": int(batch.get("task_count", len(tasks))),
    }
