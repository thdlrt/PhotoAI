from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyvips
import torch
from PIL import Image, ImageDraw, ImageFont

from .constants import VLM_MODEL_ID
from .group_critic import OllamaGroupCritic
from .util import read_json, write_json

SMART_CROP_VERSION = "semantic-crop-v2"
GROUNDING_DINO_MODEL_ID = "IDEA-Research/grounding-dino-tiny"
SAM2_MODEL_ID = "facebook/sam2-hiera-tiny"
SEGFORMER_MODEL_ID = "nvidia/segformer-b2-finetuned-ade-512-512"
SmartCropProgress = Callable[[str, str], None]

_OBJECT_PROMPT = (
    "person. face. animal. bird. cat. dog. horse. vehicle. bicycle. boat. "
    "building. monument. flower. tree. moon. sun."
)
_STRICT_SUBJECT_WORDS = {
    "person",
    "face",
    "animal",
    "bird",
    "cat",
    "dog",
    "horse",
    "vehicle",
    "bicycle",
    "boat",
    "moon",
    "sun",
}
_SKY_LABELS = {"sky"}
_WATER_LABELS = {"water", "sea", "river", "lake", "swimming pool"}
_GROUND_LABELS = {
    "earth",
    "field",
    "grass",
    "road",
    "sand",
    "mountain",
    "hill",
    "dirt",
    "snow",
    "sidewalk",
    "floor",
    "land",
}


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, float(value)))


def _bounds(left: float, top: float, right: float, bottom: float) -> dict[str, float]:
    left = _clamp(left)
    top = _clamp(top)
    right = _clamp(right)
    bottom = _clamp(bottom)
    if right <= left:
        right = min(1.0, left + 1e-4)
    if bottom <= top:
        bottom = min(1.0, top + 1e-4)
    return {
        "left": round(left, 6),
        "top": round(top, 6),
        "right": round(right, 6),
        "bottom": round(bottom, 6),
    }


def _area(box: dict[str, float]) -> float:
    return max(0.0, box["right"] - box["left"]) * max(0.0, box["bottom"] - box["top"])


def _intersection(first: dict[str, float], second: dict[str, float]) -> float:
    return max(
        0.0, min(first["right"], second["right"]) - max(first["left"], second["left"])
    ) * max(
        0.0, min(first["bottom"], second["bottom"]) - max(first["top"], second["top"])
    )


def _iou(first: dict[str, float], second: dict[str, float]) -> float:
    intersection = _intersection(first, second)
    return intersection / max(_area(first) + _area(second) - intersection, 1e-8)


def _coverage(subject: dict[str, float], crop: dict[str, float]) -> float:
    return _intersection(subject, crop) / max(_area(subject), 1e-8)


def _crop_dimensions(
    width: int, height: int, aspect: float, coverage: float
) -> tuple[float, float]:
    source_aspect = width / max(1, height)
    if source_aspect >= aspect:
        crop_height = _clamp(coverage, 0.1, 1.0)
        crop_width = crop_height * aspect / source_aspect
    else:
        crop_width = _clamp(coverage, 0.1, 1.0)
        crop_height = crop_width * source_aspect / aspect
    return min(1.0, crop_width), min(1.0, crop_height)


def _vips_image(image: Image.Image) -> pyvips.Image:
    rgb = np.ascontiguousarray(np.asarray(image.convert("RGB"), dtype=np.uint8))
    height, width = rgb.shape[:2]
    return pyvips.Image.new_from_memory(rgb.tobytes(), width, height, 3, "uchar")


def _smartcrop(
    source: pyvips.Image, crop_width: float, crop_height: float, interesting: str
) -> dict[str, float]:
    width = max(1, min(source.width, round(crop_width * source.width)))
    height = max(1, min(source.height, round(crop_height * source.height)))
    cropped = source.smartcrop(width, height, interesting=interesting)
    left = (
        max(0, min(source.width - width, -int(cropped.get("xoffset")))) / source.width
    )
    top = (
        max(0, min(source.height - height, -int(cropped.get("yoffset"))))
        / source.height
    )
    return _bounds(left, top, left + width / source.width, top + height / source.height)


def _image_digest(image: Image.Image) -> str:
    sample = image.convert("RGB")
    sample.thumbnail((512, 512), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    sample.save(buffer, format="JPEG", quality=82)
    return hashlib.sha256(
        SMART_CROP_VERSION.encode("utf-8")
        + f"{image.width}x{image.height}".encode("ascii")
        + buffer.getvalue()
    ).hexdigest()


def _encode_image(image: Image.Image, max_pixels: int = 896) -> str:
    source = image.convert("RGB")
    source.thumbnail((max_pixels, max_pixels), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _data_dir_for_run(run_dir: Path) -> Path:
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent
    return run_dir.parent


def _model_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


@dataclass
class _VisionModels:
    object_processor: Any = None
    object_model: Any = None
    mask_processor: Any = None
    mask_model: Any = None
    segment_processor: Any = None
    segment_model: Any = None


_VISION_MODELS = _VisionModels()


def _report_progress(
    progress: SmartCropProgress | None, phase: str, label: str
) -> None:
    if progress is not None:
        progress(phase, label)


def _load_object_model() -> tuple[Any, Any]:
    if _VISION_MODELS.object_model is None:
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        from .model_resources import managed_hf_model_path

        device = _model_device()
        model_path = managed_hf_model_path("grounding-dino-tiny")
        processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
        model = AutoModelForZeroShotObjectDetection.from_pretrained(
            model_path, local_files_only=True
        )
        model.to(device).eval()
        _VISION_MODELS.object_processor = processor
        _VISION_MODELS.object_model = model
    return _VISION_MODELS.object_processor, _VISION_MODELS.object_model


def _load_segment_model() -> tuple[Any, Any]:
    if _VISION_MODELS.segment_model is None:
        from transformers import (
            SegformerForSemanticSegmentation,
            SegformerImageProcessor,
        )

        from .model_resources import managed_hf_model_path

        device = _model_device()
        model_path = managed_hf_model_path("segformer-b2")
        processor = SegformerImageProcessor.from_pretrained(
            model_path, local_files_only=True
        )
        model = SegformerForSemanticSegmentation.from_pretrained(
            model_path, local_files_only=True
        )
        model.to(device).eval()
        _VISION_MODELS.segment_processor = processor
        _VISION_MODELS.segment_model = model
    return _VISION_MODELS.segment_processor, _VISION_MODELS.segment_model


def _load_mask_model() -> tuple[Any, Any]:
    if _VISION_MODELS.mask_model is None:
        from transformers import Sam2Model, Sam2Processor

        from .model_resources import managed_hf_model_path

        device = _model_device()
        model_path = managed_hf_model_path("sam2-hiera-tiny")
        processor = Sam2Processor.from_pretrained(model_path, local_files_only=True)
        model = Sam2Model.from_pretrained(model_path, local_files_only=True)
        model.to(device).eval()
        _VISION_MODELS.mask_processor = processor
        _VISION_MODELS.mask_model = model
    return _VISION_MODELS.mask_processor, _VISION_MODELS.mask_model


def _subject_detection(image: Image.Image) -> list[dict[str, Any]]:
    processor, model = _load_object_model()
    device = next(model.parameters()).device
    inputs = processor(images=image, text=_OBJECT_PROMPT, return_tensors="pt")
    model_inputs = {
        key: value.to(device) if hasattr(value, "to") else value
        for key, value in inputs.items()
    }
    with torch.inference_mode():
        outputs = model(**model_inputs)
    result = processor.post_process_grounded_object_detection(
        outputs,
        inputs.input_ids,
        threshold=0.28,
        text_threshold=0.22,
        target_sizes=[(image.height, image.width)],
    )[0]
    labels = result.get("text_labels") or result.get("labels") or []
    subjects: list[dict[str, Any]] = []
    for box, score, label in zip(
        result.get("boxes", []),
        result.get("scores", []),
        labels,
        strict=False,
    ):
        coords = [float(value) for value in box.tolist()]
        name = str(label).strip().casefold()
        normalized = _bounds(
            coords[0] / image.width,
            coords[1] / image.height,
            coords[2] / image.width,
            coords[3] / image.height,
        )
        if _area(normalized) < 0.0002:
            continue
        subjects.append(
            {
                "label": name,
                "score": round(float(score), 4),
                "box": normalized,
                "strict": any(word in name for word in _STRICT_SUBJECT_WORDS),
                "source": "grounding-dino",
            }
        )
    return sorted(
        subjects,
        key=lambda item: (bool(item["strict"]), item["score"], _area(item["box"])),
        reverse=True,
    )[:12]


def _mask_integral(
    mask: np.ndarray, width: int, height: int
) -> tuple[np.ndarray, tuple[int, int], int]:
    grid_width = min(320, max(1, width))
    grid_height = min(320, max(1, round(height * grid_width / max(1, width))))
    grid = (
        np.asarray(
            Image.fromarray(mask.astype(np.uint8) * 255, mode="L").resize(
                (grid_width, grid_height), Image.Resampling.NEAREST
            ),
            dtype=np.uint8,
        )
        >= 128
    )
    integral = np.pad(
        grid.astype(np.int32).cumsum(axis=0).cumsum(axis=1),
        ((1, 0), (1, 0)),
        mode="constant",
    )
    return integral, (grid_width, grid_height), int(grid.sum())


def _refine_subject_masks(image: Image.Image, subjects: list[dict[str, Any]]) -> int:
    selected = [
        item
        for item in subjects
        if item.get("strict") or float(item.get("score", 0.0)) >= 0.5
    ][:8]
    if not selected:
        return 0
    processor, model = _load_mask_model()
    device = next(model.parameters()).device
    input_boxes = [
        [
            float(item["box"]["left"]) * image.width,
            float(item["box"]["top"]) * image.height,
            float(item["box"]["right"]) * image.width,
            float(item["box"]["bottom"]) * image.height,
        ]
        for item in selected
    ]
    inputs = processor(images=image, input_boxes=[input_boxes], return_tensors="pt")
    model_inputs = {
        key: value.to(device) if hasattr(value, "to") else value
        for key, value in inputs.items()
    }
    with torch.inference_mode():
        outputs = model(**model_inputs, multimask_output=False)
    masks = processor.post_process_masks(
        outputs.pred_masks.cpu(), inputs["original_sizes"]
    )[0]
    scores = getattr(outputs, "iou_scores", None)
    score_values = (
        scores.detach().float().cpu().reshape(-1).tolist() if scores is not None else []
    )
    refined = 0
    for index, subject in enumerate(selected):
        if index >= int(masks.shape[0]):
            break
        mask = masks[index]
        while mask.ndim > 2:
            mask = mask[0]
        array = mask.detach().cpu().numpy() > 0
        if array.shape != (image.height, image.width):
            array = (
                np.asarray(
                    Image.fromarray(array.astype(np.uint8) * 255, mode="L").resize(
                        (image.width, image.height), Image.Resampling.NEAREST
                    ),
                    dtype=np.uint8,
                )
                >= 128
            )

        # A loose box guard prevents a bad prompt from turning a tiny subject into
        # a background-sized mask while still allowing SAM2 to recover missed edges.
        box = subject["box"]
        pad_x = max(0.012, (box["right"] - box["left"]) * 0.18)
        pad_y = max(0.012, (box["bottom"] - box["top"]) * 0.18)
        guard = _bounds(
            box["left"] - pad_x,
            box["top"] - pad_y,
            box["right"] + pad_x,
            box["bottom"] + pad_y,
        )
        x1 = max(0, math.floor(guard["left"] * image.width))
        y1 = max(0, math.floor(guard["top"] * image.height))
        x2 = min(image.width, math.ceil(guard["right"] * image.width))
        y2 = min(image.height, math.ceil(guard["bottom"] * image.height))
        guarded = np.zeros_like(array, dtype=bool)
        guarded[y1:y2, x1:x2] = array[y1:y2, x1:x2]
        ys, xs = np.nonzero(guarded)
        if len(xs) < 16:
            continue
        mask_box = _bounds(
            float(xs.min()) / image.width,
            float(ys.min()) / image.height,
            float(xs.max() + 1) / image.width,
            float(ys.max() + 1) / image.height,
        )
        integral, shape, mask_area = _mask_integral(guarded, image.width, image.height)
        if mask_area <= 0:
            continue
        subject["mask_box"] = mask_box
        subject["mask_area"] = round(float(guarded.mean()), 6)
        subject["mask_score"] = (
            round(float(score_values[index]), 4) if index < len(score_values) else None
        )
        subject["mask_source"] = "sam2"
        subject["_mask_integral"] = integral
        subject["_mask_shape"] = shape
        subject["_mask_grid_area"] = mask_area
        refined += 1
    return refined


def _landscape_segmentation(image: Image.Image) -> dict[str, Any]:
    processor, model = _load_segment_model()
    device = next(model.parameters()).device
    inputs = processor(images=image, return_tensors="pt")
    model_inputs = {
        key: value.to(device) if hasattr(value, "to") else value
        for key, value in inputs.items()
    }
    with torch.inference_mode():
        outputs = model(**model_inputs)
    segmentation = (
        processor.post_process_semantic_segmentation(
            outputs, target_sizes=[(image.height, image.width)]
        )[0]
        .detach()
        .cpu()
        .numpy()
    )
    id2label = {
        int(key): str(value).strip().casefold()
        for key, value in model.config.id2label.items()
    }
    total = max(1, segmentation.size)

    def fraction(names: set[str]) -> float:
        ids = [index for index, label in id2label.items() if label in names]
        return float(np.isin(segmentation, ids).sum()) / total if ids else 0.0

    sky_ids = [index for index, label in id2label.items() if label in _SKY_LABELS]
    horizon_y = -1.0
    horizon_confidence = 0.0
    if sky_ids:
        sky_mask = np.isin(segmentation, sky_ids)
        row_fraction = sky_mask.mean(axis=1)
        rows = np.flatnonzero(row_fraction >= 0.12)
        if rows.size:
            horizon_y = float(rows[-1] + 1) / image.height
            continuity = float((row_fraction[: rows[-1] + 1] >= 0.12).mean())
            horizon_confidence = _clamp(
                float(row_fraction[: rows[-1] + 1].mean()) * 1.4 * continuity
            )
    return {
        "sky_fraction": round(fraction(_SKY_LABELS), 4),
        "water_fraction": round(fraction(_WATER_LABELS), 4),
        "ground_fraction": round(fraction(_GROUND_LABELS), 4),
        "horizon_y": round(horizon_y, 4),
        "horizon_confidence": round(horizon_confidence, 4),
        "engine": SEGFORMER_MODEL_ID,
    }


def _scene_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "scene_type": {
                "type": "string",
                "enum": ["landscape", "wildlife", "portrait", "architecture", "other"],
            },
            "composition": {
                "type": "string",
                "enum": [
                    "thirds",
                    "centered",
                    "symmetry",
                    "layers",
                    "leading_lines",
                    "negative_space",
                    "other",
                ],
            },
            "horizon_y": {"type": "number", "minimum": -1, "maximum": 1},
            "horizon_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "main_subject_boxes": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "array",
                    "minItems": 4,
                    "maxItems": 4,
                    "items": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
            "gaze_direction": {
                "type": "string",
                "enum": ["left", "right", "up", "down", "none"],
            },
            "negative_space_direction": {
                "type": "string",
                "enum": ["left", "right", "top", "bottom", "none"],
            },
            "sky_importance": {"type": "number", "minimum": 0, "maximum": 1},
            "ground_importance": {"type": "number", "minimum": 0, "maximum": 1},
            "water_importance": {"type": "number", "minimum": 0, "maximum": 1},
            "preferred_aspect": {
                "type": "string",
                "enum": ["original", "3:2", "4:3", "4:5", "16:9"],
            },
            "summary": {"type": "string"},
        },
        "required": [
            "scene_type",
            "composition",
            "horizon_y",
            "horizon_confidence",
            "main_subject_boxes",
            "gaze_direction",
            "negative_space_direction",
            "sky_importance",
            "ground_importance",
            "water_importance",
            "preferred_aspect",
            "summary",
        ],
        "additionalProperties": False,
    }


def _rank_schema(ids: list[str]) -> dict[str, Any]:
    count = len(ids)
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "rank": {"type": "integer", "minimum": 1, "maximum": count},
                        "score": {"type": "number", "minimum": 0, "maximum": 100},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "rank", "score", "reason"],
                    "additionalProperties": False,
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "summary": {"type": "string"},
        },
        "required": ["items", "confidence", "summary"],
        "additionalProperties": False,
    }


def _vlm_json(
    images: list[Image.Image], prompt: str, schema: dict[str, Any], data_dir: Path
) -> dict[str, Any]:
    # The desktop JobManager assigns every worker its own random loopback
    # endpoint.  Do not replace that managed endpoint with the legacy 11435
    # development default here.
    critic = OllamaGroupCritic(data_dir, model=VLM_MODEL_ID)
    critic.ensure_ready()
    payload = {
        "model": VLM_MODEL_ID,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [_encode_image(image) for image in images],
            }
        ],
        "stream": False,
        "think": False,
        "format": schema,
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1800},
        "keep_alive": "5m",
    }
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            response = critic._request("/api/chat", payload, timeout=900.0)
            content = response["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
            if not isinstance(result, dict):
                raise TypeError("视觉模型没有返回 JSON 对象")
            return result
        except (KeyError, TypeError, ValueError, OSError) as exc:
            last_error = exc
    raise RuntimeError(f"Qwen3-VL 构图分析失败：{last_error}")


def _default_scene(
    segmentation: dict[str, Any], subjects: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "scene_type": "landscape",
        "composition": "thirds",
        "horizon_y": float(segmentation.get("horizon_y", -1.0)),
        "horizon_confidence": float(segmentation.get("horizon_confidence", 0.0)),
        "main_subject_boxes": [item["box"] for item in subjects[:3]],
        "gaze_direction": "none",
        "negative_space_direction": "none",
        "sky_importance": float(segmentation.get("sky_fraction", 0.0)),
        "ground_importance": float(segmentation.get("ground_fraction", 0.0)),
        "water_importance": float(segmentation.get("water_fraction", 0.0)),
        "preferred_aspect": "original",
        "summary": "本地语义模型回退结果",
    }


def _normalize_scene(
    payload: dict[str, Any], fallback: dict[str, Any]
) -> dict[str, Any]:
    output = dict(fallback)
    for key in (
        "scene_type",
        "composition",
        "gaze_direction",
        "negative_space_direction",
        "preferred_aspect",
        "summary",
    ):
        if isinstance(payload.get(key), str) and payload[key].strip():
            output[key] = payload[key].strip()[:240]
    for key in (
        "horizon_y",
        "horizon_confidence",
        "sky_importance",
        "ground_importance",
        "water_importance",
    ):
        try:
            value = float(payload.get(key))
        except (TypeError, ValueError):
            continue
        output[key] = _clamp(value, -1.0 if key == "horizon_y" else 0.0, 1.0)
    boxes: list[dict[str, float]] = []
    for value in payload.get("main_subject_boxes", []):
        if not isinstance(value, list) or len(value) != 4:
            continue
        try:
            box = _bounds(*(float(item) for item in value))
        except (TypeError, ValueError):
            continue
        if _area(box) >= 0.0002:
            boxes.append(box)
    if boxes:
        output["main_subject_boxes"] = boxes[:4]
    return output


def analyze_scene(
    image: Image.Image,
    data_dir: Path,
    *,
    mode: str = "full",
    progress: SmartCropProgress | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    subjects: list[dict[str, Any]] = []
    mask_count = 0
    segmentation: dict[str, Any] = {
        "sky_fraction": 0.0,
        "water_fraction": 0.0,
        "ground_fraction": 0.0,
        "horizon_y": -1.0,
        "horizon_confidence": 0.0,
        "engine": "unavailable",
    }
    if mode == "full":
        _report_progress(progress, "subjects", "Grounding DINO 识别主体")
        try:
            subjects = _subject_detection(image)
        except Exception as exc:  # noqa: BLE001 - model failures must not break a batch
            warnings.append(f"主体检测降级：{type(exc).__name__}: {exc}")
        if subjects:
            _report_progress(progress, "masks", "SAM2 生成主体蒙版")
            try:
                mask_count = _refine_subject_masks(image, subjects)
            except Exception as exc:  # noqa: BLE001 - optional mask refinement degrades safely
                warnings.append(f"主体蒙版降级：{type(exc).__name__}: {exc}")
        _report_progress(progress, "regions", "SegFormer 分析天空与地面")
        try:
            segmentation = _landscape_segmentation(image)
        except Exception as exc:  # noqa: BLE001 - optional segmentation degrades safely
            warnings.append(f"风光分区降级：{type(exc).__name__}: {exc}")
    fallback = _default_scene(segmentation, subjects)
    scene = dict(fallback)
    if mode == "full":
        _report_progress(progress, "scene", "Qwen3-VL 理解构图语义")
        detected = [
            {"label": item["label"], "score": item["score"], "box": item["box"]}
            for item in subjects
        ]
        prompt = (
            "你是专业摄影构图编辑。分析这张照片，不要进行调色。结合以下机器检测结果："
            f"主体={json.dumps(detected, ensure_ascii=False)}；"
            f"风光分区={json.dumps(segmentation, ensure_ascii=False)}。"
            "判断主次主体、地平线、人物或动物朝向、应保留的视线空间、天空/地面/水面重要性，"
            "以及最适合的三分法、居中、对称、层次、引导线或留白构图。"
            "所有坐标按原图归一化为 0–1，框顺序为 left,top,right,bottom。"
            "只按 JSON Schema 返回可观察结论。"
        )
        try:
            scene = _normalize_scene(
                _vlm_json([image], prompt, _scene_schema(), data_dir), fallback
            )
        except Exception as exc:  # noqa: BLE001 - optional VLM analysis degrades safely
            warnings.append(f"构图语义分析降级：{type(exc).__name__}: {exc}")
    # SegFormer is more reliable for a visible sky boundary than free-form VLM
    # coordinates, so prefer it when its confidence is materially stronger.
    if float(segmentation.get("horizon_confidence", 0.0)) > float(
        scene.get("horizon_confidence", 0.0)
    ):
        scene["horizon_y"] = float(segmentation["horizon_y"])
        scene["horizon_confidence"] = float(segmentation["horizon_confidence"])
    scene["subjects"] = subjects
    scene["mask_count"] = mask_count
    scene["segmentation"] = segmentation
    scene["warnings"] = warnings
    scene["engines"] = {
        "scene": VLM_MODEL_ID
        if mode == "full" and not any("构图语义" in item for item in warnings)
        else "heuristic",
        "subjects": GROUNDING_DINO_MODEL_ID if subjects else "none",
        "masks": SAM2_MODEL_ID if mask_count else "none",
        "regions": segmentation.get("engine", "none"),
    }
    return scene


def _candidate_positions(
    crop_width: float, crop_height: float, scene: dict[str, Any]
) -> Iterable[tuple[float, float]]:
    max_left = max(0.0, 1.0 - crop_width)
    max_top = max(0.0, 1.0 - crop_height)
    x_values = (
        [max_left * value for value in (0.0, 0.25, 0.5, 0.75, 1.0)]
        if max_left
        else [0.0]
    )
    y_values = (
        [max_top * value for value in (0.0, 0.25, 0.5, 0.75, 1.0)] if max_top else [0.0]
    )
    positions = {
        (round(left, 6), round(top, 6)) for left in x_values for top in y_values
    }
    boxes = list(scene.get("main_subject_boxes", []))
    boxes.extend(
        item.get("mask_box") or item.get("box")
        for item in scene.get("subjects", [])[:4]
        if isinstance(item.get("mask_box") or item.get("box"), dict)
    )
    for box in boxes:
        center_x = (box["left"] + box["right"]) / 2
        center_y = (box["top"] + box["bottom"]) / 2
        for target_x in (1 / 3, 1 / 2, 2 / 3):
            for target_y in (1 / 3, 1 / 2, 2 / 3):
                positions.add(
                    (
                        round(
                            _clamp(center_x - crop_width * target_x, 0.0, max_left), 6
                        ),
                        round(
                            _clamp(center_y - crop_height * target_y, 0.0, max_top), 6
                        ),
                    )
                )
    return sorted(positions)


def _subject_mask_coverage(
    subject: dict[str, Any], crop: dict[str, float]
) -> float | None:
    integral = subject.get("_mask_integral")
    shape = subject.get("_mask_shape")
    total = int(subject.get("_mask_grid_area", 0))
    if (
        not isinstance(integral, np.ndarray)
        or not isinstance(shape, tuple)
        or len(shape) != 2
        or total <= 0
    ):
        return None
    width, height = shape
    x1 = max(0, min(width, math.floor(crop["left"] * width)))
    y1 = max(0, min(height, math.floor(crop["top"] * height)))
    x2 = max(x1, min(width, math.ceil(crop["right"] * width)))
    y2 = max(y1, min(height, math.ceil(crop["bottom"] * height)))
    kept = int(
        integral[y2, x2] - integral[y1, x2] - integral[y2, x1] + integral[y1, x1]
    )
    return _clamp(kept / total)


def _subject_score(
    crop: dict[str, float], scene: dict[str, Any]
) -> tuple[float, float]:
    subjects = list(scene.get("subjects", []))
    if not subjects:
        subjects = [
            {"box": box, "strict": False} for box in scene.get("main_subject_boxes", [])
        ]
    if not subjects:
        return 0.72, 1.0
    weighted: list[float] = []
    minimum_strict = 1.0
    for subject in subjects:
        box = subject.get("box")
        if not isinstance(box, dict):
            continue
        mask_coverage = _subject_mask_coverage(subject, crop)
        value = mask_coverage if mask_coverage is not None else _coverage(box, crop)
        weight = 1.5 if subject.get("strict") else 1.0
        weighted.append(value * weight)
        if subject.get("strict"):
            minimum_strict = min(minimum_strict, value)
    denominator = sum(1.5 if item.get("strict") else 1.0 for item in subjects) or 1.0
    return sum(weighted) / denominator, minimum_strict


def _composition_score(crop: dict[str, float], scene: dict[str, Any]) -> float:
    boxes = list(scene.get("main_subject_boxes", []))
    if not boxes and scene.get("subjects"):
        boxes = [scene["subjects"][0]["box"]]
    if not boxes:
        return 0.65
    width = crop["right"] - crop["left"]
    height = crop["bottom"] - crop["top"]
    composition = str(scene.get("composition", "thirds"))
    scores: list[float] = []
    for box in boxes[:3]:
        x = ((box["left"] + box["right"]) / 2 - crop["left"]) / max(width, 1e-8)
        y = ((box["top"] + box["bottom"]) / 2 - crop["top"]) / max(height, 1e-8)
        if composition in {"centered", "symmetry"}:
            distance = math.hypot(x - 0.5, y - 0.5)
            scores.append(_clamp(1.0 - distance / 0.55))
        else:
            distance = min(
                math.hypot(x - tx, y - ty)
                for tx in (1 / 3, 2 / 3)
                for ty in (1 / 3, 2 / 3)
            )
            scores.append(_clamp(1.0 - distance / 0.48))
    return sum(scores) / len(scores)


def _horizon_score(
    crop: dict[str, float], scene: dict[str, Any]
) -> tuple[float, float]:
    confidence = float(scene.get("horizon_confidence", 0.0))
    horizon = float(scene.get("horizon_y", -1.0))
    if confidence < 0.25 or not 0.0 <= horizon <= 1.0:
        return 0.68, 1.0
    if not crop["top"] <= horizon <= crop["bottom"]:
        return 0.0, 0.0
    relative = (horizon - crop["top"]) / max(crop["bottom"] - crop["top"], 1e-8)
    target_score = max(
        _clamp(1.0 - abs(relative - 1 / 3) / 0.34),
        _clamp(1.0 - abs(relative - 2 / 3) / 0.34),
    )
    return target_score, 1.0


def _space_score(crop: dict[str, float], scene: dict[str, Any]) -> float:
    direction = str(scene.get("gaze_direction", "none"))
    boxes = list(scene.get("main_subject_boxes", []))
    if direction == "none" or not boxes:
        direction = str(scene.get("negative_space_direction", "none"))
    if direction == "none" or not boxes:
        return 0.7
    box = boxes[0]
    cx = (box["left"] + box["right"]) / 2
    cy = (box["top"] + box["bottom"]) / 2
    width = max(crop["right"] - crop["left"], 1e-8)
    height = max(crop["bottom"] - crop["top"], 1e-8)
    available = {
        "left": (cx - crop["left"]) / width,
        "right": (crop["right"] - cx) / width,
        "up": (cy - crop["top"]) / height,
        "top": (cy - crop["top"]) / height,
        "down": (crop["bottom"] - cy) / height,
        "bottom": (crop["bottom"] - cy) / height,
    }.get(direction, 0.5)
    return _clamp(available / 0.58)


def _sky_ground_score(crop: dict[str, float], scene: dict[str, Any]) -> float:
    horizon = float(scene.get("horizon_y", -1.0))
    confidence = float(scene.get("horizon_confidence", 0.0))
    if confidence < 0.25 or not crop["top"] <= horizon <= crop["bottom"]:
        return 0.68
    height = max(crop["bottom"] - crop["top"], 1e-8)
    sky_ratio = (horizon - crop["top"]) / height
    sky_importance = float(scene.get("sky_importance", 0.0))
    land_importance = max(
        float(scene.get("ground_importance", 0.0)),
        float(scene.get("water_importance", 0.0)),
    )
    target = (
        2 / 3
        if sky_importance > land_importance + 0.15
        else 1 / 3
        if land_importance > sky_importance + 0.15
        else 0.5
    )
    return _clamp(1.0 - abs(sky_ratio - target) / 0.5)


def generate_dense_candidates(
    image: Image.Image, scene: dict[str, Any]
) -> list[dict[str, Any]]:
    source = _vips_image(image)
    original_aspect = image.width / max(1, image.height)
    landscape = original_aspect >= 1.0
    aspect_specs: list[tuple[str, float, list[float]]] = [
        ("original", original_aspect, [0.96, 0.92, 0.88, 0.82, 0.76]),
        ("wide", 16 / 9 if landscape else 4 / 5, [1.0, 0.96, 0.91, 0.86]),
        ("classic", 3 / 2 if landscape else 2 / 3, [1.0, 0.94, 0.88]),
        ("standard", 4 / 3 if landscape else 3 / 4, [1.0, 0.94, 0.88]),
    ]
    preferred = str(scene.get("preferred_aspect", "original"))
    preferred_value = {"3:2": 3 / 2, "4:3": 4 / 3, "4:5": 4 / 5, "16:9": 16 / 9}.get(
        preferred
    )
    if preferred_value and all(
        abs(spec[1] - preferred_value) > 0.01 for spec in aspect_specs
    ):
        aspect_specs.append(("preferred", preferred_value, [1.0, 0.94, 0.88]))

    original = {
        "internal_id": "C000",
        "kind": "original",
        "aspect": round(original_aspect, 4),
        "coverage": 1.0,
        "bounds": _bounds(0, 0, 1, 1),
        "rule_score": 0.66,
        "valid": True,
        "metrics": {"original": 1.0},
        "reasons": ["保留完整原始画面"],
    }
    candidates: list[dict[str, Any]] = [original]
    seen: list[dict[str, float]] = [original["bounds"]]
    sequence = 1
    for kind, aspect, coverages in aspect_specs:
        for coverage in coverages:
            crop_width, crop_height = _crop_dimensions(
                image.width, image.height, aspect, coverage
            )
            attention = _smartcrop(source, crop_width, crop_height, "attention")
            entropy = _smartcrop(source, crop_width, crop_height, "entropy")
            positions = set(_candidate_positions(crop_width, crop_height, scene))
            positions.add((attention["left"], attention["top"]))
            positions.add((entropy["left"], entropy["top"]))
            for left, top in sorted(positions):
                crop = _bounds(left, top, left + crop_width, top + crop_height)
                if any(_iou(crop, existing) >= 0.997 for existing in seen):
                    continue
                subject_score, strict_coverage = _subject_score(crop, scene)
                horizon_score, horizon_kept = _horizon_score(crop, scene)
                metrics = {
                    "subject": subject_score,
                    "composition": _composition_score(crop, scene),
                    "horizon": horizon_score,
                    "space": _space_score(crop, scene),
                    "sky_ground": _sky_ground_score(crop, scene),
                    "attention": max(_iou(crop, attention), _iou(crop, entropy)),
                    "retention": math.sqrt(_area(crop)),
                }
                valid = strict_coverage >= 0.96 and horizon_kept >= 0.5
                score = (
                    0.28 * metrics["subject"]
                    + 0.19 * metrics["composition"]
                    + 0.15 * metrics["horizon"]
                    + 0.10 * metrics["space"]
                    + 0.10 * metrics["sky_ground"]
                    + 0.11 * metrics["attention"]
                    + 0.07 * metrics["retention"]
                )
                if not valid:
                    score *= 0.35
                reasons: list[str] = []
                if metrics["subject"] >= 0.96:
                    reasons.append(
                        "像素级完整保留主体"
                        if scene.get("mask_count")
                        else "完整保留主体"
                    )
                if metrics["horizon"] >= 0.8:
                    reasons.append("地平线接近三分线")
                if metrics["composition"] >= 0.78:
                    reasons.append("主体位置符合构图关系")
                if (
                    metrics["space"] >= 0.8
                    and str(scene.get("gaze_direction", "none")) != "none"
                ):
                    reasons.append("保留视线方向空间")
                candidates.append(
                    {
                        "internal_id": f"C{sequence:03d}",
                        "kind": kind,
                        "aspect": round(aspect, 4),
                        "coverage": round(math.sqrt(_area(crop)), 4),
                        "bounds": crop,
                        "rule_score": round(_clamp(score), 4),
                        "valid": valid,
                        "metrics": {
                            key: round(float(value), 4)
                            for key, value in metrics.items()
                        },
                        "reasons": reasons[:3] or ["综合构图候选"],
                    }
                )
                sequence += 1
                seen.append(crop)
    return candidates


def _crop_image(image: Image.Image, bounds: dict[str, float]) -> Image.Image:
    return image.crop(
        (
            round(bounds["left"] * image.width),
            round(bounds["top"] * image.height),
            round(bounds["right"] * image.width),
            round(bounds["bottom"] * image.height),
        )
    ).convert("RGB")


def _contact_sheet(image: Image.Image, candidates: list[dict[str, Any]]) -> Image.Image:
    columns = 3
    tile_width, tile_height, label_height = 360, 250, 28
    rows = math.ceil(len(candidates) / columns)
    sheet = Image.new(
        "RGB", (columns * tile_width, rows * (tile_height + label_height)), "#111313"
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, candidate in enumerate(candidates):
        crop = _crop_image(image, candidate["bounds"])
        crop.thumbnail((tile_width - 8, tile_height - 8), Image.Resampling.LANCZOS)
        x = (index % columns) * tile_width
        y = (index // columns) * (tile_height + label_height)
        sheet.paste(
            crop,
            (x + (tile_width - crop.width) // 2, y + (tile_height - crop.height) // 2),
        )
        label = f"{candidate['internal_id']}  rule={candidate['rule_score']:.2f}"
        draw.rectangle(
            (x, y + tile_height, x + tile_width, y + tile_height + label_height),
            fill="#202323",
        )
        draw.text((x + 8, y + tile_height + 7), label, fill="white", font=font)
    return sheet


def _rank_candidates(
    image: Image.Image,
    candidates: list[dict[str, Any]],
    scene: dict[str, Any],
    data_dir: Path,
    *,
    mode: str,
) -> tuple[list[dict[str, Any]], float, str]:
    valid = [item for item in candidates if item.get("valid")]
    original = candidates[0]
    pools: list[dict[str, Any]] = [original]
    for kind in ("original", "wide", "classic", "standard"):
        pools.extend(
            sorted(
                (item for item in valid if item["kind"] == kind),
                key=lambda item: item["rule_score"],
                reverse=True,
            )[:3]
        )
    pools.extend(sorted(valid, key=lambda item: item["rule_score"], reverse=True)[:5])
    shortlist: list[dict[str, Any]] = []
    for item in pools:
        if item not in shortlist:
            shortlist.append(item)
    shortlist = shortlist[:12]
    if mode != "full" or len(shortlist) <= 1:
        return shortlist, 0.0, "规则评分回退"
    sheet = _contact_sheet(image, shortlist)
    ids = [item["internal_id"] for item in shortlist]
    prompt = (
        "你是专业摄影构图编辑。第一张图是原始照片，第二张是带编号的候选裁剪联系表。"
        f"场景分析={json.dumps({key: value for key, value in scene.items() if key not in {'subjects', 'segmentation'}}, ensure_ascii=False)}。"
        "逐一评价主体是否完整、三分法/居中/对称关系、地平线位置、人物或动物视线空间、"
        "天空与地面或水面比例、引导线、边缘截断和画面平衡。不要把裁得更紧本身当成优点。"
        "原图可以排名第一；只有明确改善才推荐裁切。为所有编号给出唯一完整名次和 0–100 分。"
    )
    result = _vlm_json([image, sheet], prompt, _rank_schema(ids), data_dir)
    items = result.get("items")
    if not isinstance(items, list) or {
        str(item.get("id")) for item in items if isinstance(item, dict)
    } != set(ids):
        raise ValueError("Qwen3-VL 没有覆盖全部构图候选")
    cleaned: dict[str, dict[str, Any]] = {}
    for item in items:
        identifier = str(item["id"])
        cleaned[identifier] = {
            "rank": int(item.get("rank", len(ids))),
            "score": _clamp(float(item.get("score", 0.0)) / 100.0),
            "reason": str(item.get("reason", "")).strip()[:180],
        }
    ranked = sorted(
        shortlist,
        key=lambda item: (
            -(
                0.58 * cleaned[item["internal_id"]]["score"]
                + 0.42 * float(item["rule_score"])
            ),
            cleaned[item["internal_id"]]["rank"],
            item["internal_id"],
        ),
    )
    for rank, item in enumerate(ranked, start=1):
        model = cleaned[item["internal_id"]]
        item["vlm_rank"] = rank
        item["vlm_score"] = round(model["score"], 4)
        item["score"] = round(
            0.58 * model["score"] + 0.42 * float(item["rule_score"]), 4
        )
        if model["reason"]:
            item["reasons"] = [model["reason"], *item.get("reasons", [])][:3]
    return (
        ranked,
        _clamp(float(result.get("confidence", 0.0))),
        str(result.get("summary", ""))[:240],
    )


def _choose_distinct(
    ranked: list[dict[str, Any]], predicate: Any, excluded: list[dict[str, Any]]
) -> dict[str, Any] | None:
    for item in ranked:
        if not predicate(item):
            continue
        if any(_iou(item["bounds"], other["bounds"]) > 0.94 for other in excluded):
            continue
        return item
    return None


def _public_candidates(
    dense: list[dict[str, Any]],
    ranked: list[dict[str, Any]],
    confidence: float,
    summary: str,
) -> tuple[list[dict[str, Any]], str]:
    original = dense[0]
    ranked_non_original = [
        item for item in ranked if item["internal_id"] != original["internal_id"]
    ]
    selection_pool = list(ranked_non_original)
    for item in sorted(
        (
            item
            for item in dense
            if item.get("valid") and item["internal_id"] != original["internal_id"]
        ),
        key=lambda item: item["rule_score"],
        reverse=True,
    ):
        if item not in selection_pool:
            selection_pool.append(item)
    best = ranked_non_original[0] if ranked_non_original else None
    chosen: list[dict[str, Any]] = [original]
    if best:
        chosen.append(best)
    target_coverage = (
        min(0.84, float(best.get("coverage", 0.92)) - 0.06) if best else 0.84
    )
    tight = _choose_distinct(
        selection_pool, lambda item: float(item["coverage"]) <= target_coverage, chosen
    )
    if tight:
        chosen.append(tight)
    alternative = _choose_distinct(
        selection_pool,
        lambda item: item["kind"] in {"wide", "classic", "standard"},
        chosen,
    )
    if alternative:
        chosen.append(alternative)
    for item in selection_pool:
        if len(chosen) >= 4:
            break
        if not any(
            _iou(item["bounds"], existing["bounds"]) > 0.94 for existing in chosen
        ):
            chosen.append(item)

    aliases = [
        ("original", "原图"),
        ("balanced", "AI 推荐"),
        ("tight", "更紧"),
        ("wide", "宽幅/备选"),
    ]
    public: list[dict[str, Any]] = []
    internal_to_public: dict[str, str] = {}
    for index, item in enumerate(chosen[:4]):
        identifier, label = aliases[index]
        internal_to_public[item["internal_id"]] = identifier
        public.append(
            {
                "id": identifier,
                "label": label,
                "bounds": item["bounds"],
                "engine": "qwen3-vl-semantic-rank"
                if item.get("vlm_score") is not None
                else "semantic-rules",
                "confidence": round(
                    confidence
                    if index == 1
                    else float(item.get("score", item.get("rule_score", 0.0))),
                    4,
                ),
                "score": round(
                    float(item.get("score", item.get("rule_score", 0.0))), 4
                ),
                "aspect": item.get("aspect"),
                "coverage": item.get("coverage"),
                "rule_score": item.get("rule_score"),
                "vlm_score": item.get("vlm_score"),
                "reasons": item.get("reasons", []),
                "summary": summary if index == 1 else "",
                "metrics": item.get("metrics", {}),
            }
        )
    selected = "original"
    top = ranked[0] if ranked else original
    top_public = internal_to_public.get(top["internal_id"])
    original_score = float(original.get("score", original.get("rule_score", 0.66)))
    top_score = float(top.get("score", top.get("rule_score", 0.0)))
    if (
        top_public
        and top_public != "original"
        and confidence >= 0.55
        and top_score >= original_score + 0.025
    ):
        selected = top_public
    return public, selected


def _public_subjects(subjects: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "label",
        "score",
        "box",
        "strict",
        "source",
        "mask_box",
        "mask_area",
        "mask_score",
        "mask_source",
    )
    return [
        {
            key: subject[key]
            for key in keys
            if key in subject and subject[key] is not None
        }
        for subject in subjects
    ]


def smart_crop_candidates(
    image: Image.Image,
    run_dir: Path,
    *,
    mode: str | None = None,
    progress: SmartCropProgress | None = None,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    mode = (
        (mode or os.environ.get("PHOTO_AI_SMART_CROP_MODE", "full")).strip().casefold()
    )
    if mode not in {"full", "heuristic"}:
        mode = "full"
    data_dir = _data_dir_for_run(run_dir)
    digest = _image_digest(image)
    cache_root = Path(os.environ.get("PHOTO_AI_CACHE_DIR") or data_dir / "cache")
    cache_path = (
        cache_root / "smart-crop" / SMART_CROP_VERSION / digest[:2] / f"{digest}.json"
    )
    if cache_path.is_file():
        try:
            cached = read_json(cache_path)
            cache_is_healthy = mode != "full" or not cached.get("analysis", {}).get(
                "warnings"
            )
            if (
                cached.get("version") == SMART_CROP_VERSION
                and cached.get("mode") == mode
                and cache_is_healthy
            ):
                _report_progress(progress, "cache", "读取智能构图缓存")
                return cached["candidates"], cached["selected"], cached["analysis"]
        except (OSError, TypeError, ValueError, KeyError):
            pass

    scene = analyze_scene(image, data_dir, mode=mode, progress=progress)
    _report_progress(progress, "candidates", "生成并筛选构图候选")
    dense = generate_dense_candidates(image, scene)
    ranking_warning = None
    _report_progress(
        progress,
        "rank",
        "Qwen3-VL 复评候选构图" if mode == "full" else "规则复评候选构图",
    )
    try:
        ranked, rank_confidence, rank_summary = _rank_candidates(
            image, dense, scene, data_dir, mode=mode
        )
    except Exception as exc:  # noqa: BLE001 - candidate ranking has a deterministic fallback
        ranking_warning = f"候选重排降级：{type(exc).__name__}: {exc}"
        ranked = sorted(
            (item for item in dense if item.get("valid")),
            key=lambda item: item["rule_score"],
            reverse=True,
        )
        rank_confidence = 0.0
        rank_summary = "规则评分回退"
    candidates, selected = _public_candidates(
        dense, ranked, rank_confidence, rank_summary
    )
    warnings = list(scene.get("warnings", []))
    if ranking_warning:
        warnings.append(ranking_warning)
    analysis = {
        "version": SMART_CROP_VERSION,
        "mode": mode,
        "scene_type": scene.get("scene_type"),
        "composition": scene.get("composition"),
        "horizon_y": scene.get("horizon_y"),
        "horizon_confidence": scene.get("horizon_confidence"),
        "gaze_direction": scene.get("gaze_direction"),
        "negative_space_direction": scene.get("negative_space_direction"),
        "preferred_aspect": scene.get("preferred_aspect"),
        "summary": scene.get("summary"),
        "subjects": _public_subjects(scene.get("subjects", [])),
        "mask_count": int(scene.get("mask_count", 0)),
        "segmentation": scene.get("segmentation", {}),
        "engines": scene.get("engines", {}),
        "dense_candidate_count": len(dense),
        "valid_candidate_count": sum(bool(item.get("valid")) for item in dense),
        "rank_confidence": round(rank_confidence, 4),
        "rank_summary": rank_summary,
        "warnings": warnings,
    }
    write_json(
        cache_path,
        {
            "version": SMART_CROP_VERSION,
            "mode": mode,
            "candidates": candidates,
            "selected": selected,
            "analysis": analysis,
        },
    )
    return candidates, selected, analysis
