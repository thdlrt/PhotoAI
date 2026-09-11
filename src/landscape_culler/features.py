from __future__ import annotations

import gc
import math
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image
from tqdm import tqdm

from .constants import FEATURE_VERSION
from .preview import load_preview
from .progress import emit_progress, phase_end, phase_start, progress_enabled
from .util import cache_key

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

    def extract(
        self, paths: Iterable[Path], batch_size: int = 16
    ) -> tuple[np.ndarray, list[Path]]:
        ordered = list(paths)
        output: list[np.ndarray | None] = [None] * len(ordered)
        missing: list[tuple[int, Path, Image.Image, np.ndarray]] = []

        failures: list[tuple[Path, str]] = []
        cached_count = 0
        phase_start("previews", "读取照片", len(ordered), unit="张")
        for index, path in enumerate(
            tqdm(ordered, desc="读取预览", unit="张", disable=progress_enabled())
        ):
            cached = self._cached_path(path)
            if cached.exists():
                output[index] = np.load(cached)
                cached_count += 1
            else:
                try:
                    image = load_preview(path, self.preview_dir)
                    tech = technical_features(image)
                    missing.append((index, path, image, tech))
                except (
                    Exception
                ) as exc:  # A single old/corrupt RAW must not abort a long NAS scan.
                    failures.append((path, f"{type(exc).__name__}: {exc}"))
            emit_progress(
                "previews",
                "读取照片",
                index + 1,
                len(ordered),
                unit="张",
                cached=cached_count,
            )
        phase_end("previews", "读取照片", len(ordered), unit="张", cached=cached_count)

        phase_start(
            "features",
            "生成相似特征",
            len(ordered),
            current=cached_count,
            unit="张",
            cached=cached_count,
        )

        if missing and self.use_dino:
            self._ensure_model()
            assert (
                self._model is not None
                and self._processor is not None
                and self._torch is not None
            )
            for start in tqdm(
                range(0, len(missing), batch_size),
                desc="DINOv2 特征",
                unit="批",
                disable=progress_enabled(),
            ):
                chunk = missing[start : start + batch_size]
                images = [item[2] for item in chunk]
                inputs = self._processor(images=images, return_tensors="pt")
                inputs = {
                    key: value.to(self.device_name) for key, value in inputs.items()
                }
                with self._torch.inference_mode():
                    hidden = self._model(**inputs).last_hidden_state[:, 0]
                    hidden = self._torch.nn.functional.normalize(hidden, dim=1)
                embeddings = hidden.float().cpu().numpy().astype(np.float32)
                for item, embedding in zip(chunk, embeddings, strict=True):
                    index, path, _, tech = item
                    vector = np.concatenate([tech, embedding]).astype(np.float32)
                    output[index] = vector
                    cached = self._cached_path(path)
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    np.save(cached, vector)
                emit_progress(
                    "features",
                    "生成相似特征",
                    cached_count + min(start + len(chunk), len(missing)),
                    len(ordered),
                    unit="张",
                    cached=cached_count,
                )
        else:
            for missing_index, (index, path, _, tech) in enumerate(missing, start=1):
                output[index] = tech
                cached = self._cached_path(path)
                cached.parent.mkdir(parents=True, exist_ok=True)
                np.save(cached, tech)
                emit_progress(
                    "features",
                    "生成相似特征",
                    cached_count + missing_index,
                    len(ordered),
                    unit="张",
                    cached=cached_count,
                )
        phase_end(
            "features", "生成相似特征", len(ordered), unit="张", cached=cached_count
        )

        successful_indices = [
            index for index, item in enumerate(output) if item is not None
        ]
        if not successful_indices:
            details = "; ".join(f"{path}: {error}" for path, error in failures[:5])
            raise RuntimeError(f"所有照片的预览或特征提取均失败。{details}")
        if failures:
            tqdm.write(
                f"跳过 {len(failures)} 个无法读取的文件；首个错误：{failures[0][0]} — {failures[0][1]}"
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
