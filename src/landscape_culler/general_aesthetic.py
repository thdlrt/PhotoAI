from __future__ import annotations

import gc
import hashlib
import math
import os
from pathlib import Path
from typing import Any, Iterable

from tqdm import tqdm

from .constants import AESTHETIC_MODEL_ID, AESTHETIC_PIPELINE_VERSION, PREVIEW_VERSION
from .preview import load_preview, preview_cache_path
from .progress import emit_progress, phase_end, phase_start, progress_enabled
from .util import cache_key, read_json, write_json


class GeneralAestheticScorer:
    """Q-ReAlign quality/aesthetic scores with resumable per-photo caching."""

    def __init__(self, data_dir: Path, device: str = "auto") -> None:
        self.data_dir = data_dir
        cache_root = Path(os.environ.get("PHOTO_AI_CACHE_DIR") or data_dir / "cache")
        self.preview_dir = cache_root / "previews"
        self.cache_dir = cache_root / "ai" / AESTHETIC_PIPELINE_VERSION
        self.device = device
        self._metric: Any = None
        self._torch: Any = None

    def _cache_path(self, path: Path) -> Path:
        key = hashlib.sha256(
            f"{AESTHETIC_PIPELINE_VERSION}|{PREVIEW_VERSION}|{cache_key(path)}".encode(
                "utf-8"
            )
        ).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.json"

    @staticmethod
    def _valid(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("pipeline_version") != AESTHETIC_PIPELINE_VERSION:
            return False
        if (
            payload.get("model") != AESTHETIC_MODEL_ID
            or payload.get("preview_version") != PREVIEW_VERSION
        ):
            return False
        try:
            values = (float(payload["quality"]), float(payload["aesthetic"]))
        except (KeyError, TypeError, ValueError):
            return False
        return all(
            math.isfinite(value) and -100.0 <= value <= 100.0 for value in values
        )

    def _cached(self, path: Path) -> dict[str, Any] | None:
        cached = self._cache_path(path)
        if not cached.is_file():
            return None
        try:
            payload = read_json(cached)
        except (OSError, ValueError):
            return None
        return payload if self._valid(payload) else None

    def _ensure_metric(self) -> None:
        if self._metric is not None:
            return
        try:
            import pyiqa
            import torch
        except ImportError as exc:
            raise RuntimeError(
                "Q-ReAlign 尚未安装，请在“设置 → 资源”安装或修复当前模型套装。"
            ) from exc
        device = self.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            raise RuntimeError("Q-ReAlign 需要可用的 NVIDIA GPU；当前未检测到 CUDA。")
        self.device = device
        self._torch = torch
        from .model_resources import managed_hf_model_path

        model_path = managed_hf_model_path("qrealign-mini")
        # In pyiqa 0.1.16, `qrealign` maps to Q-ReAlign Mini (0.8B).
        self._metric = pyiqa.create_metric(
            AESTHETIC_MODEL_ID, device=device, model=str(model_path)
        )

    @staticmethod
    def _scalar(value: Any) -> float:
        if hasattr(value, "detach"):
            value = value.detach().float().cpu().reshape(-1)[0].item()
        result = float(value)
        if not math.isfinite(result):
            raise RuntimeError("Q-ReAlign 返回了无效数值。")
        return result

    def score(self, paths: Iterable[Path]) -> list[dict[str, Any]]:
        ordered = list(paths)
        output: list[dict[str, Any] | None] = [None] * len(ordered)
        missing: list[tuple[int, Path]] = []
        phase_start("aesthetic", "通用审美评分", len(ordered), unit="张")
        for index, path in enumerate(ordered):
            cached = self._cached(path)
            if cached is None:
                missing.append((index, path))
            else:
                output[index] = cached

        cached_count = len(ordered) - len(missing)
        emit_progress(
            "aesthetic",
            "通用审美评分",
            cached_count,
            len(ordered),
            unit="张",
            cached=cached_count,
        )

        if missing:
            self._ensure_metric()
            assert self._metric is not None
            for missing_index, (index, path) in enumerate(
                tqdm(
                    missing, desc="通用审美评分", unit="张", disable=progress_enabled()
                ),
                start=1,
            ):
                preview = preview_cache_path(path, self.preview_dir)
                if not preview.is_file():
                    load_preview(path, self.preview_dir)
                try:
                    quality = self._scalar(self._metric(str(preview), task_="quality"))
                    aesthetic = self._scalar(
                        self._metric(str(preview), task_="aesthetic")
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Q-ReAlign 评分失败：{path.name} — {exc}"
                    ) from exc
                payload = {
                    "model": AESTHETIC_MODEL_ID,
                    "pipeline_version": AESTHETIC_PIPELINE_VERSION,
                    "preview_version": PREVIEW_VERSION,
                    "quality": quality,
                    "aesthetic": aesthetic,
                }
                write_json(self._cache_path(path), payload)
                output[index] = payload
                emit_progress(
                    "aesthetic",
                    "通用审美评分",
                    cached_count + missing_index,
                    len(ordered),
                    unit="张",
                    cached=cached_count,
                )

        if any(item is None for item in output):
            raise AssertionError("通用审美评分结果不完整。")
        phase_end(
            "aesthetic", "通用审美评分", len(ordered), unit="张", cached=cached_count
        )
        return [item for item in output if item is not None]

    def release(self) -> None:
        self._metric = None
        torch = self._torch
        self._torch = None
        gc.collect()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
