from __future__ import annotations

import gc
import hashlib
import json
import os
import urllib.error
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image

from .constants import MODEL_ID, VLM_MODEL_ID
from .features import TECHNICAL_NAMES, FeatureExtractor
from .group_critic import OllamaGroupCritic
from .preview import load_preview
from .style_library import managed_style_root
from .util import cache_key, read_json, write_json

STYLE_SCENE_PROMPT_VERSION = "style-scene-v1"
STYLE_CLIP_INDEX_VERSION = "style-clip-v1"
DEFAULT_CLIP_MODEL_ID = "openai/clip-vit-base-patch32"


class AIStageUnavailable(RuntimeError):
    """A local AI stage cannot run without a model or runtime dependency."""


class DinoSelector(Protocol):
    model: str

    def select(self, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]: ...

    def release(self) -> None: ...


class SceneAnalyzer(Protocol):
    model: str

    def analyze(self, paths: Sequence[Path]) -> dict[str, Any]: ...

    def release(self) -> None: ...


class ClipRetriever(Protocol):
    model: str

    def score_candidates(
        self,
        representative: Path,
        scene: Mapping[str, Any] | None,
        entries: Sequence[Mapping[str, Any]],
        *,
        limit: int = 12,
    ) -> dict[str, float]: ...

    def release(self) -> None: ...


CascadeProgress = Callable[[str, str, int, int], None]


def _item_path(item: Mapping[str, Any]) -> str:
    return str(
        item.get("path")
        or item.get("source_path")
        or item.get("preview")
        or item.get("preview_path")
        or ""
    )


def _stage(
    status: str,
    model: str,
    *,
    used: bool,
    cached: bool = False,
    reason: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    result = {
        "status": status,
        "model": model,
        "used": bool(used),
        "cached": bool(cached),
    }
    if reason:
        result["reason"] = str(reason)[:500]
    result.update(details)
    return result


def _fallback_probes(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = [dict(item) for item in items]
    ordered.sort(key=_item_path)
    if not ordered:
        return {
            "representative": None,
            "brightest": None,
            "darkest": None,
            "basis": "empty",
        }
    brightness = [
        item
        for item in ordered
        if isinstance((item.get("technical") or {}).get("brightness"), (int, float))
    ]
    brightest = (
        max(brightness, key=lambda item: float(item["technical"]["brightness"]))
        if brightness
        else ordered[-1]
    )
    darkest = (
        min(brightness, key=lambda item: float(item["technical"]["brightness"]))
        if brightness
        else ordered[0]
    )
    return {
        "representative": _item_path(ordered[0]),
        "brightest": _item_path(brightest),
        "darkest": _item_path(darkest),
        "basis": "stable_fallback",
    }


def _local_hf_model_available(model_id: str) -> bool:
    """Check the configured Hugging Face cache without making a network call."""

    try:
        from transformers.utils.hub import cached_file

        return bool(cached_file(model_id, "config.json", local_files_only=True))
    except (ImportError, OSError, ValueError):
        return False


def _portable_storage_roots(data_dir: Path) -> tuple[Path, Path]:
    data_root = Path(data_dir).resolve()
    content_root_value = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    if not content_root_value:
        return data_root, data_root / "cache"
    content_root = Path(content_root_value).expanduser().resolve()
    cache_root = Path(
        os.environ.get("PHOTO_AI_CACHE_DIR", str(content_root / "cache"))
    ).expanduser().resolve()
    try:
        data_root.relative_to(content_root)
        cache_root.relative_to(content_root)
    except ValueError as exc:
        raise ValueError(
            "风格 AI 数据与缓存必须位于 PHOTO_AI_CONTENT_ROOT 内。"
        ) from exc
    return content_root, cache_root


class DinoMedoidSelector:
    """Select the cosine medoid from real, versioned DINOv2 photo features."""

    model = MODEL_ID

    def __init__(
        self,
        data_dir: Path,
        *,
        extractor_factory: Callable[..., FeatureExtractor] = FeatureExtractor,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.storage_root, self.cache_root = _portable_storage_roots(self.data_dir)
        self._extractor = extractor_factory(
            self.cache_root, device="auto", use_dino=True
        )

    def select(self, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        ordered = [dict(item) for item in items if _item_path(item)]
        ordered.sort(key=_item_path)
        if not ordered:
            raise ValueError("照片组为空。")
        paths = [Path(_item_path(item)).expanduser().resolve() for item in ordered]
        cached_count = sum(
            self._extractor._cached_path(path).is_file()
            for path in paths
        )
        if cached_count != len(paths) and not _local_hf_model_available(self.model):
            raise AIStageUnavailable(
                f"DINOv2 模型尚未安装：{self.model}。请在设置的资源页修复当前 AI 配置。"
            )
        matrix, successful_paths = self._extractor.extract(paths)
        technical_count = len(TECHNICAL_NAMES)
        if matrix.ndim != 2 or matrix.shape[1] <= technical_count:
            raise AIStageUnavailable("现有特征只有技术指标，没有 DINOv2 向量。")
        embedding = np.asarray(matrix[:, technical_count:], dtype=np.float32)
        norms = np.linalg.norm(embedding, axis=1, keepdims=True)
        if np.any(norms <= 1e-8):
            raise RuntimeError("DINOv2 返回了无效的零向量。")
        embedding /= norms
        similarity = embedding @ embedding.T
        medoid_index = int(np.argmax(similarity.mean(axis=1)))
        brightness = np.asarray(matrix[:, 1], dtype=np.float32)
        brightest_index = int(np.argmax(brightness))
        darkest_index = int(np.argmin(brightness))
        probes = {
            "representative": str(successful_paths[medoid_index]),
            "brightest": str(successful_paths[brightest_index]),
            "darkest": str(successful_paths[darkest_index]),
            "basis": "dinov2_medoid",
        }
        return {
            "probes": probes,
            "stage": _stage(
                "complete",
                self.model,
                used=True,
                cached=cached_count == len(paths),
                photo_count=len(paths),
                embedded_count=len(successful_paths),
                embedding_dimensions=int(embedding.shape[1]),
            ),
        }

    def release(self) -> None:
        self._extractor.release()


_SCENE_FIELDS = {
    "scene",
    "weather",
    "time_of_day",
    "subjects",
    "dominant_colors",
    "mood",
    "avoid_effects",
    "search_terms",
    "summary",
    "confidence",
}


def _scene_schema() -> dict[str, Any]:
    short_list = {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 8,
    }
    return {
        "type": "object",
        "properties": {
            "scene": {"type": "string"},
            "weather": {"type": "string"},
            "time_of_day": {"type": "string"},
            "subjects": short_list,
            "dominant_colors": short_list,
            "mood": short_list,
            "avoid_effects": short_list,
            "search_terms": short_list,
            "summary": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": sorted(_SCENE_FIELDS),
        "additionalProperties": False,
    }


def _validate_scene(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _SCENE_FIELDS:
        raise ValueError("Qwen3-VL 场景字段不完整。")
    result: dict[str, Any] = {}
    for key in ("scene", "weather", "time_of_day", "summary"):
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Qwen3-VL {key} 不能为空。")
        result[key] = value.strip()[:240]
    for key in (
        "subjects",
        "dominant_colors",
        "mood",
        "avoid_effects",
        "search_terms",
    ):
        value = payload[key]
        if not isinstance(value, list) or len(value) > 8:
            raise ValueError(f"Qwen3-VL {key} 必须是短列表。")
        if not all(isinstance(item, str) for item in value):
            raise ValueError(f"Qwen3-VL {key} 包含无效值。")
        result[key] = [item.strip()[:80] for item in value if item.strip()]
    confidence = float(payload["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("Qwen3-VL confidence 超出 0–1。")
    result["confidence"] = confidence
    return result


class QwenStyleSceneAnalyzer:
    """Generate a cached, structured look-selection brief with local Qwen3-VL."""

    model = VLM_MODEL_ID

    def __init__(
        self,
        data_dir: Path,
        *,
        critic_factory: Callable[..., OllamaGroupCritic] = OllamaGroupCritic,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.storage_root, self.cache_root = _portable_storage_roots(self.data_dir)
        self._critic = critic_factory(self.data_dir, model=self.model, max_pixels=768)
        self.cache_dir = (
            self.cache_root / "ai" / STYLE_SCENE_PROMPT_VERSION
        )
        self._ready = False

    @staticmethod
    def _prompt(count: int) -> str:
        return (
            f"你是风光摄影创意调色编辑。输入是同一照片组的 {count} 张代表图，依次为组内 DINOv2 中心图、"
            "最亮图和最暗图（重复项已移除）。只描述所有图片共同、可观察的场景特征。"
            "scene 写场景类型，weather 写天气，time_of_day 写时段；subjects、dominant_colors、mood 写短词。"
            "avoid_effects 写会破坏高光、阴影、肤色或自然感的效果；search_terms 必须写 4–8 个英文摄影风格检索词，"
            "用于 CLIP 检索，不写品牌和不存在的地点。summary 用一句中文。严格按 JSON Schema 返回。"
        )

    def _cache_path(self, paths: Sequence[Path]) -> Path:
        payload = {
            "model": self.model,
            "prompt": STYLE_SCENE_PROMPT_VERSION,
            "photos": [cache_key(path) for path in paths],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def analyze(self, paths: Sequence[Path]) -> dict[str, Any]:
        unique: list[Path] = []
        seen: set[str] = set()
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            key = str(path).casefold()
            if key not in seen:
                seen.add(key)
                unique.append(path)
        if not unique:
            raise ValueError("Qwen3-VL 没有收到代表图。")
        unique = unique[:3]
        cached_path = self._cache_path(unique)
        if cached_path.is_file():
            try:
                cached = read_json(cached_path)
                if (
                    cached.get("model") == self.model
                    and cached.get("prompt_version") == STYLE_SCENE_PROMPT_VERSION
                ):
                    return {"scene": _validate_scene(cached.get("scene")), "cached": True}
            except (OSError, TypeError, ValueError):
                pass
        if not self._ready:
            self._critic.ensure_ready()
            self._ready = True
        images = [self._critic._image(path) for path in unique]
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": self._prompt(len(unique)),
                    "images": images,
                }
            ],
            "stream": False,
            "think": False,
            "format": _scene_schema(),
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 900},
            "keep_alive": "2m",
        }
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                response = self._critic._request(
                    "/api/chat", payload, timeout=900.0
                )
                content = response["message"]["content"]
                parsed = json.loads(content) if isinstance(content, str) else content
                scene = _validate_scene(parsed)
                write_json(
                    cached_path,
                    {
                        "model": self.model,
                        "prompt_version": STYLE_SCENE_PROMPT_VERSION,
                        "scene": scene,
                    },
                )
                return {"scene": scene, "cached": False}
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                urllib.error.URLError,
            ) as exc:
                last_error = exc
        raise RuntimeError(f"Qwen3-VL 连续两次未返回有效风格场景：{last_error}")

    def release(self) -> None:
        if self._ready:
            self._critic.unload()
        self._ready = False


def _flatten_text(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            result.append(str(key))
            result.extend(_flatten_text(nested))
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            result.extend(_flatten_text(nested))
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        result.append(str(value))
    return result


def style_entry_text(entry: Mapping[str, Any]) -> str:
    """Build CLIP text from actual LUT/Look metadata, not a fixed category only."""

    keys = (
        "name",
        "label",
        "group",
        "category",
        "source",
        "description",
        "tags",
        "style_terms",
        "look_kind",
        "profile_name",
        "profile_hash",
        "lut_path",
        "look_path",
        "relative_path",
        "calibration",
    )
    values: list[str] = []
    for key in keys:
        if key in entry:
            values.extend(_flatten_text(entry.get(key)))
    return "photographic creative look, " + ", ".join(values)


def scene_query_text(scene: Mapping[str, Any] | None) -> str:
    if not scene:
        return "natural landscape photography creative color grade"
    values: list[str] = []
    for key in (
        "scene",
        "weather",
        "time_of_day",
        "subjects",
        "dominant_colors",
        "mood",
        "search_terms",
        "summary",
    ):
        values.extend(_flatten_text(scene.get(key)))
    return "landscape photography, " + ", ".join(values)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _clip_feature_tensor(output: Any) -> Any:
    """Normalize Transformers 4.x tensor and 5.x pooled-output APIs."""

    pooled = getattr(output, "pooler_output", None)
    return pooled if pooled is not None else output


class LocalClipStyleRetriever:
    """Recall real looks in CLIP space using representative image and metadata."""

    def __init__(
        self,
        data_dir: Path,
        *,
        model: str | None = None,
        device: str = "auto",
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.storage_root, self.cache_root = _portable_storage_roots(self.data_dir)
        self.model = model or os.environ.get(
            "PHOTO_AI_CLIP_MODEL", DEFAULT_CLIP_MODEL_ID
        )
        self.device = device
        self.cache_dir = self.cache_root / "ai" / STYLE_CLIP_INDEX_VERSION
        self._torch: Any = None
        self._model: Any = None
        self._processor: Any = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        if not _local_hf_model_available(self.model):
            raise AIStageUnavailable(
                f"CLIP 模型尚未安装：{self.model}。请在设置的资源页修复当前 AI 配置。"
            )
        import torch
        from transformers import CLIPModel, CLIPProcessor

        device = self.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = device
        self._torch = torch
        self._processor = CLIPProcessor.from_pretrained(
            self.model, local_files_only=True
        )
        self._model = (
            CLIPModel.from_pretrained(self.model, local_files_only=True)
            .eval()
            .to(device)
        )

    @staticmethod
    def _normalize(values: np.ndarray) -> np.ndarray:
        array = np.asarray(values, dtype=np.float32)
        norm = np.linalg.norm(array, axis=-1, keepdims=True)
        return array / np.maximum(norm, 1e-8)

    def _text_features(self, texts: Sequence[str]) -> np.ndarray:
        assert self._torch is not None and self._model is not None
        assert self._processor is not None
        output: list[np.ndarray] = []
        for start in range(0, len(texts), 64):
            inputs = self._processor(
                text=list(texts[start : start + 64]),
                padding=True,
                truncation=True,
                return_tensors="pt",
            )
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with self._torch.inference_mode():
                features = _clip_feature_tensor(
                    self._model.get_text_features(**inputs)
                )
            output.append(features.float().cpu().numpy())
        return self._normalize(np.concatenate(output, axis=0))

    def _image_features(self, images: Sequence[Image.Image]) -> np.ndarray:
        assert self._torch is not None and self._model is not None
        assert self._processor is not None
        output: list[np.ndarray] = []
        for start in range(0, len(images), 16):
            inputs = self._processor(
                images=list(images[start : start + 16]), return_tensors="pt"
            )
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with self._torch.inference_mode():
                features = _clip_feature_tensor(
                    self._model.get_image_features(**inputs)
                )
            output.append(features.float().cpu().numpy())
        return self._normalize(np.concatenate(output, axis=0))

    def _entry_preview(self, entry: Mapping[str, Any]) -> Path | None:
        calibration = (
            entry.get("calibration")
            if isinstance(entry.get("calibration"), Mapping)
            else {}
        )
        values = [
            entry.get("preview_path"),
            entry.get("calibration_preview"),
            entry.get("look_preview"),
            calibration.get("preview_path"),
        ]
        for value in values:
            if not value:
                continue
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                path = managed_style_root(self.data_dir) / path
            try:
                path = path.resolve(strict=True)
            except OSError:
                continue
            if (
                _inside(path, self.storage_root)
                and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}
            ):
                return path
        return None

    def _entry_cache_path(self, entry: Mapping[str, Any], text: str) -> Path:
        preview = self._entry_preview(entry)
        preview_key = cache_key(preview) if preview else "text-only"
        payload = "|".join(
            (
                self.model,
                str(
                    entry.get("preset_id")
                    or entry.get("profile_id")
                    or entry.get("lut_id")
                    or ""
                ),
                str(entry.get("file_hash") or ""),
                preview_key,
                text,
            )
        )
        digest = hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()
        model_key = hashlib.sha256(self.model.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / model_key / digest[:2] / f"{digest}.npy"

    def _entry_features(
        self, entries: Sequence[Mapping[str, Any]]
    ) -> tuple[np.ndarray, list[str]]:
        texts = [style_entry_text(entry) for entry in entries]
        cached: list[np.ndarray | None] = []
        missing: list[int] = []
        for index, (entry, text) in enumerate(zip(entries, texts, strict=True)):
            path = self._entry_cache_path(entry, text)
            try:
                feature = np.load(path, allow_pickle=False) if path.is_file() else None
                if feature is None or feature.ndim != 1:
                    raise ValueError
            except (OSError, ValueError):
                feature = None
                missing.append(index)
            cached.append(feature)
        if missing:
            text_features = self._text_features([texts[index] for index in missing])
            preview_indices: list[int] = []
            preview_images: list[Image.Image] = []
            for local_index, entry_index in enumerate(missing):
                preview = self._entry_preview(entries[entry_index])
                if preview is None:
                    continue
                with Image.open(preview) as source:
                    preview_images.append(source.convert("RGB"))
                preview_indices.append(local_index)
            if preview_images:
                image_features = self._image_features(preview_images)
                for image_index, local_index in enumerate(preview_indices):
                    text_features[local_index] = self._normalize(
                        0.40 * text_features[local_index]
                        + 0.60 * image_features[image_index]
                    )
            for local_index, entry_index in enumerate(missing):
                feature = np.asarray(text_features[local_index], dtype=np.float32)
                cached[entry_index] = feature
                path = self._entry_cache_path(entries[entry_index], texts[entry_index])
                path.parent.mkdir(parents=True, exist_ok=True)
                np.save(path, feature, allow_pickle=False)
        matrix = np.stack([np.asarray(value) for value in cached]).astype(np.float32)
        return self._normalize(matrix), texts

    @staticmethod
    def _avoid_penalty(text: str, scene: Mapping[str, Any] | None) -> float:
        if not scene:
            return 0.0
        lowered = text.casefold()
        terms = [
            str(value).casefold()
            for value in scene.get("avoid_effects", [])
            if isinstance(value, str)
        ]
        matches = sum(term in lowered for term in terms if len(term) >= 3)
        return min(0.18, 0.06 * matches)

    def score_candidates(
        self,
        representative: Path,
        scene: Mapping[str, Any] | None,
        entries: Sequence[Mapping[str, Any]],
        *,
        limit: int = 12,
    ) -> dict[str, float]:
        if not entries:
            return {}
        self._ensure_model()
        assert self._model is not None
        raw = Path(representative).expanduser().resolve(strict=True)
        preview = load_preview(raw, self.cache_root / "previews")
        image_feature = self._image_features([preview.convert("RGB")])[0]
        text_feature = self._text_features([scene_query_text(scene)])[0]
        query = self._normalize(0.62 * image_feature + 0.38 * text_feature)
        entry_features, texts = self._entry_features(entries)
        cosine = entry_features @ query
        scored: list[tuple[str, float]] = []
        for entry, text, similarity in zip(entries, texts, cosine, strict=True):
            preset_id = str(
                entry.get("preset_id")
                or entry.get("profile_id")
                or entry.get("lut_id")
                or ""
            )
            if not preset_id:
                continue
            score = min(
                1.0,
                max(
                    0.0,
                    (float(similarity) + 1.0) / 2.0
                    - self._avoid_penalty(text, scene),
                ),
            )
            scored.append((preset_id, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return {
            preset_id: round(score, 8)
            for preset_id, score in scored[: max(0, min(12, int(limit)))]
        }

    def release(self) -> None:
        self._model = None
        self._processor = None
        torch = self._torch
        self._torch = None
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()


def _aggregate_stage(
    model: str, records: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    used = sum(bool(record.get("used")) for record in records)
    if records and used == len(records):
        status = "complete"
    elif used:
        status = "degraded"
    else:
        status = "unavailable"
    reasons = sorted(
        {
            str(record.get("reason"))
            for record in records
            if record.get("reason")
        }
    )
    result = _stage(
        status,
        model,
        used=bool(used),
        cached=bool(records) and all(bool(record.get("cached")) for record in records),
        used_groups=used,
        group_count=len(records),
    )
    if reasons:
        result["reasons"] = reasons
    return result


def run_style_ai_cascade(
    groups: Mapping[str, Sequence[Mapping[str, Any]]],
    catalog_entries: Sequence[Mapping[str, Any]],
    data_dir: Path,
    *,
    seed_scenes: Mapping[str, Mapping[str, Any]] | None = None,
    dino_selector: DinoSelector | None = None,
    scene_analyzer: SceneAnalyzer | None = None,
    clip_retriever: ClipRetriever | None = None,
    progress: CascadeProgress | None = None,
) -> dict[str, Any]:
    """Run DINO -> Qwen3-VL -> CLIP with explicit, per-stage provenance.

    Failures are deliberately visible in ``stages`` and ``missing_stages``.
    The caller may continue with deterministic scene/name recall, but it can no
    longer present that fallback as a completed CLIP recommendation.
    """

    data_root = Path(data_dir).resolve()
    normalized_groups = {str(key): value for key, value in groups.items()}
    seed_scenes = {str(key): value for key, value in (seed_scenes or {}).items()}
    selector = dino_selector or DinoMedoidSelector(data_root)
    analyzer = scene_analyzer or QwenStyleSceneAnalyzer(data_root)
    retriever = clip_retriever or LocalClipStyleRetriever(data_root)
    ordered_ids = sorted(normalized_groups, key=str)
    output: dict[str, dict[str, Any]] = {
        group_id: {"stages": {}} for group_id in ordered_ids
    }

    try:
        for index, group_id in enumerate(ordered_ids, start=1):
            items = normalized_groups[group_id]
            try:
                selected = selector.select(items)
                output[group_id]["probes"] = dict(selected["probes"])
                output[group_id]["stages"]["dinov2"] = dict(selected["stage"])
            # Model libraries expose backend-specific exceptions (LibRaw,
            # CUDA, HTTP). This is the deliberate degradation boundary; never
            # let one optional AI stage abort exact Lightroom preview work.
            except Exception as exc:  # noqa: BLE001
                output[group_id]["probes"] = _fallback_probes(items)
                output[group_id]["stages"]["dinov2"] = _stage(
                    "unavailable" if isinstance(exc, AIStageUnavailable) else "degraded",
                    getattr(selector, "model", MODEL_ID),
                    used=False,
                    reason=str(exc) or type(exc).__name__,
                    fallback="stable_path",
                )
            if progress:
                progress("representative", "DINOv2 选择组内代表图", index, len(ordered_ids))
    finally:
        selector.release()

    try:
        for index, group_id in enumerate(ordered_ids, start=1):
            probes = output[group_id]["probes"]
            paths = [
                Path(value)
                for value in (
                    probes.get("representative"),
                    probes.get("brightest"),
                    probes.get("darkest"),
                )
                if value
            ]
            try:
                analyzed = analyzer.analyze(paths)
                output[group_id]["scene"] = dict(analyzed["scene"])
                output[group_id]["stages"]["qwen3_vl"] = _stage(
                    "complete",
                    getattr(analyzer, "model", VLM_MODEL_ID),
                    used=True,
                    cached=bool(analyzed.get("cached")),
                    photo_count=len({str(path).casefold() for path in paths}),
                )
            except Exception as exc:  # noqa: BLE001
                fallback = seed_scenes.get(group_id)
                output[group_id]["scene"] = dict(fallback) if fallback else None
                output[group_id]["stages"]["qwen3_vl"] = _stage(
                    "degraded" if fallback else "unavailable",
                    getattr(analyzer, "model", VLM_MODEL_ID),
                    used=False,
                    reason=str(exc) or type(exc).__name__,
                    fallback="existing_scene_metadata" if fallback else "none",
                )
            if progress:
                progress("scene", "Qwen3-VL 分析场景与禁用效果", index, len(ordered_ids))
    finally:
        analyzer.release()

    try:
        for index, group_id in enumerate(ordered_ids, start=1):
            representative = Path(output[group_id]["probes"]["representative"])
            try:
                scores = retriever.score_candidates(
                    representative,
                    output[group_id].get("scene"),
                    catalog_entries,
                    limit=12,
                )
                if not scores:
                    raise RuntimeError("CLIP 没有召回任何可用 LUT/Look。")
                output[group_id]["clip_scores"] = dict(scores)
                output[group_id]["stages"]["clip"] = _stage(
                    "complete",
                    getattr(retriever, "model", DEFAULT_CLIP_MODEL_ID),
                    used=True,
                    candidate_count=len(scores),
                    catalog_count=len(catalog_entries),
                )
            except Exception as exc:  # noqa: BLE001
                output[group_id]["clip_scores"] = {}
                output[group_id]["stages"]["clip"] = _stage(
                    "unavailable" if isinstance(exc, AIStageUnavailable) else "degraded",
                    getattr(retriever, "model", DEFAULT_CLIP_MODEL_ID),
                    used=False,
                    reason=str(exc) or type(exc).__name__,
                    fallback="scene_metadata_recall",
                )
            if progress:
                progress("recall", "CLIP 召回创意外观", index, len(ordered_ids))
    finally:
        retriever.release()

    stage_names = ("dinov2", "qwen3_vl", "clip")
    stages: dict[str, dict[str, Any]] = {}
    for name in stage_names:
        records = [output[group_id]["stages"][name] for group_id in ordered_ids]
        model = str(records[0].get("model") or name) if records else name
        stages[name] = _aggregate_stage(model, records)
    return {"groups": output, "stages": stages}
