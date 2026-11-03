"""Similarity grouping without model loading or scoring side effects."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np

from .util import sequence_number

GROUPING_VERSION = "sequence-cohesion-v2"
NEIGHBOR_SIMILARITY = 0.82
ANCHOR_SIMILARITY = 0.75


def group_images(paths: list[Path], features: np.ndarray, technical_count: int = 8) -> list[list[int]]:
    """Keep consecutive bursts coherent instead of chaining unrelated endpoints.

    Neighbor similarity detects abrupt changes. The running centroid and first
    frame also bound gradual drift. Technical-only legacy inputs retain their
    filename grouping behavior; invalid semantic vectors never create a merge.
    """
    matrix = np.asarray(features, dtype=np.float64)
    if matrix.ndim != 2 or len(matrix) != len(paths) or technical_count < 0 or matrix.shape[1] < technical_count:
        raise ValueError("相似特征必须与照片一一对应。")
    semantic = matrix.shape[1] > technical_count
    vectors = matrix[:, technical_count:]
    norms = np.linalg.norm(vectors, axis=1)
    valid = np.isfinite(vectors).all(axis=1) & np.isfinite(norms) & (norms > 1e-8)
    vectors = np.divide(vectors, norms[:, None], out=np.zeros_like(vectors), where=valid[:, None])
    numbers = [sequence_number(path) for path in paths]
    by_parent: dict[str, list[int]] = defaultdict(list)
    for index, path in enumerate(paths):
        by_parent[str(path.parent)].append(index)
    groups: list[list[int]] = []
    for indices in by_parent.values():
        indices.sort(key=lambda i: (numbers[i] is None, numbers[i] or 0, paths[i].name.lower()))
        current: list[int] = []
        center_sum = np.zeros(vectors.shape[1], dtype=np.float64)
        for index in indices:
            close = False
            coherent = not semantic
            if current:
                previous = current[-1]
                close = numbers[previous] is not None and numbers[index] is not None and 0 < numbers[index] - numbers[previous] <= 5
                if semantic and valid[index] and valid[previous] and valid[current[0]]:
                    center = center_sum / max(float(np.linalg.norm(center_sum)), 1e-8)
                    coherent = (float(vectors[index] @ vectors[previous]) >= NEIGHBOR_SIMILARITY
                        and float(vectors[index] @ center) >= NEIGHBOR_SIMILARITY
                        and float(vectors[index] @ vectors[current[0]]) >= ANCHOR_SIMILARITY)
            if current and not (close and coherent):
                groups.append(current)
                current = []
                center_sum = np.zeros(vectors.shape[1], dtype=np.float64)
            current.append(index)
            center_sum += vectors[index]
        if current:
            groups.append(current)
    return groups
