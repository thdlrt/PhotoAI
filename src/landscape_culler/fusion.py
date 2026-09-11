from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import numpy as np

FAST_WEIGHTS = {
    # Preserve the relative contribution of the remaining signals after the
    # retired fourth component is removed.
    "aesthetic": 11.0 / 17.0,
    "quality": 4.0 / 17.0,
    "technical": 2.0 / 17.0,
}

DEEP_WEIGHTS = {
    "vlm": 11.0 / 17.0,
    "aesthetic": 3.0 / 17.0,
    "quality": 2.0 / 17.0,
    "technical": 1.0 / 17.0,
}


def _external_01(value: float) -> float:
    """Normalize common 0–1, 1–5, 0–10, or 0–100 score ranges."""

    value = float(value)
    if 0.0 <= value <= 1.05:
        return float(np.clip(value, 0.0, 1.0))
    if value <= 5.5:
        return float(np.clip((value - 1.0) / 4.0, 0.0, 1.0))
    if value <= 10.5:
        return float(np.clip(value / 10.0, 0.0, 1.0))
    return float(np.clip(value / 100.0, 0.0, 1.0))


def group_normalize(values: Iterable[float], groups: list[list[int]]) -> np.ndarray:
    """Continuous technical calibration without rank-step amplification."""

    array = np.asarray(list(values), dtype=np.float64)
    if not len(array):
        return array
    median = float(np.median(array))
    q25, q75 = np.quantile(array, [0.25, 0.75])
    scale = float(max(q75 - q25, np.std(array), 1e-6))
    absolute = 1.0 / (1.0 + np.exp(-np.clip((array - median) / scale, -12.0, 12.0)))
    result = absolute.copy()
    for indices in groups:
        if len(indices) < 2:
            continue
        idx = np.asarray(indices, dtype=np.int64)
        center = float(np.median(array[idx]))
        # The global scale floor ensures tiny model noise inside a burst cannot
        # become a near 0-vs-1 rank jump.
        within_scale = max(scale * 0.50, 1e-3)
        relative = 1.0 / (1.0 + np.exp(-np.clip((array[idx] - center) / within_scale, -12.0, 12.0)))
        result[idx] = 0.70 * absolute[idx] + 0.30 * relative
    return np.clip(result, 0.0, 1.0)


def technical_quality(features: np.ndarray, groups: list[list[int]]) -> np.ndarray:
    tech = np.asarray(features[:, :8], dtype=np.float64)
    sharp = group_normalize(tech[:, 0], groups)
    contrast = np.clip(tech[:, 2] / 1.2, 0.0, 1.0)
    entropy = np.clip(tech[:, 6], 0.0, 1.0)
    exposure = np.clip(1.0 - np.abs(tech[:, 1] - 0.48) / 0.48, 0.0, 1.0)
    clipping = np.clip(1.0 - 5.0 * tech[:, 3] - 8.0 * tech[:, 4], 0.0, 1.0)
    return np.clip(0.32 * sharp + 0.18 * contrast + 0.18 * entropy + 0.17 * exposure + 0.15 * clipping, 0.0, 1.0)


def vlm_dimension_score(item: dict[str, Any]) -> float:
    """Deterministic visual score; never trust a model's contradictory rank."""

    return float(np.clip((
        0.25 * float(item["composition"])
        + 0.20 * float(item["light"])
        + 0.15 * float(item["subject_layers"])
        + 0.12 * float(item["color"])
        + 0.08 * float(item["technical_quality"])
        + 0.12 * float(item["edit_potential"])
        + 0.08 * (100.0 - float(item["distraction"]))
    ) / 100.0, 0.0, 1.0))


def fuse_scores(
    features: np.ndarray,
    groups: list[list[int]],
    general: list[dict[str, Any]],
    *,
    mode: str,
    critiques: dict[int, dict[str, Any]] | None = None,
) -> tuple[np.ndarray, list[dict[str, float]], list[dict[str, Any]]]:
    technical = technical_quality(features, groups)
    aesthetic_raw = np.asarray([_external_01(item["aesthetic"]) for item in general])
    quality_raw = np.asarray([_external_01(item["quality"]) for item in general])
    # Q-ReAlign is already calibrated to 0–1. Preserve the absolute values:
    # exact within-group ranks would turn harmless 0.001 noise into a 0.65 gap.
    aesthetic = np.clip(aesthetic_raw, 0.0, 1.0)
    quality = np.clip(quality_raw, 0.0, 1.0)
    components: list[dict[str, float]] = []
    reasons: list[dict[str, Any]] = []
    final = np.zeros(len(features), dtype=np.float64)
    for index in range(len(features)):
        base = {
            "technical": float(technical[index]),
            "aesthetic": float(aesthetic[index]),
            "quality": float(quality[index]),
        }
        if mode == "deep":
            if critiques is None or index not in critiques:
                raise RuntimeError(f"深度评分缺少第 {index + 1} 张照片的视觉评审。")
            critique = critiques[index]
            vlm = vlm_dimension_score(critique)
            base["vlm"] = vlm
            base.update({
                "composition": float(critique["composition"]) / 100.0,
                "light": float(critique["light"]) / 100.0,
                "subject_layers": float(critique["subject_layers"]) / 100.0,
                "color": float(critique["color"]) / 100.0,
                "technical_quality": float(critique["technical_quality"]) / 100.0,
                "edit_potential": float(critique["edit_potential"]) / 100.0,
                "distraction": float(critique["distraction"]) / 100.0,
            })
            final[index] = sum(DEEP_WEIGHTS[name] * base[name] for name in DEEP_WEIGHTS)
            reasons.append({
                "summary": str(critique["summary"]),
                "strengths": list(critique.get("strengths", []))[:3],
                "issues": list(critique.get("issues", []))[:3],
                "confidence": float(critique["confidence"]),
            })
        else:
            final[index] = sum(FAST_WEIGHTS[name] * base[name] for name in FAST_WEIGHTS)
            strongest = max(
                ("审美", base["aesthetic"]),
                ("画质", base["quality"]),
                ("技术", base["technical"]),
                key=lambda item: item[1],
            )[0]
            weakest = min(("审美", base["aesthetic"]), ("画质", base["quality"]), ("技术", base["technical"]), key=lambda item: item[1])[0]
            reasons.append({
                "summary": f"{strongest}信号较强；{weakest}项相对保守。",
                "strengths": [],
                "issues": [],
                "confidence": None,
            })
        if not math.isfinite(float(final[index])):
            raise RuntimeError("融合评分出现无效数值。")
        components.append({key: round(float(value), 6) for key, value in base.items()})
    return np.clip(final, 0.0, 1.0), components, reasons
