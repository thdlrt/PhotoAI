from __future__ import annotations

import hashlib
import importlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import read_json, write_json

SMART_CROP_VERSION = "semantic-crop-v2"


class _LazyModule:
    def __init__(self, name: str) -> None:
        self.name = name
        self.module: Any | None = None

    def __getattr__(self, attribute: str) -> Any:
        if self.module is None:
            self.module = importlib.import_module(self.name)
        return getattr(self.module, attribute)


np = _LazyModule("numpy")
pyvips = _LazyModule("pyvips")
Image = _LazyModule("PIL.Image")
ImageEnhance = _LazyModule("PIL.ImageEnhance")


def smart_crop_candidates(*args: Any, **kwargs: Any) -> Any:
    implementation = importlib.import_module(
        ".smart_crop", package=__package__
    ).smart_crop_candidates
    return implementation(*args, **kwargs)

DEVELOP_SCHEMA_VERSION = 5
LEGACY_DEVELOP_SCHEMA_VERSIONS = {1, 2, 3, 4}
DevelopProgress = Callable[[dict[str, Any]], None]
DEVELOP_PROGRESS_PHASES = (
    ("subjects", "识别主体"),
    ("masks", "主体蒙版"),
    ("regions", "风光分区"),
    ("scene", "理解构图"),
    ("candidates", "生成候选"),
    ("rank", "AI 复评"),
    ("preview", "生成预览"),
)

STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "lightroom": {
        "label": "Lightroom 自动",
        "description": "不叠加自制风格；由 Lightroom Auto 完成基础颜色与明暗",
        "adjustments": {},
        "rgb": (1.0, 1.0, 1.0),
        "saturation": 1.0,
        "contrast": 1.0,
    }
}

# Kept only so an old materialized XMP snapshot remains readable.  New plans
# never expose or select these former hand-tuned looks.
LEGACY_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "natural": {
        "label": "自然",
        "description": "保持现场色彩，只做轻微通透处理",
        "adjustments": {"Vibrance": 6, "Clarity2012": 2},
        "rgb": (1.0, 1.0, 1.0),
        "saturation": 1.02,
        "contrast": 1.01,
    },
    "golden": {
        "label": "暖色时刻",
        "description": "保留高光暖意，阴影不过度偏黄",
        "adjustments": {
            "Vibrance": 10,
            "SplitToningHighlightHue": 43,
            "SplitToningHighlightSaturation": 12,
            "SplitToningShadowHue": 220,
            "SplitToningShadowSaturation": 3,
            "SplitToningBalance": 18,
        },
        "rgb": (1.055, 1.012, 0.955),
        "saturation": 1.06,
        "contrast": 1.02,
    },
    "blue_hour": {
        "label": "克制蓝调",
        "description": "强化蓝调氛围，同时保护中性色",
        "adjustments": {
            "Vibrance": 7,
            "SplitToningHighlightHue": 205,
            "SplitToningHighlightSaturation": 4,
            "SplitToningShadowHue": 225,
            "SplitToningShadowSaturation": 10,
            "SplitToningBalance": -18,
        },
        "rgb": (0.955, 1.005, 1.065),
        "saturation": 1.03,
        "contrast": 1.025,
    },
    "forest": {
        "label": "森林低饱和",
        "description": "压低杂乱绿色，保留叶片层次",
        "adjustments": {
            "Vibrance": 5,
            "Saturation": -4,
            "HueAdjustmentGreen": -4,
            "SaturationAdjustmentGreen": -12,
            "LuminanceAdjustmentGreen": -3,
        },
        "rgb": (0.985, 1.018, 0.975),
        "saturation": 0.94,
        "contrast": 1.025,
    },
    "soft": {
        "label": "柔和层次",
        "description": "降低硬反差，适合雾景和高动态范围",
        "adjustments": {
            "Contrast2012": -8,
            "Highlights2012": -8,
            "Shadows2012": 8,
            "Clarity2012": -3,
        },
        "rgb": (1.0, 1.0, 1.0),
        "saturation": 0.99,
        "contrast": 0.92,
    },
    "clear": {
        "label": "清透风光",
        "description": "增加局部反差和空气感，强度保持克制",
        "adjustments": {
            "Contrast2012": 5,
            "Clarity2012": 7,
            "Dehaze": 6,
            "Vibrance": 8,
        },
        "rgb": (1.0, 1.0, 1.0),
        "saturation": 1.04,
        "contrast": 1.07,
    },
}


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _legacy_plan_id(plan: dict[str, Any]) -> str:
    """Return a stable identity for a plan created before plan_id existed."""

    identity = {
        "run_id": plan.get("run_id"),
        "created_at": plan.get("created_at"),
        "source_review_revision": plan.get("source_review_revision"),
        "schema_version": plan.get("schema_version"),
        "items": [
            {"index": item.get("index"), "path": item.get("path")}
            for item in plan.get("items", [])
            if isinstance(item, dict)
        ],
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"legacy-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _ensure_plan_defaults(plan: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional workflow fields while keeping legacy plans readable."""

    original_schema = int(plan.get("schema_version", 1) or 1)
    missing_legacy_identity = (
        not isinstance(plan.get("plan_id"), str)
        or not str(plan.get("plan_id", "")).strip()
    )
    missing_legacy_color_flag = "color_enabled" not in plan
    if original_schema in LEGACY_DEVELOP_SCHEMA_VERSIONS:
        plan.setdefault("migrated_from_schema_version", original_schema)
    if missing_legacy_identity:
        plan["plan_id"] = _legacy_plan_id(plan)
    plan["crop_skipped"] = bool(plan.get("crop_skipped", False))
    plan["color_enabled"] = bool(plan.get("color_enabled", True))
    items = [item for item in plan.get("items", []) if isinstance(item, dict)]
    confirmed_count = sum(
        bool(item.get("crop_confirmed", item.get("confirmed", False))) for item in items
    )
    crop_complete = bool(items) and confirmed_count == len(items)

    crop = plan.get("crop") if isinstance(plan.get("crop"), dict) else {}
    crop_status = str(crop.get("status", ""))
    if crop_status not in {"pending", "confirmed", "skipped"}:
        crop_status = (
            "skipped"
            if plan["crop_skipped"]
            else "confirmed"
            if crop_complete
            else "pending"
        )
    plan["crop"] = {**crop, "status": crop_status}
    plan["crop_skipped"] = crop_status == "skipped"

    basic = plan.get("basic_color") if isinstance(plan.get("basic_color"), dict) else {}
    basic_status = str(basic.get("status", ""))
    if missing_legacy_identity and missing_legacy_color_flag:
        basic_status = "enabled"
    elif basic_status not in {"pending", "enabled", "skipped"}:
        basic_status = "enabled" if plan["color_enabled"] else "skipped"
    plan["basic_color"] = {**basic, "status": basic_status}
    plan["color_enabled"] = basic_status != "skipped"

    creative = (
        plan.get("creative_style")
        if isinstance(plan.get("creative_style"), dict)
        else {}
    )
    creative_status = str(creative.get("status", ""))
    if creative_status not in {"pending", "confirmed", "skipped"}:
        # A historical plan never opted in to creative presets.  Migrating it
        # as skipped prevents a newly installed style library from changing an
        # already reviewed project.
        creative_status = (
            "skipped"
            if original_schema in LEGACY_DEVELOP_SCHEMA_VERSIONS
            else "pending"
        )
    groups = creative.get("groups") if isinstance(creative.get("groups"), dict) else {}
    scope = str(creative.get("scope") or "group")
    if scope not in {"global", "group"}:
        scope = "group"
    global_selection = (
        creative.get("global_selection")
        if isinstance(creative.get("global_selection"), dict)
        else None
    )
    plan["creative_style"] = {
        **creative,
        "status": creative_status,
        "scope": scope,
        "groups": groups,
        "global_selection": global_selection,
    }
    color_mode = str(plan.get("color_mode", ""))
    if missing_legacy_identity and missing_legacy_color_flag:
        color_mode = "auto"
    if color_mode not in {"pending", "skip", "auto", "style"}:
        color_mode = (
            "style"
            if creative_status == "confirmed" and groups
            else "auto"
            if basic_status == "enabled"
            else "skip"
            if basic_status == "skipped"
            else "pending"
        )
    plan["color_mode"] = color_mode
    plan["schema_version"] = DEVELOP_SCHEMA_VERSION
    for item in items:
        item["crop_confirmed"] = bool(
            item.get("crop_confirmed", item.get("confirmed", False))
        )
        # Keep the old field as a read/write compatibility alias for existing
        # clients and materialized result snapshots.
        item["confirmed"] = item["crop_confirmed"]
    return plan


def _bump_plan(plan: dict[str, Any], run_dir: Path) -> None:
    _ensure_plan_defaults(plan)
    plan["revision"] = int(plan.get("revision", 0)) + 1
    plan["updated_at"] = _utc_now()
    write_json(run_dir / "develop.json", plan)


def _vips_image(image: Image.Image) -> pyvips.Image:
    rgb = np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8))
    height, width = rgb.shape[:2]
    return pyvips.Image.new_from_memory(rgb.tobytes(), width, height, 3, "uchar")


def _target_crop_size(
    width: int, height: int, target_aspect: float, coverage: float
) -> tuple[int, int]:
    aspect = width / max(1, height)
    if aspect >= target_aspect:
        crop_height = max(1, min(height, round(height * coverage)))
        crop_width = max(1, min(width, round(crop_height * target_aspect)))
    else:
        crop_width = max(1, min(width, round(width * coverage)))
        crop_height = max(1, min(height, round(crop_width / target_aspect)))
    return crop_width, crop_height


def _smartcrop_bounds(
    source: pyvips.Image, width: int, height: int, interesting: str
) -> dict[str, float]:
    cropped = source.smartcrop(width, height, interesting=interesting)
    left_px = max(0, min(source.width - width, -int(cropped.get("xoffset"))))
    top_px = max(0, min(source.height - height, -int(cropped.get("yoffset"))))
    return {
        "left": round(left_px / source.width, 6),
        "top": round(top_px / source.height, 6),
        "right": round((left_px + width) / source.width, 6),
        "bottom": round((top_px + height) / source.height, 6),
    }


def _crop_agreement(first: dict[str, float], second: dict[str, float]) -> float:
    left = max(first["left"], second["left"])
    top = max(first["top"], second["top"])
    right = min(first["right"], second["right"])
    bottom = min(first["bottom"], second["bottom"])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    area_first = (first["right"] - first["left"]) * (first["bottom"] - first["top"])
    area_second = (second["right"] - second["left"]) * (
        second["bottom"] - second["top"]
    )
    return intersection / max(area_first + area_second - intersection, 1e-8)


def crop_candidates(
    image: Image.Image,
) -> tuple[list[dict[str, Any]], str, float, float]:
    source = _vips_image(image)
    aspect = image.width / max(1, image.height)
    original = {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
    specs: list[tuple[str, str, float, float]] = [
        ("original", "原图", aspect, 1.0),
        ("balanced", "推荐", aspect, 0.92),
        ("tight", "更紧", aspect, 0.82),
    ]
    specs.append(
        (
            "wide",
            "16:9" if aspect >= 1.0 else "4:5",
            16 / 9 if aspect >= 1.0 else 4 / 5,
            0.96,
        )
    )
    candidates: list[dict[str, Any]] = []
    for crop_id, label, target, coverage in specs:
        if crop_id == "original":
            bounds = original
            confidence = 1.0
        else:
            crop_width, crop_height = _target_crop_size(
                image.width, image.height, target, coverage
            )
            bounds = _smartcrop_bounds(source, crop_width, crop_height, "attention")
            entropy_bounds = _smartcrop_bounds(
                source, crop_width, crop_height, "entropy"
            )
            confidence = _crop_agreement(bounds, entropy_bounds)
        candidates.append(
            {
                "id": crop_id,
                "label": label,
                "bounds": bounds,
                "engine": "libvips-attention" if crop_id != "original" else "original",
                "confidence": round(float(confidence), 4),
                "score": round(float(confidence), 4),
            }
        )
    balanced = next(item for item in candidates if item["id"] == "balanced")
    selected = "balanced" if balanced["confidence"] >= 0.78 else "original"
    # Creative crop remains reviewable. Technical straightening/perspective is
    # delegated to Lightroom Upright instead of another local heuristic.
    return candidates, selected, 0.0, 0.0


def base_adjustments(technical: dict[str, Any], image: Image.Image) -> dict[str, Any]:
    del technical, image
    return {
        "WhiteBalance": "Auto",
        "AutoTone": "True",
        "AutoLateralCA": 1,
        "LensProfileEnable": 1,
        "EnableTransform": 1,
        "PerspectiveUpright": 1,
        "ConstrainToWarp": 1,
    }


def style_options(
    image: Image.Image, technical: dict[str, Any]
) -> list[dict[str, Any]]:
    del image, technical
    preset = STYLE_PRESETS["lightroom"]
    return [
        {
            "id": "lightroom",
            "label": preset["label"],
            "description": preset["description"],
        }
    ]


def _scaled_style_settings(style_id: str, strength: int) -> dict[str, Any]:
    presets = {**LEGACY_STYLE_PRESETS, **STYLE_PRESETS}
    if style_id not in presets:
        raise ValueError(f"未知调色风格：{style_id}")
    amount = _clamp(float(strength), 0.0, 100.0) / 100.0
    output: dict[str, Any] = {}
    hue_fields = {
        "SplitToningHighlightHue",
        "SplitToningShadowHue",
    }
    for key, value in presets[style_id]["adjustments"].items():
        if key in hue_fields:
            output[key] = int(value)
        elif isinstance(value, int):
            output[key] = round(value * amount)
        else:
            output[key] = round(float(value) * amount, 4)
    return output


def xmp_settings_for_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    base = dict(recipe.get("base", {}))
    style = _scaled_style_settings(
        str(recipe.get("style_id", "lightroom")), int(recipe.get("style_strength", 60))
    )
    additive = {
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
    }
    for key, value in style.items():
        if key in additive:
            base[key] = round(
                _clamp(float(base.get(key, 0)) + float(value), -100.0, 100.0)
            )
        else:
            base[key] = value
    candidates = {item["id"]: item for item in recipe.get("crop_candidates", [])}
    crop = candidates.get(str(recipe.get("crop_id"))) or candidates.get("original")
    if crop:
        bounds = crop["bounds"]
        base.update(
            {
                "CropLeft": round(float(bounds["left"]), 6),
                "CropTop": round(float(bounds["top"]), 6),
                "CropRight": round(float(bounds["right"]), 6),
                "CropBottom": round(float(bounds["bottom"]), 6),
                "CropAngle": round(float(recipe.get("angle", 0.0)), 2),
                "HasCrop": "True",
            }
        )
    return base


def _apply_preview_recipe(image: Image.Image, recipe: dict[str, Any]) -> Image.Image:
    base = xmp_settings_for_recipe(recipe)
    result = image.convert("RGB")
    angle = float(base.get("CropAngle", 0.0))
    if angle:
        result = result.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)
    left = int(float(base.get("CropLeft", 0.0)) * result.width)
    top = int(float(base.get("CropTop", 0.0)) * result.height)
    right = int(float(base.get("CropRight", 1.0)) * result.width)
    bottom = int(float(base.get("CropBottom", 1.0)) * result.height)
    result = result.crop(
        (
            max(0, left),
            max(0, top),
            min(result.width, right),
            min(result.height, bottom),
        )
    )
    exposure = float(base.get("Exposure2012", 0.0))
    result = ImageEnhance.Brightness(result).enhance(2.0**exposure)
    contrast = 1.0 + float(base.get("Contrast2012", 0.0)) / 120.0
    result = ImageEnhance.Contrast(result).enhance(max(0.65, contrast))
    style_id = str(recipe.get("style_id", "lightroom"))
    preset = (
        STYLE_PRESETS.get(style_id)
        or LEGACY_STYLE_PRESETS.get(style_id)
        or STYLE_PRESETS["lightroom"]
    )
    amount = _clamp(float(recipe.get("style_strength", 60)), 0.0, 100.0) / 100.0
    array = np.asarray(result, dtype=np.float32) / 255.0
    rgb_scale = np.asarray(preset["rgb"], dtype=np.float32)
    array *= 1.0 + (rgb_scale - 1.0) * amount
    array = np.clip(array, 0.0, 1.0)
    result = Image.fromarray(np.round(array * 255.0).astype(np.uint8), "RGB")
    result = ImageEnhance.Color(result).enhance(
        1.0 + (float(preset["saturation"]) - 1.0) * amount
    )
    result = ImageEnhance.Contrast(result).enhance(
        1.0 + (float(preset["contrast"]) - 1.0) * amount
    )
    return result


def _render_preview(run_dir: Path, item: dict[str, Any], source_preview: Path) -> str:
    crop = next(
        (
            candidate
            for candidate in item.get("crop_candidates", [])
            if candidate.get("id") == item.get("crop_id")
        ),
        None,
    )
    fingerprint = hashlib.sha256(
        repr(
            (
                SMART_CROP_VERSION,
                item.get("crop_id"),
                crop.get("bounds") if isinstance(crop, dict) else None,
                item.get("style_id"),
                item.get("style_strength"),
                item.get("angle"),
                item.get("base"),
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    output_dir = run_dir / "develop-previews"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{int(item['index']):05d}-{fingerprint}.jpg"
    if not target.is_file():
        with Image.open(source_preview) as source:
            rendered = _apply_preview_recipe(source, item)
            rendered.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
            rendered.save(target, format="JPEG", quality=90, optimize=True)
    return str(target)


def create_develop_plan(
    payload: dict[str, Any],
    run_dir: Path,
    review_revision: int,
    progress: DevelopProgress | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    eligible = [
        (index, result)
        for index, result in enumerate(payload.get("results", []))
        if not result.get("excluded") and int(result.get("rating", 0)) >= 3
    ]
    if not eligible:
        raise ValueError("当前没有 3 星以上照片可进行裁剪调色。")
    total = len(eligible)
    phase_indexes = {
        key: index for index, (key, _label) in enumerate(DEVELOP_PROGRESS_PHASES)
    }

    for position, (index, result) in enumerate(eligible):
        filename = Path(str(result.get("path", ""))).name

        def report(
            phase: str,
            label: str,
            _position: int = position,
            _filename: str = filename,
        ) -> None:
            if progress is None:
                return
            phase_index = phase_indexes.get(phase, len(DEVELOP_PROGRESS_PHASES) - 1)
            if phase == "cache":
                phase_index = len(DEVELOP_PROGRESS_PHASES) - 2
            overall = (
                (_position + phase_index / len(DEVELOP_PROGRESS_PHASES)) / total
            ) * 100.0
            progress(
                {
                    "status": "running",
                    "phase": phase,
                    "stage_label": label,
                    "current": _position + 1,
                    "completed": _position,
                    "total": total,
                    "filename": _filename,
                    "overall_percent": round(min(99.5, overall), 1),
                    "nodes": [
                        {
                            "key": key,
                            "label": node_label,
                            "status": "completed"
                            if node_index < phase_index
                            else "active"
                            if node_index == phase_index
                            else "pending",
                        }
                        for node_index, (key, node_label) in enumerate(
                            DEVELOP_PROGRESS_PHASES
                        )
                    ],
                }
            )

        preview_path = Path(str(result.get("preview", "")))
        if not preview_path.is_file():
            raise FileNotFoundError(
                f"缺少照片预览：{Path(str(result.get('path', ''))).name}"
            )
        with Image.open(preview_path) as source:
            image = source.convert("RGB")
            crops, crop_id, smart_crop = smart_crop_candidates(
                image, run_dir, progress=report
            )
            angle = 0.0
            horizon_confidence = float(smart_crop.get("horizon_confidence", 0.0))
            technical = dict(result.get("technical", {}))
            base = base_adjustments(technical, image)
            styles = style_options(image, technical)
        item = {
            "index": index,
            "path": str(result.get("path", "")),
            "filename": Path(str(result.get("path", ""))).name,
            "group_id": int(result.get("group_id", index + 1)),
            "rating": int(result.get("rating", 0)),
            "crop_candidates": crops,
            "crop_id": crop_id,
            "angle": angle,
            "horizon_confidence": horizon_confidence,
            "smart_crop": smart_crop,
            "base_engine": "lightroom-classic",
            "crop_engine": SMART_CROP_VERSION,
            "base": base,
            "style_options": styles,
            "style_id": styles[0]["id"],
            "style_strength": 60,
            "crop_confirmed": False,
            "confirmed": False,
        }
        report("preview", "生成构图预览")
        item["preview_path"] = _render_preview(run_dir, item, preview_path)
        items.append(item)
        if progress is not None:
            progress(
                {
                    "status": "running",
                    "phase": "preview",
                    "stage_label": "构图预览已完成",
                    "current": position + 1,
                    "completed": position + 1,
                    "total": total,
                    "filename": filename,
                    "overall_percent": round(((position + 1) / total) * 100.0, 1),
                    "nodes": [
                        {"key": key, "label": label, "status": "completed"}
                        for key, label in DEVELOP_PROGRESS_PHASES
                    ],
                }
            )
    now = _utc_now()
    plan = {
        "schema_version": DEVELOP_SCHEMA_VERSION,
        "plan_id": f"develop-{uuid.uuid4().hex}",
        "run_id": payload.get("run_id", run_dir.name),
        "source_review_revision": int(review_revision),
        "revision": 0,
        "crop_skipped": False,
        "color_enabled": True,
        "crop": {"status": "pending"},
        "basic_color": {"status": "pending"},
        "creative_style": {"status": "pending", "scope": "global", "groups": {}},
        "color_mode": "pending",
        "created_at": now,
        "updated_at": now,
        "items": items,
    }
    write_json(run_dir / "develop.json", plan)
    return plan


def load_develop_plan(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "develop.json"
    if not path.is_file():
        return None
    try:
        plan = read_json(path)
    except (OSError, ValueError):
        return None
    if plan.get("schema_version") not in LEGACY_DEVELOP_SCHEMA_VERSIONS | {
        DEVELOP_SCHEMA_VERSION
    } or not isinstance(plan.get("items"), list):
        return None
    return _ensure_plan_defaults(plan)


def develop_summary(
    plan: dict[str, Any] | None, review_revision: int
) -> dict[str, Any]:
    if not plan:
        return {
            "exists": False,
            "stale": False,
            "eligible_count": 0,
            "confirmed_count": 0,
            "revision": None,
            "plan_id": None,
            "crop_skipped": False,
            "color_enabled": True,
            "crop": {"status": "pending"},
            "basic_color": {"status": "pending"},
            "creative_style": {"status": "pending", "scope": "global", "groups": {}},
            "color_mode": "pending",
        }
    _ensure_plan_defaults(plan)
    items = plan.get("items", [])
    engine_outdated = (
        int(plan.get("migrated_from_schema_version", DEVELOP_SCHEMA_VERSION))
        != DEVELOP_SCHEMA_VERSION
    )
    stale = int(plan.get("source_review_revision", -1)) != int(review_revision)
    crop_confirmed_count = sum(
        bool(item.get("crop_confirmed", item.get("confirmed"))) for item in items
    )
    return {
        "exists": True,
        "stale": stale,
        "engine_outdated": engine_outdated,
        "eligible_count": len(items),
        "confirmed_count": crop_confirmed_count,
        "crop_confirmed_count": crop_confirmed_count,
        "revision": int(plan.get("revision", 0)),
        "plan_id": plan["plan_id"],
        "crop_skipped": bool(plan["crop_skipped"]),
        "color_enabled": bool(plan["color_enabled"]),
        "crop": dict(plan["crop"]),
        "basic_color": dict(plan["basic_color"]),
        "creative_style": dict(plan["creative_style"]),
        "color_mode": str(plan["color_mode"]),
        "updated_at": plan.get("updated_at"),
    }


def update_develop_item(
    plan: dict[str, Any],
    run_dir: Path,
    index: int,
    *,
    crop_id: str | None = None,
    style_id: str | None = None,
    style_strength: int | None = None,
    confirmed: bool | None = None,
) -> dict[str, Any]:
    item = next(
        (
            value
            for value in plan.get("items", [])
            if int(value.get("index", -1)) == index
        ),
        None,
    )
    if item is None:
        raise KeyError(index)
    if crop_id is not None:
        allowed = {value["id"] for value in item.get("crop_candidates", [])}
        if crop_id not in allowed:
            raise ValueError("裁剪方案不存在。")
        item["crop_id"] = crop_id
        plan["crop_skipped"] = False
        plan["crop"] = {"status": "pending"}
        item["crop_confirmed"] = False
        item["confirmed"] = False
    if style_id is not None:
        allowed_styles = {value["id"] for value in item.get("style_options", [])}
        if style_id not in allowed_styles or style_id not in STYLE_PRESETS:
            raise ValueError("调色风格不存在。")
        item["style_id"] = style_id
    if style_strength is not None:
        item["style_strength"] = int(_clamp(style_strength, 0, 100))
    if confirmed is not None:
        item["crop_confirmed"] = bool(confirmed)
        item["confirmed"] = item["crop_confirmed"]
    payload_path = Path(str(item.get("path", "")))
    source_preview = None
    # The caller supplies the run's canonical results through preview_source.
    if item.get("source_preview"):
        source_preview = Path(str(item["source_preview"]))
    if source_preview is None or not source_preview.is_file():
        # Existing preview paths live in the shared cache and are discovered by
        # the web layer before this helper is called.
        source_preview = Path(str(item.get("_source_preview", "")))
    if not source_preview.is_file():
        raise FileNotFoundError(f"缺少照片预览：{payload_path.name}")
    item["preview_path"] = _render_preview(run_dir, item, source_preview)
    item.pop("_source_preview", None)
    item.pop("source_preview", None)
    crop_complete = bool(plan.get("items")) and all(
        bool(value.get("crop_confirmed", value.get("confirmed", False)))
        for value in plan.get("items", [])
    )
    if crop_complete:
        plan["crop"] = {"status": "confirmed"}
    elif str(plan.get("crop", {}).get("status")) != "skipped":
        plan["crop"] = {"status": "pending"}
    _bump_plan(plan, run_dir)
    return item


def confirm_all(plan: dict[str, Any], run_dir: Path) -> None:
    for item in plan.get("items", []):
        item["crop_confirmed"] = True
        item["confirmed"] = True
    plan["crop"] = {"status": "confirmed"}
    plan["crop_skipped"] = False
    _bump_plan(plan, run_dir)


def skip_all_crops(
    plan: dict[str, Any],
    run_dir: Path,
    source_previews: dict[int, Path] | None = None,
) -> None:
    """Select the original frame for every eligible photo and confirm the step."""

    for item in plan.get("items", []):
        candidates = {
            str(candidate.get("id"))
            for candidate in item.get("crop_candidates", [])
            if isinstance(candidate, dict)
        }
        if "original" not in candidates:
            raise ValueError(f"{item.get('filename', '照片')} 缺少原图构图方案。")
    for item in plan.get("items", []):
        item["crop_id"] = "original"
        item["crop_confirmed"] = True
        item["confirmed"] = True
        if source_previews is not None:
            index = int(item.get("index", -1))
            source_preview = source_previews.get(index)
            if source_preview is None or not source_preview.is_file():
                raise FileNotFoundError(f"缺少照片预览：{item.get('filename', index)}")
            item["preview_path"] = _render_preview(run_dir, item, source_preview)
    plan["crop_skipped"] = True
    plan["crop"] = {"status": "skipped"}
    _bump_plan(plan, run_dir)


def update_develop_options(
    plan: dict[str, Any], run_dir: Path, *, color_enabled: bool
) -> None:
    plan["color_enabled"] = bool(color_enabled)
    plan["basic_color"] = {"status": "enabled" if color_enabled else "skipped"}
    plan["creative_style"] = {"status": "skipped", "scope": "global", "groups": {}}
    plan["color_mode"] = "auto" if color_enabled else "skip"
    _bump_plan(plan, run_dir)


def update_color_mode(plan: dict[str, Any], run_dir: Path, *, mode: str) -> None:
    """Select the third workflow step without applying or exporting anything."""

    if mode not in {"skip", "auto", "style"}:
        raise ValueError("调色模式不存在。")
    if mode == "skip":
        plan["basic_color"] = {"status": "skipped"}
        plan["creative_style"] = {"status": "skipped", "scope": "global", "groups": {}}
        plan["color_enabled"] = False
    elif mode == "auto":
        plan["basic_color"] = {"status": "enabled"}
        plan["creative_style"] = {"status": "skipped", "scope": "global", "groups": {}}
        plan["color_enabled"] = True
    else:
        plan["basic_color"] = {"status": "enabled"}
        creative = (
            plan.get("creative_style")
            if isinstance(plan.get("creative_style"), dict)
            else {}
        )
        groups = (
            creative.get("groups") if isinstance(creative.get("groups"), dict) else {}
        )
        group_ids = {
            str(int(item.get("group_id", 0))) for item in plan.get("items", [])
        }
        selected_groups = {
            key: value
            for key, value in groups.items()
            if key in group_ids and isinstance(value, dict)
        }
        all_confirmed = bool(group_ids) and all(
            str(selected_groups.get(key, {}).get("status", "pending"))
            in {"confirmed", "skipped"}
            for key in group_ids
        )
        plan["creative_style"] = {
            **creative,
            "status": "confirmed" if all_confirmed else "pending",
            "groups": selected_groups,
        }
        plan["color_enabled"] = True
    plan["color_mode"] = mode
    _bump_plan(plan, run_dir)


def update_style_group(
    plan: dict[str, Any],
    run_dir: Path,
    group_id: int,
    *,
    preset_id: str | None,
    preset_hash: str | None = None,
    lut_id: str | None = None,
    lut_hash: str | None = None,
    amount: int = 100,
    status: str = "confirmed",
    manual_override: bool = True,
) -> dict[str, Any]:
    """Persist one group selection while keeping the recommendation evidence."""

    valid_group_ids = {int(item.get("group_id", -1)) for item in plan.get("items", [])}
    if group_id not in valid_group_ids:
        raise KeyError(group_id)
    if status not in {"pending", "confirmed", "skipped"}:
        raise ValueError("风格状态不存在。")
    if status != "skipped" and bool(preset_id) == bool(lut_id):
        raise ValueError(
            "请选择一个 Lightroom 外观或 .cube LUT，或将此组设为不套风格。"
        )
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or not 0 <= amount <= 200
    ):
        raise ValueError("风格强度必须是 0 到 200 的整数。")
    _ensure_plan_defaults(plan)
    creative = plan["creative_style"]
    groups = creative["groups"]
    previous = (
        groups.get(str(group_id), {})
        if isinstance(groups.get(str(group_id)), dict)
        else {}
    )
    candidate: dict[str, Any] | None = None
    resource_is_lut = bool(lut_id)
    if status != "skipped":
        selected_hash = lut_hash if resource_is_lut else preset_hash
        if not selected_hash:
            raise ValueError("请选择带版本信息的创意外观。")
        candidate = next(
            (
                value
                for value in previous.get("top3") or []
                if isinstance(value, dict)
                and str(value.get("lut_id" if resource_is_lut else "preset_id") or "")
                == str(lut_id if resource_is_lut else preset_id)
            ),
            None,
        )
        if (
            candidate is None
            or candidate.get("render_status") != "ready"
            or not candidate.get("preview_key")
        ):
            raise ValueError("请先为这个创意外观生成真实预览。")
        expected_hash = str(
            candidate.get("lut_hash" if resource_is_lut else "preset_hash") or ""
        )
        if not expected_hash or str(selected_hash) != expected_hash:
            if resource_is_lut:
                raise ValueError("LUT 版本已经变化，请重新生成预览。")
            raise ValueError("预设版本已经变化，请重新生成预览。")
        candidate_amount = candidate.get("strength", candidate.get("amount"))
        if (
            isinstance(candidate_amount, bool)
            or not isinstance(candidate_amount, (int, float))
            or int(candidate_amount) != candidate_amount
            or int(candidate_amount) != amount
        ):
            raise ValueError("所选强度与 Lightroom 真实预览不一致，请重新生成预览。")
        if (
            not resource_is_lut
            and not candidate.get("amount_supported")
            and amount != 100
        ):
            raise ValueError("这个预设不支持强度调整。")
    selected = {
        **previous,
        "group_id": group_id,
        "status": status,
        "preset_id": None if status == "skipped" or resource_is_lut else str(preset_id),
        "preset_hash": None
        if status == "skipped" or resource_is_lut
        else str(candidate.get("preset_hash")),
        "lut_id": None if status == "skipped" or not resource_is_lut else str(lut_id),
        "lut_hash": None
        if status == "skipped" or not resource_is_lut
        else str(candidate.get("lut_hash")),
        "amount": 0 if status == "skipped" else amount,
        "strength": amount if status != "skipped" and resource_is_lut else None,
        "manual_override": bool(manual_override),
        "updated_at": _utc_now(),
    }
    if candidate is not None:
        selected.update(
            preset_uuid=candidate.get("preset_uuid"),
            preset_scope=candidate.get("preset_scope"),
            look_kind=candidate.get("look_kind")
            or ("rendered_lut" if resource_is_lut else "develop_preset"),
            profile_name=candidate.get("profile_name"),
            profile_hash=candidate.get("profile_hash"),
            lut_id=str(lut_id) if resource_is_lut else candidate.get("lut_id"),
            lut_hash=str(lut_hash) if resource_is_lut else candidate.get("lut_hash"),
            xmp_compatible=False
            if resource_is_lut
            else bool(candidate.get("xmp_compatible", True)),
            amount_supported=True
            if resource_is_lut
            else bool(candidate.get("amount_supported")),
            amount_note=candidate.get("amount_note"),
            selected_preview_key=candidate.get("preview_key"),
            selected_preview_path=candidate.get("preview_path"),
        )
    else:
        selected.update(
            preset_uuid=None,
            preset_scope=None,
            look_kind=None,
            profile_name=None,
            profile_hash=None,
            lut_id=None,
            lut_hash=None,
            xmp_compatible=True,
            amount_supported=False,
            amount_note=None,
            selected_preview_key=None,
            selected_preview_path=None,
        )
    groups[str(group_id)] = selected
    creative["scope"] = "group"
    all_group_ids = {str(value) for value in valid_group_ids}
    creative["status"] = (
        "confirmed"
        if all(
            str(groups.get(key, {}).get("status", "pending"))
            in {"confirmed", "skipped"}
            for key in all_group_ids
        )
        else "pending"
    )
    plan["basic_color"] = {"status": "enabled"}
    plan["color_enabled"] = True
    plan["color_mode"] = "style"
    _bump_plan(plan, run_dir)
    return selected


def update_style_global(
    plan: dict[str, Any],
    run_dir: Path,
    *,
    preset_id: str | None,
    preset_hash: str | None = None,
    lut_id: str | None = None,
    lut_hash: str | None = None,
    amount: int = 100,
    status: str = "confirmed",
    manual_override: bool = True,
) -> dict[str, Any]:
    """Persist one project-wide choice without copying it to groups yet."""

    _ensure_plan_defaults(plan)
    creative = plan["creative_style"]
    previous = (
        dict(creative.get("global_selection"))
        if isinstance(creative.get("global_selection"), dict)
        else {}
    )
    if not previous:
        raise ValueError("当前工程还没有全局统一风格推荐。")
    if status not in {"pending", "confirmed", "skipped"}:
        raise ValueError("风格状态不存在。")
    if status != "skipped" and bool(preset_id) == bool(lut_id):
        raise ValueError("请选择一个 Lightroom 外观或 .cube LUT，或保持自然。")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or not 0 <= amount <= 200
    ):
        raise ValueError("风格强度必须是 0 到 200 的整数。")
    resource_is_lut = bool(lut_id)
    candidate: dict[str, Any] | None = None
    if status != "skipped":
        resource_id = lut_id if resource_is_lut else preset_id
        selected_hash = lut_hash if resource_is_lut else preset_hash
        if not selected_hash:
            raise ValueError("请选择带版本信息的创意外观。")
        candidate = next(
            (
                value
                for value in previous.get("top3") or []
                if isinstance(value, dict)
                and str(value.get("lut_id" if resource_is_lut else "preset_id") or "")
                == str(resource_id)
            ),
            None,
        )
        if (
            candidate is None
            or candidate.get("render_status") != "ready"
            or not candidate.get("preview_key")
        ):
            raise ValueError("请先为这个创意外观生成真实预览。")
        expected_hash = str(
            candidate.get("lut_hash" if resource_is_lut else "preset_hash") or ""
        )
        if expected_hash != str(selected_hash):
            raise ValueError("外观版本已经变化，请重新生成预览。")
        candidate_amount = candidate.get("strength", candidate.get("amount"))
        if (
            isinstance(candidate_amount, bool)
            or not isinstance(candidate_amount, (int, float))
            or int(candidate_amount) != candidate_amount
            or int(candidate_amount) != amount
        ):
            raise ValueError("所选强度与 Lightroom 真实预览不一致，请重新生成预览。")
        if (
            not resource_is_lut
            and not candidate.get("amount_supported")
            and amount != 100
        ):
            raise ValueError("这个预设不支持强度调整。")

    selected = {
        **previous,
        "scope": "global",
        "status": status,
        "preset_id": None if status == "skipped" or resource_is_lut else str(preset_id),
        "preset_hash": None
        if status == "skipped" or resource_is_lut
        else str(candidate.get("preset_hash")),
        "lut_id": None if status == "skipped" or not resource_is_lut else str(lut_id),
        "lut_hash": None
        if status == "skipped" or not resource_is_lut
        else str(candidate.get("lut_hash")),
        "amount": 0 if status == "skipped" else amount,
        "strength": amount if status != "skipped" and resource_is_lut else None,
        "manual_override": bool(manual_override),
        "updated_at": _utc_now(),
    }
    if candidate is not None:
        selected.update(
            preset_uuid=candidate.get("preset_uuid"),
            preset_scope=candidate.get("preset_scope"),
            look_kind=candidate.get("look_kind")
            or ("rendered_lut" if resource_is_lut else "develop_preset"),
            profile_name=candidate.get("profile_name"),
            profile_hash=candidate.get("profile_hash"),
            xmp_compatible=False
            if resource_is_lut
            else bool(candidate.get("xmp_compatible", True)),
            amount_supported=True
            if resource_is_lut
            else bool(candidate.get("amount_supported")),
            amount_note=candidate.get("amount_note"),
            selected_preview_key=candidate.get("preview_key"),
            selected_preview_path=candidate.get("preview_path"),
        )
    else:
        selected.update(
            preset_uuid=None,
            preset_scope=None,
            look_kind=None,
            profile_name=None,
            profile_hash=None,
            xmp_compatible=True,
            amount_supported=False,
            amount_note=None,
            selected_preview_key=previous.get("neutral_preview_key"),
            selected_preview_path=previous.get("neutral_preview_path"),
        )
    creative["scope"] = "global"
    creative["global_selection"] = selected
    creative["status"] = "pending"
    plan["basic_color"] = {"status": "enabled"}
    plan["color_enabled"] = True
    plan["color_mode"] = "style"
    _bump_plan(plan, run_dir)
    return selected


def confirm_recommended_styles(plan: dict[str, Any], run_dir: Path) -> None:
    """Adopt each group's recommended/neutral selection in one operation."""

    _ensure_plan_defaults(plan)
    creative = plan["creative_style"]
    group_ids = {str(int(item.get("group_id", -1))) for item in plan.get("items", [])}
    if not group_ids:
        raise ValueError("当前工程没有可应用创意外观的照片组。")
    scope = str(creative.get("scope") or "group")
    if scope == "global":
        global_selection = creative.get("global_selection")
        if not isinstance(global_selection, dict):
            raise ValueError("当前工程还没有全局统一风格推荐。")
        groups = {
            group_id: {**global_selection, "group_id": int(group_id)}
            for group_id in group_ids
        }
    else:
        groups = {
            str(key): dict(value)
            for key, value in creative.get("groups", {}).items()
            if isinstance(value, dict)
        }
    for group_id in group_ids:
        selection = groups.get(group_id)
        if not isinstance(selection, dict):
            raise ValueError("部分照片组还没有风格推荐，请先运行 AI 推荐。")  # noqa: TRY004
        if selection.get("manual_override") and selection.get("status") in {
            "confirmed",
            "skipped",
        }:
            continue
        if selection.get("recommendation_status") != "complete":
            raise ValueError("AI 推荐仍在生成真实预览，完成前不能全部采用。")
        recommended_kind = str(selection.get("recommended_kind") or "")
        preset_id = selection.get("recommended_preset_id")
        lut_id = selection.get("recommended_lut_id")
        if recommended_kind == "neutral":
            if not selection.get("neutral_preview_key"):
                raise ValueError("自然版本的 Lightroom 真实预览尚未完成。")
            selection.update(
                status="skipped",
                preset_id=None,
                preset_hash=None,
                preset_uuid=None,
                preset_scope=None,
                look_kind=None,
                profile_name=None,
                profile_hash=None,
                lut_id=None,
                lut_hash=None,
                xmp_compatible=True,
                amount=0,
                amount_supported=False,
                selected_preview_key=selection.get("neutral_preview_key"),
                selected_preview_path=selection.get("neutral_preview_path"),
                manual_override=False,
            )
        elif recommended_kind in {"preset", "lut"}:
            resource_is_lut = recommended_kind == "lut" or bool(lut_id)
            resource_id = lut_id if resource_is_lut else preset_id
            if not resource_id or resource_id == "natural":
                raise ValueError("AI 推荐缺少可确认的创意外观，请重新运行 AI 推荐。")
            candidate = next(
                (
                    value
                    for value in selection.get("top3") or []
                    if isinstance(value, dict)
                    and str(
                        value.get("lut_id" if resource_is_lut else "preset_id") or ""
                    )
                    == str(resource_id)
                    and value.get("render_status") == "ready"
                    and value.get("preview_key")
                ),
                None,
            )
            if candidate is None:
                raise ValueError("推荐创意外观的真实预览尚未完成。")
            recommended_hash = str(
                selection.get(
                    "recommended_lut_hash"
                    if resource_is_lut
                    else "recommended_preset_hash"
                )
                or ""
            )
            candidate_hash = str(
                candidate.get("lut_hash" if resource_is_lut else "preset_hash") or ""
            )
            if (
                not recommended_hash
                or not candidate_hash
                or recommended_hash != candidate_hash
            ):
                raise ValueError("推荐外观版本与真实预览不一致，请重新运行 AI 推荐。")
            recommended_amount = selection.get(
                "recommended_strength" if resource_is_lut else "recommended_amount"
            )
            if recommended_amount is None:
                recommended_amount = selection.get("recommended_amount")
            candidate_amount = candidate.get("strength", candidate.get("amount"))
            if (
                isinstance(recommended_amount, bool)
                or isinstance(candidate_amount, bool)
                or not isinstance(recommended_amount, (int, float))
                or not isinstance(candidate_amount, (int, float))
                or int(recommended_amount) != recommended_amount
                or int(candidate_amount) != candidate_amount
                or int(recommended_amount) != int(candidate_amount)
            ):
                raise ValueError(
                    "推荐强度与 Lightroom 真实预览不一致，请重新运行 AI 推荐。"
                )
            if (
                not resource_is_lut
                and not candidate.get("amount_supported")
                and int(candidate_amount) != 100
            ):
                raise ValueError("推荐预设不支持当前强度，请重新运行 AI 推荐。")
            selection.update(
                status="confirmed",
                preset_id=None if resource_is_lut else candidate.get("preset_id"),
                preset_hash=None if resource_is_lut else candidate_hash,
                preset_uuid=candidate.get("preset_uuid"),
                preset_scope=candidate.get("preset_scope"),
                look_kind=candidate.get("look_kind")
                or ("rendered_lut" if resource_is_lut else "develop_preset"),
                profile_name=candidate.get("profile_name"),
                profile_hash=candidate.get("profile_hash"),
                lut_id=candidate.get("lut_id") if resource_is_lut else None,
                lut_hash=candidate_hash if resource_is_lut else None,
                xmp_compatible=False
                if resource_is_lut
                else bool(candidate.get("xmp_compatible", True)),
                amount=int(candidate_amount),
                strength=int(candidate_amount) if resource_is_lut else None,
                amount_supported=True
                if resource_is_lut
                else bool(candidate.get("amount_supported")),
                amount_note=candidate.get("amount_note"),
                selected_preview_key=candidate.get("preview_key"),
                selected_preview_path=candidate.get("preview_path"),
                manual_override=False,
            )
        else:
            raise ValueError("AI 推荐结果不完整，请重新运行 AI 推荐。")
    creative["groups"] = groups
    if scope == "global":
        frozen = dict(next(iter(groups.values())))
        frozen.pop("group_id", None)
        frozen["scope"] = "global"
        creative["global_selection"] = frozen
    creative["status"] = "confirmed"
    plan["basic_color"] = {"status": "enabled"}
    plan["color_enabled"] = True
    plan["color_mode"] = "style"
    _bump_plan(plan, run_dir)


def replace_style_recommendations(
    plan: dict[str, Any],
    run_dir: Path,
    groups: dict[str, dict[str, Any]],
) -> None:
    """Replace recommendation evidence after an explicit AI rerun."""

    _ensure_plan_defaults(plan)
    valid_group_ids = {
        str(int(item.get("group_id", -1))) for item in plan.get("items", [])
    }
    unknown = set(groups) - valid_group_ids
    if unknown:
        raise ValueError(f"推荐结果包含未知照片组：{', '.join(sorted(unknown))}")
    current = plan["creative_style"].get("groups", {})
    merged = {
        key: dict(value)
        for key, value in current.items()
        if key in valid_group_ids and key not in groups and isinstance(value, dict)
    }
    merged.update({str(key): dict(value) for key, value in groups.items()})
    plan["creative_style"] = {
        "status": "pending",
        "groups": merged,
        "updated_at": _utc_now(),
    }
    plan["basic_color"] = {"status": "enabled"}
    plan["color_enabled"] = True
    plan["color_mode"] = "style"
    _bump_plan(plan, run_dir)


def merge_confirmed_develop(
    payload: dict[str, Any], plan: dict[str, Any] | None, review_revision: int
) -> dict[str, Any]:
    if (
        not plan
        or int(plan.get("schema_version", 0)) != DEVELOP_SCHEMA_VERSION
        or int(plan.get("source_review_revision", -1)) != int(review_revision)
    ):
        payload["develop_confirmed_count"] = 0
        return payload
    _ensure_plan_defaults(plan)
    by_index = {
        int(item["index"]): item
        for item in plan.get("items", [])
        if item.get("crop_confirmed", item.get("confirmed"))
    }
    basic_status = str(plan["basic_color"].get("status", "pending"))
    creative = plan["creative_style"]
    creative_groups = (
        creative.get("groups", {}) if isinstance(creative.get("groups"), dict) else {}
    )
    for index, result in enumerate(payload.get("results", [])):
        if index in by_index:
            source = by_index[index]
            recipe = {
                key: source[key]
                for key in (
                    "base_engine",
                    "crop_engine",
                    "base",
                    "crop_candidates",
                    "crop_id",
                    "angle",
                    "horizon_confidence",
                    "smart_crop",
                    "style_id",
                    "style_strength",
                    "confirmed",
                )
                if key in source
            }
            recipe["confirmed"] = True
            recipe["crop_status"] = str(plan["crop"].get("status", "pending"))
            recipe["basic_color_status"] = basic_status
            if basic_status != "enabled":
                recipe["base"] = {}
            group_selection = creative_groups.get(str(int(source.get("group_id", -1))))
            if creative.get("status") == "confirmed" and isinstance(
                group_selection, dict
            ):
                recipe["creative_style"] = {
                    key: group_selection.get(key)
                    for key in (
                        "status",
                        "preset_id",
                        "preset_hash",
                        "preset_uuid",
                        "preset_scope",
                        "look_kind",
                        "profile_name",
                        "profile_hash",
                        "lut_id",
                        "lut_hash",
                        "xmp_compatible",
                        "amount",
                        "amount_supported",
                        "confidence",
                        "top3",
                        "manual_override",
                    )
                    if key in group_selection
                }
            else:
                recipe["creative_style"] = {"status": "skipped"}
            result["develop"] = recipe
    payload["develop_confirmed_count"] = len(by_index)
    payload["develop_revision"] = int(plan.get("revision", 0))
    return payload
