"""Machine-independent ``.photoai-settings`` import and export.

The transfer file is intentionally a preference document, not a backup.  It
never contains projects, paths, models, style files, Lightroom configuration,
hardware state, credentials, or background-task records.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .content_root import (
    SETTINGS_EXPORT_SCHEMA_VERSION,
    resolve_resource_path,
    sanitize_settings_export,
)
from .util import read_json, write_json

MAX_SETTINGS_TRANSFER_BYTES = 256 * 1024
SETTINGS_TRANSFER_MEDIA_TYPE = "application/vnd.photoai.settings+json"
SETTINGS_TRANSFER_FILENAME = "PhotoAI.photoai-settings"

_PROFILE_IDS = frozenset({"8gb", "16gb"})
_STYLE_RESOURCE_ID = re.compile(r"^(?:xmp|lut)-[A-Za-z0-9][A-Za-z0-9_-]{0,159}$")
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "ui",
        "ui_preferences",
        "workflow_defaults",
        "model_profile_preference",
        "style_sources",
        "style_library",
        "export_defaults",
    }
)


class SettingsTransferError(ValueError):
    """The settings document is invalid or unsafe to apply."""


def _defaults() -> dict[str, Any]:
    return {
        "schema_version": SETTINGS_EXPORT_SCHEMA_VERSION,
        "ui": {"theme": "dark"},
        "workflow_defaults": {"retain_ratio": 0.30, "mode": "deep"},
        "model_profile_preference": None,
        "style_sources": {
            "include_lightroom_presets": True,
            "include_user_uploads": True,
            "hidden_resource_ids": [],
        },
        "export_defaults": {
            "xmp": True,
            "jpeg": False,
            "jpeg_settings": {
                "color_space": "sRGB",
                "size": "original",
                "quality": 90,
                "sharpening": "screen_standard",
                "collision": "suffix",
            },
        },
    }


def preferences_path(data_dir: Path) -> Path:
    root = Path(data_dir).resolve()
    return resolve_resource_path(root, "settings/preferences.json")


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SettingsTransferError(f"{field} 必须是对象。")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise SettingsTransferError(f"{field} 必须是布尔值。")
    return value


def _normal_ui(value: Any, fallback: Mapping[str, Any]) -> dict[str, Any]:
    source = _object(value, "ui")
    theme = source.get("theme", fallback.get("theme", "dark"))
    if theme != "dark":
        raise SettingsTransferError("首版界面主题仅支持 dark。")
    return {"theme": theme}


def _normal_workflow(value: Any, fallback: Mapping[str, Any]) -> dict[str, Any]:
    source = _object(value, "workflow_defaults")
    ratio = source.get("retain_ratio", fallback.get("retain_ratio", 0.30))
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        raise SettingsTransferError("workflow_defaults.retain_ratio 必须是数字。")
    ratio = float(ratio)
    if not math.isfinite(ratio) or not 0.05 <= ratio <= 1.0:
        raise SettingsTransferError("保留比例必须在 0.05 到 1.0 之间。")
    mode = source.get("mode", fallback.get("mode", "deep"))
    if mode not in {"deep", "fast"}:
        raise SettingsTransferError("评分方式仅支持 deep 或 fast。")
    return {"retain_ratio": round(ratio, 4), "mode": mode}


def _normal_profile(value: Any) -> str | None:
    if value is None:
        return None
    if value not in _PROFILE_IDS:
        raise SettingsTransferError("模型偏好仅支持 8gb 或 16gb。")
    return str(value)


def _normal_styles(value: Any, fallback: Mapping[str, Any]) -> dict[str, Any]:
    source = _object(value, "style_sources")
    lightroom = source.get(
        "include_lightroom_presets",
        fallback.get("include_lightroom_presets", True),
    )
    user = source.get(
        "include_user_uploads", fallback.get("include_user_uploads", True)
    )
    hidden = source.get(
        "hidden_resource_ids", fallback.get("hidden_resource_ids", [])
    )
    if not isinstance(hidden, list):
        raise SettingsTransferError("style_sources.hidden_resource_ids 必须是数组。")
    if len(hidden) > 10_000:
        raise SettingsTransferError("单项停用记录过多。")
    normalized_hidden: set[str] = set()
    for item in hidden:
        if not isinstance(item, str) or not _STYLE_RESOURCE_ID.fullmatch(item):
            raise SettingsTransferError("风格资源标识格式无效。")
        normalized_hidden.add(item)
    return {
        "include_lightroom_presets": _boolean(
            lightroom, "style_sources.include_lightroom_presets"
        ),
        "include_user_uploads": _boolean(
            user, "style_sources.include_user_uploads"
        ),
        "hidden_resource_ids": sorted(normalized_hidden),
    }


def _normal_export(value: Any, fallback: Mapping[str, Any]) -> dict[str, Any]:
    source = _object(value, "export_defaults")
    fallback_jpeg = _object(fallback.get("jpeg_settings", {}), "jpeg_settings")
    jpeg_source = source.get("jpeg_settings", fallback_jpeg)
    jpeg_source = _object(jpeg_source, "export_defaults.jpeg_settings")
    xmp = _boolean(source.get("xmp", fallback.get("xmp", True)), "export_defaults.xmp")
    jpeg = _boolean(
        source.get("jpeg", fallback.get("jpeg", False)), "export_defaults.jpeg"
    )
    if not xmp and not jpeg:
        raise SettingsTransferError("导出默认值至少需要启用 XMP 或 JPEG。")
    quality = jpeg_source.get("quality", fallback_jpeg.get("quality", 90))
    if isinstance(quality, bool) or quality != 90:
        raise SettingsTransferError("首版 JPEG 质量固定为 90。")
    color_space = jpeg_source.get(
        "color_space", fallback_jpeg.get("color_space", "sRGB")
    )
    size = jpeg_source.get("size", fallback_jpeg.get("size", "original"))
    sharpening = jpeg_source.get(
        "sharpening", fallback_jpeg.get("sharpening", "screen_standard")
    )
    collision = jpeg_source.get(
        "collision", fallback_jpeg.get("collision", "suffix")
    )
    if color_space != "sRGB" or size != "original":
        raise SettingsTransferError("首版仅支持 sRGB、原尺寸 JPEG 默认值。")
    if sharpening != "screen_standard":
        raise SettingsTransferError("首版 JPEG 锐化固定为 screen_standard。")
    if collision != "suffix":
        raise SettingsTransferError("首版文件冲突策略固定为 suffix。")
    return {
        "xmp": xmp,
        "jpeg": jpeg,
        "jpeg_settings": {
            "color_space": color_space,
            "size": size,
            "quality": quality,
            "sharpening": sharpening,
            "collision": collision,
        },
    }


def _payload_size(payload: Mapping[str, Any]) -> int:
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SettingsTransferError("设置文件必须是有效的 JSON 数据。") from exc
    if len(encoded) > MAX_SETTINGS_TRANSFER_BYTES:
        raise SettingsTransferError("设置文件不能超过 256 KB。")
    return len(encoded)


def normalize_settings_payload(
    payload: Mapping[str, Any],
    *,
    current: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate and merge a transfer file into a canonical safe document."""

    if not isinstance(payload, Mapping):
        raise SettingsTransferError("设置文件顶层必须是对象。")
    _payload_size(payload)
    if payload.get("schema_version") != SETTINGS_EXPORT_SCHEMA_VERSION:
        raise SettingsTransferError("设置文件版本不受支持。")

    ignored = sorted(str(key) for key in payload if key not in _ALLOWED_TOP_LEVEL)
    sanitized = sanitize_settings_export(payload)
    if not any(key in sanitized for key in _ALLOWED_TOP_LEVEL - {"schema_version"}):
        raise SettingsTransferError("设置文件中没有可导入的偏好。")

    base = _defaults()
    if current is not None:
        current_clean = sanitize_settings_export(current)
        base["ui"] = _normal_ui(current_clean.get("ui", base["ui"]), base["ui"])
        base["workflow_defaults"] = _normal_workflow(
            current_clean.get("workflow_defaults", base["workflow_defaults"]),
            base["workflow_defaults"],
        )
        base["model_profile_preference"] = _normal_profile(
            current_clean.get("model_profile_preference")
        )
        current_styles = current_clean.get("style_sources") or current_clean.get(
            "style_library"
        )
        if current_styles is not None:
            base["style_sources"] = _normal_styles(
                current_styles, base["style_sources"]
            )
        base["export_defaults"] = _normal_export(
            current_clean.get("export_defaults", base["export_defaults"]),
            base["export_defaults"],
        )

    ui_value = sanitized.get("ui", sanitized.get("ui_preferences"))
    workflow_value = sanitized.get("workflow_defaults")
    profile_present = "model_profile_preference" in sanitized
    style_value = sanitized.get("style_sources", sanitized.get("style_library"))
    export_value = sanitized.get("export_defaults")
    result = {
        "schema_version": SETTINGS_EXPORT_SCHEMA_VERSION,
        "ui": _normal_ui(ui_value, base["ui"])
        if ui_value is not None
        else base["ui"],
        "workflow_defaults": _normal_workflow(
            workflow_value, base["workflow_defaults"]
        )
        if workflow_value is not None
        else base["workflow_defaults"],
        "model_profile_preference": _normal_profile(
            sanitized.get("model_profile_preference")
        )
        if profile_present
        else base["model_profile_preference"],
        "style_sources": _normal_styles(style_value, base["style_sources"])
        if style_value is not None
        else base["style_sources"],
        "export_defaults": _normal_export(export_value, base["export_defaults"])
        if export_value is not None
        else base["export_defaults"],
    }
    _payload_size(result)
    return result, ignored


def read_local_preferences(data_dir: Path) -> dict[str, Any]:
    """Read the path-free local preferences; missing state returns defaults."""

    path = preferences_path(data_dir)
    if not path.is_file():
        return _defaults()
    try:
        payload = read_json(path)
    except (OSError, TypeError, ValueError) as exc:
        raise SettingsTransferError("本机偏好文件已损坏。") from exc
    if not isinstance(payload, dict):
        raise SettingsTransferError("本机偏好文件格式无效。")
    transferable = {
        "schema_version": SETTINGS_EXPORT_SCHEMA_VERSION,
        "ui": payload.get("ui", _defaults()["ui"]),
        "workflow_defaults": payload.get(
            "workflow_defaults", _defaults()["workflow_defaults"]
        ),
        "model_profile_preference": payload.get("model_profile_preference"),
        "export_defaults": payload.get(
            "export_defaults", _defaults()["export_defaults"]
        ),
    }
    normalized, _ = normalize_settings_payload(
        {**transferable, "style_sources": _defaults()["style_sources"]}
    )
    return normalized


def write_local_preferences(data_dir: Path, settings: Mapping[str, Any]) -> Path:
    normalized, _ = normalize_settings_payload(settings)
    stored = {
        "schema_version": SETTINGS_EXPORT_SCHEMA_VERSION,
        "ui": normalized["ui"],
        "workflow_defaults": normalized["workflow_defaults"],
        "model_profile_preference": normalized["model_profile_preference"],
        "export_defaults": normalized["export_defaults"],
        "updated_utc": datetime.now(UTC).isoformat(),
    }
    path = preferences_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, stored)
    return path


def build_settings_export(
    data_dir: Path,
    *,
    style_settings: Mapping[str, Any],
    active_profile: str | None = None,
) -> dict[str, Any]:
    """Collect current preferences and externally owned style-source switches."""

    local = read_local_preferences(data_dir)
    profile = local.get("model_profile_preference") or active_profile
    payload = {
        "schema_version": SETTINGS_EXPORT_SCHEMA_VERSION,
        "ui": local["ui"],
        "workflow_defaults": local["workflow_defaults"],
        "model_profile_preference": profile if profile in _PROFILE_IDS else None,
        "style_sources": {
            "include_lightroom_presets": bool(
                style_settings.get("include_lightroom_presets", True)
            ),
            "include_user_uploads": bool(
                style_settings.get("include_user_uploads", True)
            ),
            "hidden_resource_ids": list(
                style_settings.get("hidden_resource_ids", [])
            ),
        },
        "export_defaults": local["export_defaults"],
    }
    normalized, _ = normalize_settings_payload(payload)
    # Keep this second boundary explicit: future preference fields do not enter
    # transfer files merely because they were added to the local state file.
    return sanitize_settings_export(normalized)
