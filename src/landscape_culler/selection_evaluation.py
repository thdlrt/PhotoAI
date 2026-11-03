"""Read-only, fixed-budget evaluation against explicitly supplied human labels.

No model imports, inference, source-image access, or automatic label generation.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .selection_policy import retention_budget


def _ratio(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not .05 <= value <= 1:
        raise ValueError("保留比例必须是 0.05–1.0 的有限数值。")
    return float(value)


def _photo_id(path: str, root: str) -> str:
    if not isinstance(path, str) or not isinstance(root, str) or not path or not root:
        raise ValueError("评分结果缺少照片路径或 input_root。")
    windows = bool(PureWindowsPath(root).drive)
    path_type = PureWindowsPath if windows else PurePosixPath
    source, directory = path_type(path), path_type(root)
    try:
        relative = source.relative_to(directory)
    except ValueError as exc:
        raise ValueError(f"照片位于评分目录之外：{path}") from exc
    if not source.is_absolute() or not directory.is_absolute() or ".." in source.parts or not relative.parts:
        raise ValueError(f"评分照片路径无效：{path}")
    value = relative.as_posix()
    return value.casefold() if windows else value


def _records(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("评分结果必须是 JSON 对象。")
    if payload.get("workflow_state") != "scored":
        raise ValueError("评测需要已完成评分的 results.json。")
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise ValueError("评分结果缺少照片列表。")
    records: list[dict] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("评分照片记录格式无效。")
        if any(key in item and not isinstance(item[key], bool) for key in ("excluded", "deleted")):
            raise ValueError("excluded/deleted 标志必须是布尔值。")
        if item.get("excluded") or item.get("deleted"):
            continue
        identity = _photo_id(item.get("path"), payload.get("input_root"))
        if identity in seen:
            raise ValueError(f"评分结果包含重复照片：{identity}")
        seen.add(identity)
        key = item.get("source_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"照片缺少 source_key，不能确认是否为同一源文件：{identity}")
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise ValueError(f"照片综合分必须是 0–1 的有限数值：{identity}")
        records.append({"photo_id": identity, "source_key": key, "score": float(score)})
    if not records:
        raise ValueError("没有可评测照片。")
    return records


def _dataset_id(records: list[dict]) -> str:
    identities = sorted((row["photo_id"], row["source_key"]) for row in records)
    return hashlib.sha256(json.dumps(identities, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def create_reference_template(payload: dict) -> dict:
    """Deliberately omit AI scores and ratings from the blank human reference."""
    records = _records(payload)
    return {
        "schema_version": 1, "dataset_id": _dataset_id(records),
        "retain_ratio": _ratio(payload.get("retain_ratio", .3)),
        "label_protocol": "unlabeled", "rater": "", "notes": "",
        "photos": [{"photo_id": row["photo_id"], "source_key": row["source_key"],
                    "human_score": None, "technical_failure": None} for row in records],
    }


def evaluate_selection(payload: dict, reference: dict, retain_ratio: float | None = None) -> dict:
    if not isinstance(reference, dict):
        raise ValueError("人工参考必须是 JSON 对象。")
    if type(reference.get("schema_version")) is not int or reference["schema_version"] != 1:
        raise ValueError("不支持的人工参考格式。")
    if reference.get("label_protocol") not in {"independent", "assisted"}:
        raise ValueError("人工标注尚未完成：label_protocol 应声明为 independent 或 assisted。")
    if not isinstance(reference.get("rater"), str) or not reference["rater"].strip():
        raise ValueError("请记录人工评审者 rater。")
    photos = reference.get("photos")
    if not isinstance(photos, list) or not photos:
        raise ValueError("人工参考缺少照片列表。")
    labels: dict[str, dict] = {}
    for row in photos:
        if not isinstance(row, dict):
            raise ValueError("人工参考照片记录格式无效。")
        identity = row.get("photo_id")
        if not isinstance(identity, str) or not identity or identity in labels:
            raise ValueError("人工参考包含重复或无效 photo_id。")
        grade = row.get("human_score")
        if isinstance(grade, bool) or not isinstance(grade, int) or not 0 <= grade <= 5:
            raise ValueError(f"每张照片必须提供人工 0–5 整数评分：{identity}")
        if row.get("technical_failure") is not None and not isinstance(row["technical_failure"], bool):
            raise ValueError(f"technical_failure 必须为 true、false 或 null：{identity}")
        key = row.get("source_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"人工参考缺少 source_key：{identity}")
        labels[identity] = row
    if _dataset_id(photos) != reference.get("dataset_id"):
        raise ValueError("人工参考的照片集合或源文件标识已被修改。")

    records = _records(payload)
    predicted = {row["photo_id"]: row for row in records}
    if set(predicted) != set(labels):
        missing = sorted(set(labels) - set(predicted))
        extra = sorted(set(predicted) - set(labels))
        raise ValueError(f"评测照片集合不一致，不能只评估交集。缺少 {len(missing)} 张，新增 {len(extra)} 张。")
    if _dataset_id(records) != reference["dataset_id"]:
        raise ValueError("源文件标识不一致，不能将不同版本照片的分数直接比较。")
    ratio = _ratio(reference.get("retain_ratio") if retain_ratio is None else retain_ratio)
    budget = retention_budget(len(records), ratio)
    # Same stable tie rule as production: original result order wins equal scores.
    ranked = sorted(records, key=lambda row: row["score"], reverse=True)
    selected = ranked[:budget]
    selected_ids = {row["photo_id"] for row in selected}
    grades = [labels[row["photo_id"]]["human_score"] for row in selected]
    all_grades = [row["human_score"] for row in photos]
    ideal = sorted(all_grades, reverse=True)[:budget]
    dcg = lambda values: sum((2 ** grade - 1) / math.log2(index + 2) for index, grade in enumerate(values))
    ideal_dcg = dcg(ideal)
    preferred = {identity for identity, row in labels.items() if row["human_score"] >= 3}
    kept_preferred = len(selected_ids & preferred)
    technical_labeled = [row for row in selected if labels[row["photo_id"]].get("technical_failure") is not None]
    technical_failures = [row["photo_id"] for row in selected if labels[row["photo_id"]].get("technical_failure") is True]
    floor = min(grades)
    return {
        "schema_version": 1, "dataset_id": reference["dataset_id"],
        "label_protocol": reference["label_protocol"], "rater": reference["rater"],
        "photo_count": len(records), "retain_ratio": ratio, "budget": budget,
        "pipeline_version": payload.get("pipeline_version"),
        "selection_strategy": payload.get("selection_strategy"),
        "tie_break": "original_result_order", "human_preferred_threshold": 3,
        "metrics": {
            "selected_human_mean": sum(grades) / budget,
            "ideal_human_mean": sum(ideal) / budget,
            "human_mean_regret": (sum(ideal) - sum(grades)) / budget,
            "ndcg_at_budget": dcg(grades) / ideal_dcg if ideal_dcg else None,
            "preferred_precision": kept_preferred / budget,
            "preferred_recall": kept_preferred / len(preferred) if preferred else None,
            "technical_failure_rate": len(technical_failures) / len(technical_labeled) if technical_labeled else None,
            "technical_label_coverage": len(technical_labeled) / budget,
        },
        "selected": [{**row, "human_score": labels[row["photo_id"]]["human_score"]} for row in selected],
        "missed_above_selection_floor": [
            {**row, "human_score": labels[row["photo_id"]]["human_score"]}
            for row in ranked[budget:] if labels[row["photo_id"]]["human_score"] > floor
        ],
        "selected_technical_failures": technical_failures,
        "warnings": (["人工标注参考过 AI 结果，不能作为独立盲评。"] if reference["label_protocol"] == "assisted" else [])
            + (["技术失败标注不完整，不能直接比较技术失败率。"] if len(technical_labeled) != budget else [])
            + (["全部人工评分为 0，nDCG 与优选召回率无定义。"] if not any(all_grades) else []),
    }
