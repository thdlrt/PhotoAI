from __future__ import annotations

import gc
import io
import math
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image
from tqdm import tqdm

from .constants import FEATURE_VERSION
from .preview import load_preview, preview_cache_path
from .progress import emit_progress, phase_end, phase_start, progress_enabled
from .util import atomic_write_bytes, cache_key

DINO_DIMENSIONS = 768
MAX_PREVIEW_BATCH = 16

TECHNICAL_NAMES = [
    "sharpness_log",
    "brightness",
    "contrast",
    "shadow_clip",
    "highlight_clip",
    "saturation",
    "entropy",
    "aspect_log",
]


def technical_features(image: Image.Image) -> np.ndarray:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean() / 255.0)
    contrast = float(gray.std() / 128.0)
    shadow_clip = float((gray <= 4).mean())
    highlight_clip = float((gray >= 251).mean())
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    saturation = float(hsv[..., 1].mean() / 255.0)
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    prob = hist / max(1.0, hist.sum())
    entropy = float(-(prob[prob > 0] * np.log2(prob[prob > 0])).sum() / 8.0)
    aspect = math.log(
        max(image.width, image.height) / max(1, min(image.width, image.height))
    )
    return np.asarray(
        [
            math.log1p(sharpness),
            brightness,
            contrast,
            shadow_clip,
            highlight_clip,
            saturation,
            entropy,
            aspect,
        ],
        dtype=np.float32,
    )


class FeatureExtractor:
    def __init__(
        self, cache_dir: Path, device: str = "auto", use_dino: bool = True
    ) -> None:
        self.cache_dir = cache_dir
        self.preview_dir = cache_dir / "previews"
        self.feature_dir = cache_dir / "features"
        self.use_dino = use_dino
        self.device_name = device
        self._model = None
        self._processor = None
        self._torch = None
        self._inference_batch_limit = MAX_PREVIEW_BATCH
        self.failures: list[tuple[Path, str]] = []
        self.last_stats: dict[str, int] = {}

    @property
    def feature_version(self) -> str:
        return FEATURE_VERSION if self.use_dino else "technical-v1"

    def _ensure_model(self) -> None:
        if not self.use_dino or self._model is not None:
            return
        import torch
        from transformers import AutoImageProcessor, AutoModel

        from .model_resources import managed_hf_model_path

        device = self.device_name
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device_name = device
        self._torch = torch
        model_path = managed_hf_model_path("dinov2-base")
        self._processor = AutoImageProcessor.from_pretrained(
            model_path, local_files_only=True
        )
        self._model = (
            AutoModel.from_pretrained(model_path, local_files_only=True)
            .eval()
            .to(device)
        )

    def _cached_path(self, path: Path) -> Path:
        key = cache_key(path)
        return self.feature_dir / self.feature_version / key[:2] / f"{key}.npy"

    def _valid_vector(self, vector: np.ndarray) -> bool:
        dimensions = len(TECHNICAL_NAMES) + (DINO_DIMENSIONS if self.use_dino else 0)
        if not isinstance(vector, np.ndarray) or vector.shape != (dimensions,) or vector.dtype.kind != "f":
            return False
        if not np.isfinite(vector).all():
            return False
        return not self.use_dino or abs(float(np.linalg.norm(vector[8:])) - 1.0) < .01

    def _read_cached(self, cached: Path) -> np.ndarray | None:
        try:
            # Bound parser input before loading a disposable cache file.
            if cached.stat().st_size > 16 * 1024:
                return None
            vector = np.load(cached, allow_pickle=False)
            if not isinstance(vector, np.ndarray):
                vector.close()
                return None
            if not self._valid_vector(vector):
                return None
            with np.errstate(over="ignore", invalid="ignore"):
                vector = vector.astype(np.float32)
            return vector if self._valid_vector(vector) else None
        except (OSError, ValueError, EOFError):
            return None

    def _write_cached(self, cached: Path, vector: np.ndarray) -> None:
        if not self._valid_vector(vector):
            raise RuntimeError("相似特征包含无效数值或模型维度不匹配，未写入缓存。")
        with io.BytesIO() as buffer:
            np.save(buffer, vector, allow_pickle=False)
            atomic_write_bytes(cached, buffer.getvalue())

    def _infer_embeddings(self, images: list[Image.Image]) -> np.ndarray:
        assert self._processor is not None and self._torch is not None and self._model is not None
        inputs = self._processor(images=images, return_tensors="pt")
        inputs = {key: value.to(self.device_name) for key, value in inputs.items()}
        with self._torch.inference_mode():
            hidden = self._model(**inputs).last_hidden_state[:, 0]
            hidden = self._torch.nn.functional.normalize(hidden, dim=1)
        embeddings = hidden.float().cpu().numpy().astype(np.float32)
        if embeddings.shape != (len(images), DINO_DIMENSIONS) or not np.isfinite(embeddings).all():
            raise RuntimeError("DINOv2 返回了无效特征。")
        return embeddings

    def _embed_batch(self, images: list[Image.Image]) -> np.ndarray:
        self._ensure_model()
        if len(images) > self._inference_batch_limit:
            limit = self._inference_batch_limit
            return np.concatenate([self._embed_batch(images[start:start + limit])
                                   for start in range(0, len(images), limit)])
        try:
            return self._infer_embeddings(images)
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower() or len(images) <= 1:
                raise
        # Leave the exception scope before retrying so its traceback no longer
        # holds failed GPU tensors. Only this uncommitted batch is retried.
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()
        middle = len(images) // 2
        self._inference_batch_limit = min(self._inference_batch_limit, middle)
        return np.concatenate([self._embed_batch(images[:middle]), self._embed_batch(images[middle:])])

    def extract(
        self, paths: Iterable[Path], batch_size: int = 16
    ) -> tuple[np.ndarray, list[Path]]:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("特征提取批大小必须是正整数。")
        batch_size = min(batch_size, MAX_PREVIEW_BATCH) if self.use_dino else 1
        ordered = list(paths)
        output: list[np.ndarray | None] = [None] * len(ordered)
        pending: list[tuple[int, Path, Path, bool]] = []
        self.failures = []
        self.last_stats = {"cache_hits": 0, "cache_repairs": 0, "peak_previews": 0, "processed": 0}
        cached_count = 0
        phase_start("previews", "检查照片缓存", len(ordered), unit="张")
        for index, path in enumerate(
            tqdm(ordered, desc="读取预览", unit="张", disable=progress_enabled())
        ):
            try:
                cached = self._cached_path(path)
                vector = self._read_cached(cached)
                output[index] = vector
                if vector is not None:
                    cached_count += 1
                    # Keep complete feature hits, but repair missing/broken
                    # previews so a cached run never produces an empty gallery.
                    try:
                        with Image.open(preview_cache_path(path, self.preview_dir)) as preview:
                            preview.load()
                            self.last_stats["peak_previews"] = max(self.last_stats["peak_previews"], 1)
                        needs_preview = False
                    except (OSError, ValueError):
                        needs_preview = True
                    if needs_preview:
                        pending.append((index, path, cached, False))
                else:
                    self.last_stats["cache_repairs"] += int(cached.exists())
                    pending.append((index, path, cached, True))
            except OSError as exc:
                self.failures.append((path, f"{type(exc).__name__}: {exc}"))
            emit_progress(
                "previews",
                "检查照片缓存",
                index + 1,
                len(ordered),
                unit="张",
                cached=cached_count,
            )
        self.last_stats["cache_hits"] = cached_count
        phase_end("previews", "检查照片缓存", len(ordered), unit="张", cached=cached_count)

        phase_start(
            "features",
            "读取照片与相似特征",
            len(ordered),
            current=len(ordered) - len(pending),
            unit="张",
            cached=cached_count,
        )

        for start in tqdm(range(0, len(pending), batch_size), desc="相似特征", unit="批", disable=progress_enabled()):
            chunk = pending[start:start + batch_size]
            loaded: list[tuple[int, Path, Path, Image.Image, np.ndarray]] = []
            try:
                for index, path, cached, needs_features in chunk:
                    image = None
                    try:
                        image = load_preview(path, self.preview_dir)
                        self.last_stats["peak_previews"] = max(self.last_stats["peak_previews"], len(loaded) + 1)
                        if not needs_features:
                            continue
                        tech = technical_features(image)
                        loaded.append((index, path, cached, image, tech))
                        image = None  # Ownership transfers to the batch.
                    except Exception as exc:
                        output[index] = None
                        self.failures.append((path, f"{type(exc).__name__}: {exc}"))
                    finally:
                        if image is not None:
                            image.close()
                self.last_stats["peak_previews"] = max(self.last_stats["peak_previews"], len(loaded))
                embeddings = self._embed_batch([item[3] for item in loaded]) if loaded and self.use_dino else None
                for offset, (index, path, cached, _, tech) in enumerate(loaded):
                    vector = np.concatenate([tech, embeddings[offset]]).astype(np.float32) if embeddings is not None else tech
                    if self._cached_path(path) != cached:
                        raise RuntimeError(f"照片在处理期间发生变化，请重新运行：{path.name}")
                    self._write_cached(cached, vector)
                    output[index] = vector
                    self.last_stats["processed"] += 1
            finally:
                for _, _, _, image, _ in loaded:
                    image.close()
            emit_progress("features", "读取照片与相似特征",
                len(ordered) - len(pending) + min(start + len(chunk), len(pending)),
                len(ordered), unit="张", cached=cached_count)
        phase_end(
            "features", "读取照片与相似特征", len(ordered), unit="张", cached=cached_count
        )

        successful_indices = [
            index for index, item in enumerate(output) if item is not None
        ]
        if not successful_indices:
            details = "; ".join(f"{path}: {error}" for path, error in self.failures[:5])
            raise RuntimeError(f"所有照片的预览或特征提取均失败。{details}")
        if self.failures:
            tqdm.write(
                f"跳过 {len(self.failures)} 个无法读取的文件；首个错误：{self.failures[0][0]} — {self.failures[0][1]}"
            )
        matrix = np.stack([output[index] for index in successful_indices]).astype(
            np.float32
        )
        successful_paths = [ordered[index] for index in successful_indices]
        return matrix, successful_paths

    def release(self) -> None:
        """Release DINO before the next GPU stage on 16 GB cards."""

        self._model = None
        self._processor = None
        torch = self._torch
        self._torch = None
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
