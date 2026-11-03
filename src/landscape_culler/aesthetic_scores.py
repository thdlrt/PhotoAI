"""The Q-ReAlign model contract shared by selection and appearance ranking."""
from collections.abc import Mapping
import math
from numbers import Real
from typing import Any


def qrealign_score(value: Any, field: str = "评分") -> float:
    # Softmax and the weighted sum run in float32. Allow rounding at the two
    # endpoints, but never infer a different scale from an individual value.
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"Q-ReAlign {field}必须是 0–1 的有限数值。")
    try:
        score = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Q-ReAlign {field}必须是 0–1 的有限数值。") from exc
    if not math.isfinite(score) or not -1e-6 <= score <= 1.0 + 1e-6:
        raise ValueError(f"Q-ReAlign {field}超出 0–1 范围：{score!r}。")
    return min(1.0, max(0.0, score))


def qrealign_scores(record: Any) -> dict[str, float]:
    if not isinstance(record, Mapping) or not {"quality", "aesthetic"} <= record.keys():
        raise ValueError("Q-ReAlign 缺少画质或审美评分。")
    return {
        "quality": qrealign_score(record["quality"], "画质评分"),
        "aesthetic": qrealign_score(record["aesthetic"], "审美评分"),
    }
