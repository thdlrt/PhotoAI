from __future__ import annotations

import hashlib
import html
import os
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from .constants import (
    PROPRIETARY_RAW_EXTENSIONS,
    SCORING_PIPELINE_VERSION,
)
from .features import TECHNICAL_NAMES, FeatureExtractor
from .fusion import fuse_scores, relative_sharpness_flags, technical_quality
from .general_aesthetic import GeneralAestheticScorer
from .group_critic import OllamaGroupCritic
from .grouping import GROUPING_VERSION, group_images as _group_images
from .preview import preview_cache_path
from .progress import emit_progress, phase_end, phase_start
from .selection_policy import retention_budget
from .util import (
    cache_key, is_photo_trash_path, raw_photo_paths, read_json,
    runs_root, write_json,
)


def _cache_root(data_dir: Path) -> Path:
    return Path(os.environ.get("PHOTO_AI_CACHE_DIR") or data_dir / "cache")


def _keywords(
    rating: int, tech: dict, low_sharpness: bool, group_size: int, mode: str
) -> list[str]:
    result = ["AI|来源|深度视觉模型" if mode == "deep" else "AI|来源|通用审美模型"]
    if rating == 4:
        result.append("AI|候选|强推荐")
    elif rating == 3:
        result.append("AI|候选")
    if group_size > 1:
        result.append("AI|相似序列")
    if tech["highlight_clip"] >= 0.02:
        result.append("AI|技术警告|高光剪切")
    if tech["shadow_clip"] >= 0.08:
        result.append("AI|技术警告|阴影堵塞")
    if low_sharpness:
        result.append("AI|技术警告|清晰度待复核")
    return result


def _raw_paths(input_root: Path) -> list[Path]:
    return raw_photo_paths(input_root)


def _path_identity(path: Path) -> str:
    value = str(path.resolve(strict=False))
    return value.casefold() if os.name == "nt" else value


def _group_ids(
    groups: list[list[int]], count: int, labels: list[int] | None = None
) -> np.ndarray:
    labels = labels or list(range(1, len(groups) + 1))
    if (
        len(labels) != len(groups)
        or len(set(labels)) != len(labels)
        or any(label <= 0 for label in labels)
    ):
        raise ValueError("分组编号无效或重复。")
    result = np.zeros(count, dtype=np.int32)
    seen: set[int] = set()
    for group_id, indices in zip(labels, groups, strict=True):
        if not indices:
            raise ValueError("分组中不能包含空组。")
        for index in indices:
            if index < 0 or index >= count or index in seen:
                raise ValueError("分组包含重复或超出范围的照片。")
            seen.add(index)
            result[index] = group_id
    if seen != set(range(count)):
        raise ValueError("分组没有覆盖全部照片。")
    return result


def _groups_from_file(
    paths: list[Path], grouping_path: Path
) -> tuple[list[list[int]], list[int]]:
    payload = read_json(grouping_path)
    records = [
        record for record in payload.get("results", [])
        if not record.get("excluded") and not record.get("deleted")
        and not is_photo_trash_path(Path(record.get("path", "")))
    ]
    by_path: dict[str, tuple[int, str | None]] = {}
    for record in records:
        raw = Path(record.get("path", ""))
        identity = _path_identity(raw)
        if not identity or identity in by_path:
            raise RuntimeError("人工分组文件包含重复或无效照片。")
        by_path[identity] = (int(record.get("group_id", 0)), record.get("source_key"))
    current = {_path_identity(path) for path in paths}
    if set(by_path) != current:
        raise RuntimeError("照片目录内容已变化，请重新分类后再评分。")
    grouped: dict[int, list[int]] = defaultdict(list)
    for index, path in enumerate(paths):
        group_id, expected_key = by_path[_path_identity(path)]
        if group_id <= 0:
            raise RuntimeError("人工分组编号无效，请返回工程重新调整。")
        if expected_key and expected_key != cache_key(path):
            raise RuntimeError(f"照片在分类后发生变化，请重新分类：{path.name}")
        grouped[group_id].append(index)
    labels = sorted(grouped)
    groups = [grouped[group_id] for group_id in labels]
    _group_ids(groups, len(paths), labels)
    return groups, labels


def _paths_from_grouping(
    paths: list[Path],
    grouping_path: Path,
) -> tuple[list[Path], list[dict]]:
    """Validate the full directory snapshot, then omit reviewed exclusions."""

    payload = read_json(grouping_path)
    records = [record for record in payload.get("results", [])
               if not record.get("deleted")
               and not is_photo_trash_path(Path(record.get("path", "")))]
    by_path: dict[str, dict] = {}
    for record in records:
        raw = Path(record.get("path", ""))
        identity = _path_identity(raw)
        if not identity or identity in by_path:
            raise RuntimeError("人工分组文件包含重复或无效照片。")
        by_path[identity] = record
    current = {_path_identity(path) for path in paths}
    if set(by_path) != current:
        raise RuntimeError("照片目录内容已变化，请重新分类后再评分。")
    active: list[Path] = []
    excluded: list[dict] = []
    for path in paths:
        record = by_path[_path_identity(path)]
        expected_key = record.get("source_key")
        if expected_key and expected_key != cache_key(path):
            raise RuntimeError(f"照片在分类后发生变化，请重新分类：{path.name}")
        if record.get("excluded"):
            excluded.append(record)
        else:
            active.append(path)
    if not active:
        raise RuntimeError("工程中没有可评分照片，请先恢复至少一张。")
    return active, excluded


def assign_global_ratings(
    local_scores: np.ndarray,
    global_scores: np.ndarray,
    groups: list[list[int]],
    retain_ratio: float,
) -> tuple[np.ndarray, list[int], dict[int, int]]:
    """Strict batch quota: grouping never limits a photo's eligibility."""
    if len(local_scores) != len(global_scores) or not np.isfinite(global_scores).all():
        raise ValueError("全批评分必须覆盖全部照片，并为有限数值。")
    keep = retention_budget(len(global_scores), retain_ratio)
    _group_ids(groups, len(global_scores))
    pool = list(range(len(global_scores)))
    ranked = sorted(pool, key=lambda index: float(global_scores[index]), reverse=True)
    strong = max(1, (keep + 2) // 3)
    ratings = np.zeros(len(local_scores), dtype=np.int32)
    for rank, index in enumerate(ranked[:keep]):
        ratings[index] = 4 if rank < strong else 3
    ranks = {index: rank for rank, index in enumerate(ranked, start=1)}
    return ratings, pool, ranks


def _run_dir(data_dir: Path) -> tuple[str, Path]:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run_dir = runs_root(data_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_id, run_dir


def _publish_run(data_dir: Path, run_dir: Path, payload: dict, *, report: bool) -> dict:
    results_path = run_dir / "results.json"
    write_json(results_path, payload)
    report_path = run_dir / "report.html"
    if report:
        _write_html_report(report_path, payload)
    if report:
        latest_dir = runs_root(data_dir) / "latest"
        latest_dir.mkdir(parents=True, exist_ok=True)
        write_json(latest_dir / "results.json", payload)
        _write_html_report(latest_dir / "report.html", payload)
    return {
        **{key: value for key, value in payload.items() if key != "results"},
        "results_path": str(results_path),
        "report_path": str(report_path) if report else None,
    }


def classify_directory(
    input_root: Path,
    data_dir: Path,
    retain_ratio: float = 0.30,
    mode: str = "deep",
) -> dict:
    """Create an editable project by extracting previews and similarity groups only."""

    if not 0.05 <= retain_ratio <= 1.0:
        raise ValueError("retain_ratio 必须在 0.05 到 1.0 之间。")
    if mode not in {"fast", "deep"}:
        raise ValueError("mode 必须是 fast 或 deep。")
    paths = _raw_paths(input_root)
    if not paths:
        raise RuntimeError(f"没有找到可分类 RAW：{input_root}")
    cache_root = _cache_root(data_dir)
    extractor = FeatureExtractor(cache_root, use_dino=True)
    try:
        features, paths = extractor.extract(paths)
    finally:
        extractor.release()
    phase_start("grouping", "自动整理分组", 1, unit="步")
    groups = _group_images(paths, features, technical_count=8)
    phase_end("grouping", "自动整理分组", 1, unit="步")
    group_ids = _group_ids(groups, len(paths))
    group_sizes = CounterLike(group_ids)
    sharpness_flags = relative_sharpness_flags(features, groups)
    results: list[dict] = []
    phase_start("finalize", "保存分组工程", len(paths), unit="张")
    for index, path in enumerate(paths):
        tech = {
            name: float(features[index, offset])
            for offset, name in enumerate(TECHNICAL_NAMES)
        }
        warnings = [
            value
            for value in _keywords(
                0,
                tech,
                bool(sharpness_flags[index]),
                group_sizes[int(group_ids[index])],
                mode,
            )
            if value == "AI|相似序列" or value.startswith("AI|技术警告")
        ]
        results.append(
            {
                "path": str(path),
                "source_key": cache_key(path),
                "extension": path.suffix.lower(),
                "can_write_sidecar": path.suffix.lower() in PROPRIETARY_RAW_EXTENSIONS,
                "score": 0.0,
                "rating": 0,
                "group_id": int(group_ids[index]),
                "group_size": group_sizes[int(group_ids[index])],
                "keywords": warnings,
                "technical": tech,
                "components": {},
                "reason": None,
                "scoring_mode": mode,
                "pipeline_version": SCORING_PIPELINE_VERSION,
                "excluded": False,
                "preview": str(preview_cache_path(path, cache_root / "previews")),
            }
        )
        emit_progress("finalize", "保存分组工程", index + 1, len(paths), unit="张")
    # Keep unreadable sources in the directory snapshot. Dropping them here
    # makes the next scoring run incorrectly report a changed directory.
    failures = getattr(extractor, "failures", [])
    for offset, (path, error) in enumerate(failures, 1):
        results.append({
            "path": str(path), "source_key": cache_key(path),
            "extension": path.suffix.lower(),
            "can_write_sidecar": path.suffix.lower() in PROPRIETARY_RAW_EXTENSIONS,
            "score": 0.0, "rating": 0, "group_id": len(groups) + offset,
            "group_size": 0, "keywords": ["AI|读取失败"],
            "technical": {}, "components": {}, "reason": None,
            "scoring_mode": mode, "pipeline_version": SCORING_PIPELINE_VERSION,
            "excluded": True, "read_error": str(error)[:400],
            "preview": str(preview_cache_path(path, cache_root / "previews")),
        })
    run_id, run_dir = _run_dir(data_dir)
    payload = {
        "run_id": run_id,
        "input_root": str(input_root),
        "retain_ratio": retain_ratio,
        "scoring_mode": mode,
        "workflow_state": "grouped",
        "grouping_algorithm": GROUPING_VERSION,
        "pipeline_version": SCORING_PIPELINE_VERSION,
        "image_count": len(results),
        "active_image_count": len(paths),
        "excluded_count": len(failures),
        "group_count": len(groups),
        "candidate_count": 0,
        "strong_count": 0,
        "results": results,
    }
    published = _publish_run(data_dir, run_dir, payload, report=False)
    phase_end("finalize", "保存分组工程", len(paths), unit="张")
    return published


def score_directory(
    input_root: Path,
    data_dir: Path,
    retain_ratio: float = 0.30,
    mode: str = "deep",
    grouping_path: Path | None = None,
    source_run_id: str | None = None,
) -> dict:
    if not 0.05 <= retain_ratio <= 1.0:
        raise ValueError("retain_ratio 必须在 0.05 到 1.0 之间。")
    if mode not in {"fast", "deep"}:
        raise ValueError("mode 必须是 fast 或 deep。")
    paths = _raw_paths(input_root)
    if not paths:
        raise RuntimeError(f"没有找到可评分 RAW：{input_root}")

    grouping_meta = read_json(grouping_path) if grouping_path else {}
    excluded_records: list[dict] = []
    group_labels: list[int] | None = None
    if grouping_path:
        paths, excluded_records = _paths_from_grouping(paths, grouping_path)
    # DINO remains the similarity-grouping representation and cached fallback.
    # Release the previous local VLM before allocating DINO, not afterwards.
    OllamaGroupCritic(data_dir).unload()
    cache_root = _cache_root(data_dir)
    extractor = FeatureExtractor(cache_root, use_dino=True)
    try:
        features, paths = extractor.extract(paths)
        if failures := getattr(extractor, "failures", []):
            names = "、".join(path.name for path, _ in failures[:3])
            raise RuntimeError(f"无法读取照片：{names}。请修复源文件，或重新分类以跳过无法读取的照片。")
        if grouping_path:
            groups, group_labels = _groups_from_file(paths, grouping_path)
        else:
            groups = _group_images(paths, features, 8)
    finally:
        extractor.release()
    aesthetic_scorer = GeneralAestheticScorer(data_dir)
    try:
        general = aesthetic_scorer.score(paths)
    finally:
        aesthetic_scorer.release()

    technical_scores = technical_quality(features, groups)
    fast_scores, fast_components, fast_reasons = fuse_scores(
        features,
        groups,
        general,
        mode="fast",
        technical_scores=technical_scores,
    )
    phase_start("local_rank", "初步质量排序", len(paths), unit="张")
    phase_end("local_rank", "初步质量排序", len(paths), unit="张")

    # Every photograph is reviewed on one anchored global scale. A preliminary
    # score orders the batches; it never excludes a photo. This removes both
    # per-group admission quotas and the duplicate local-then-global VLM pass.
    pool = list(range(len(paths)))
    critic = OllamaGroupCritic(data_dir) if mode == "deep" else None
    try:
        if mode == "deep":
            assert critic is not None
            global_critiques = critic.critique_groups(
                paths, [pool], fast_scores.tolist(), context="global",
            )
        phase_start("global_rank", "跨组统一排名", len(pool), unit="张")
        if mode == "fast":
            global_scores, components, reasons = fast_scores, fast_components, fast_reasons
        else:
            global_scores, components, reasons = fuse_scores(
                features, groups, general, mode=mode, critiques=global_critiques,
                technical_scores=technical_scores,
            )
        phase_end("global_rank", "跨组统一排名", len(pool), unit="张")
    finally:
        if critic is not None:
            critic.unload()

    # Legacy consumers may still read local_score. It now uses the same
    # calibrated score as global_score, including for within-group ordering.
    local_scores = global_scores

    ratings, pool, global_ranks = assign_global_ratings(
        local_scores, global_scores, groups, retain_ratio
    )
    sharpness_flags = relative_sharpness_flags(features, groups)
    group_ids = _group_ids(groups, len(paths), group_labels)
    group_ranks: dict[int, int] = {}
    for group_id, indices in enumerate(groups, start=1):
        ranked = sorted(
            indices, key=lambda index: float(local_scores[index]), reverse=True
        )
        group_ranks.update({index: rank for rank, index in enumerate(ranked, start=1)})

    run_id, run_dir = _run_dir(data_dir)
    grouping_snapshot_sha256 = None
    phase_start("finalize", "生成选片结果", len(paths), unit="张")
    if grouping_path:
        snapshot_target = run_dir / "grouping.snapshot.json"
        shutil.copyfile(grouping_path, snapshot_target)
        grouping_snapshot_sha256 = hashlib.sha256(
            snapshot_target.read_bytes()
        ).hexdigest()
    results: list[dict] = []
    group_sizes = CounterLike(group_ids)
    for index, path in enumerate(paths):
        tech = {
            name: float(features[index, offset])
            for offset, name in enumerate(TECHNICAL_NAMES)
        }
        rating = int(ratings[index])
        keywords = _keywords(
            rating,
            tech,
            bool(sharpness_flags[index]),
            group_sizes[int(group_ids[index])],
            mode,
        )
        results.append(
            {
                "path": str(path),
                "extension": path.suffix.lower(),
                "source_key": cache_key(path),
                "can_write_sidecar": path.suffix.lower() in PROPRIETARY_RAW_EXTENSIONS,
                "score": float(global_scores[index]),
                "local_score": float(local_scores[index]),
                "global_score": float(global_scores[index])
                if index in global_ranks
                else None,
                "group_candidate": index in global_ranks,
                "group_rank": group_ranks[index],
                "global_rank": global_ranks.get(index),
                "rating_basis": "batch_global",
                "rating": rating,
                "group_id": int(group_ids[index]),
                "group_size": group_sizes[int(group_ids[index])],
                "keywords": keywords,
                "technical": tech,
                "components": components[index],
                "reason": reasons[index],
                "general_ai": {
                    "model": general[index]["model"],
                    "quality_raw": float(general[index]["quality"]),
                    "aesthetic_raw": float(general[index]["aesthetic"]),
                },
                "scoring_mode": mode,
                "pipeline_version": SCORING_PIPELINE_VERSION,
                "excluded": False,
                "preview": str(preview_cache_path(path, cache_root / "previews")),
            }
        )
        emit_progress("finalize", "生成选片结果", index + 1, len(paths), unit="张")

    for record in excluded_records:
        excluded = dict(record)
        keywords = [
            value
            for value in excluded.get("keywords", [])
            if not str(value).startswith("AI|候选")
            and not str(value).startswith("人工|已移除")
            and str(value) != "AI|来源|个人偏好融合"
        ]
        keywords.append("人工|已移除")
        excluded.pop("personal_score", None)
        excluded.update(
            {
                "score": 0.0,
                "local_score": None,
                "global_score": None,
                "group_candidate": False,
                "group_rank": None,
                "global_rank": None,
                "rating_basis": "excluded",
                "rating": 0,
                "group_size": 0,
                "keywords": keywords,
                "components": {},
                "reason": None,
                "general_ai": None,
                "scoring_mode": mode,
                "pipeline_version": SCORING_PIPELINE_VERSION,
                "excluded": True,
            }
        )
        results.append(excluded)

    payload = {
        "run_id": run_id,
        "input_root": str(input_root),
        "retain_ratio": retain_ratio,
        "scoring_mode": mode,
        "workflow_state": "scored",
        "pipeline_version": SCORING_PIPELINE_VERSION,
        "selection_strategy": "strict_global_budget_v2",
        "grouping_algorithm": grouping_meta.get("grouping_algorithm", "imported-snapshot") if grouping_path else GROUPING_VERSION,
        "source_run_id": source_run_id,
        "grouping_revision": grouping_meta.get("review_revision"),
        "group_order": grouping_meta.get("group_order", []),
        "grouping_snapshot_sha256": grouping_snapshot_sha256,
        "image_count": len(results),
        "active_image_count": len(paths),
        "excluded_count": len(excluded_records),
        "group_count": len(groups),
        "selection_pool_count": len(pool),
        "candidate_count": sum(1 for item in results if item["rating"] >= 3),
        "strong_count": sum(1 for item in results if item["rating"] == 4),
        "results": results,
    }
    published = _publish_run(data_dir, run_dir, payload, report=True)
    phase_end("finalize", "生成选片结果", len(paths), unit="张")
    return published


def CounterLike(values: np.ndarray) -> dict[int, int]:
    result: dict[int, int] = defaultdict(int)
    for value in values.tolist():
        result[int(value)] += 1
    return result


def _write_html_report(path: Path, payload: dict) -> None:
    results = [item for item in payload["results"] if not item.get("excluded")]
    rating_counts = {
        rating: sum(1 for item in results if int(item["rating"]) == rating)
        for rating in (0, 3, 4)
    }
    groups: dict[int, list[dict]] = defaultdict(list)
    for item in results:
        groups[int(item["group_id"])].append(item)

    group_sections: list[str] = []
    for group_id in sorted(groups):
        items = sorted(
            groups[group_id],
            key=lambda item: (
                -int(item["rating"]),
                -float(item["score"]),
                str(item["path"]).lower(),
            ),
        )
        group_size = len(items)
        candidate_count = sum(1 for item in items if int(item["rating"]) >= 3)
        singleton_note = (
            '<span class="singleton-note">单张组 · 参与全局比较</span>'
            if group_size == 1
            else ""
        )
        cards: list[str] = []
        for item in items:
            rating = int(item["rating"])
            preview_uri = html.escape(Path(item["preview"]).as_uri(), quote=True)
            keywords = " · ".join(
                html.escape(str(keyword), quote=True) for keyword in item["keywords"]
            )
            rating_label = {4: "4★ 强推荐", 3: "3★ 候选", 0: "0★ 未入选"}.get(
                rating, f"{rating}★"
            )
            singleton_card_note = (
                '<small class="singleton-card-note">按全局候选统一评分</small>'
                if group_size == 1
                else ""
            )
            cards.append(
                f'<article class="card rating-{rating}" data-rating="{rating}" data-group-size="{group_size}">'
                f'<a class="preview-link" href="{preview_uri}" title="打开本地预览">'
                f'<img loading="lazy" src="{preview_uri}" alt="{html.escape(Path(item["path"]).name, quote=True)}"></a>'
                f'<div class="meta"><div class="card-heading"><strong>{html.escape(Path(item["path"]).name, quote=True)}</strong>'
                f'<span class="rating-badge">{rating_label}</span></div>'
                f'<span class="score">综合分 {float(item["score"]):.3f} · 组 {group_id}/{group_size}</span>'
                f'{singleton_card_note}<small class="keywords">{keywords or "无 AI 关键词"}</small>'
                f"<code>{html.escape(str(item['path']), quote=True)}</code></div></article>"
            )
        group_sections.append(
            f'<section class="group" data-group-id="{group_id}"><header class="group-header">'
            f"<div><h2>组 {group_id}</h2><p>组内 {group_size} 张 · 候选 {candidate_count} 张</p></div>{singleton_note}"
            f'</header><div class="grid">{"".join(cards)}</div></section>'
        )

    group_count = len(groups)
    singleton_count = sum(1 for items in groups.values() if len(items) == 1)
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>风光 AI 选片报告</title>
<style>
:root{{--bg:#0c0e11;--panel:#15191e;--panel-2:#1b2027;--line:#2c333d;--text:#eef2f5;--muted:#98a2ad;--green:#55c878;--blue:#72a7ff;--amber:#e4b85f}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.page{{width:min(1800px,100%);margin:auto;padding:28px}}h1,h2,p{{margin:0}}.lead{{margin-top:8px;color:var(--muted);overflow-wrap:anywhere}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:22px 0 16px}}.stat{{padding:14px 16px;background:var(--panel);border:1px solid var(--line);border-radius:12px}}
.stat strong{{display:block;font-size:23px}}.stat span{{color:var(--muted)}}
.toolbar{{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 -8px 22px;padding:12px 8px;background:rgba(12,14,17,.94);backdrop-filter:blur(8px)}}
.toolbar button{{border:1px solid var(--line);border-radius:999px;padding:7px 12px;background:var(--panel);color:var(--text);cursor:pointer}}.toolbar button:hover{{border-color:#697583}}.toolbar button.active{{border-color:var(--blue);background:#1c3154}}
#visible-count{{margin-left:auto;color:var(--muted)}}.group{{margin:0 0 30px;scroll-margin-top:76px}}.group[hidden],.card[hidden]{{display:none}}
.group-header{{display:flex;align-items:end;justify-content:space-between;gap:12px;padding:0 2px 10px;border-bottom:1px solid var(--line);margin-bottom:12px}}.group-header h2{{font-size:18px}}.group-header p{{color:var(--muted)}}
.singleton-note,.singleton-card-note{{color:var(--amber)}}.singleton-note{{border:1px solid #695727;background:#2d2719;border-radius:999px;padding:4px 9px;white-space:nowrap}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}}.card{{min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;transition:opacity .15s,border-color .15s,filter .15s}}
.card.rating-4{{border-color:#3b9d5a}}.card.rating-3{{border-color:#426a9d}}.card.rating-0 .card-heading strong{{color:var(--muted)}}
.preview-link{{display:block;background:#07090b}}.card img{{display:block;width:100%;height:220px;object-fit:contain}}.meta{{display:grid;gap:7px;padding:12px}}
.card-heading{{display:flex;align-items:start;justify-content:space-between;gap:10px}}.card-heading strong{{min-width:0;overflow-wrap:anywhere}}.rating-badge{{flex:none;font-size:12px;border-radius:999px;padding:2px 7px;background:var(--panel-2)}}
.rating-4 .rating-badge{{color:#8de5a8}}.rating-3 .rating-badge{{color:#9bc1ff}}.rating-0 .rating-badge{{color:#a0a7ae}}.score{{color:#c4ccd4}}small{{color:#adb6bf}}code{{font:11px/1.45 ui-monospace,"Cascadia Mono",monospace;color:#818b96;overflow-wrap:anywhere}}
@media(max-width:640px){{.page{{padding:18px 12px}}#visible-count{{width:100%;margin-left:2px}}.card img{{height:200px}}}}
</style></head><body><div class="page"><header><h1>风光 AI 选片报告</h1>
<p class="lead">批次：{html.escape(str(payload["input_root"]), quote=True)}。报告按相似组展示，组内按星级和综合分从高到低排列；所有预览保持相同的亮度与色彩显示。</p></header>
<section class="stats" aria-label="报告统计">
<div class="stat"><strong>{len(results)}</strong><span>全部照片</span></div><div class="stat"><strong>{group_count}</strong><span>相似组</span></div>
<div class="stat"><strong>{rating_counts[4]}</strong><span>4★ 强推荐</span></div><div class="stat"><strong>{rating_counts[3]}</strong><span>3★ 候选</span></div>
<div class="stat"><strong>{rating_counts[0]}</strong><span>0★ 未入选</span></div><div class="stat"><strong>{singleton_count}</strong><span>单张组</span></div></section>
<nav class="toolbar" aria-label="照片筛选">
<button type="button" class="active" data-filter="all" aria-pressed="true">全部 {len(results)}</button>
<button type="button" data-filter="candidates" aria-pressed="false">候选 {rating_counts[3] + rating_counts[4]}</button>
<button type="button" data-filter="strong" aria-pressed="false">4★ {rating_counts[4]}</button>
<button type="button" data-filter="three" aria-pressed="false">3★ {rating_counts[3]}</button>
<button type="button" data-filter="rejected" aria-pressed="false">0★ {rating_counts[0]}</button>
<button type="button" data-filter="singleton" aria-pressed="false">单张组 {singleton_count}</button><span id="visible-count">已显示 {len(results)} 张</span></nav>
<main>{"".join(group_sections)}</main><noscript><p class="lead">当前已显示所有照片；启用 JavaScript 后可使用顶部筛选按钮。</p></noscript></div>
<script>
(() => {{
  const cards = [...document.querySelectorAll('.card')];
  const groups = [...document.querySelectorAll('.group')];
  const filters = {{
    all: card => true,
    candidates: card => Number(card.dataset.rating) >= 3,
    strong: card => Number(card.dataset.rating) === 4,
    three: card => Number(card.dataset.rating) === 3,
    rejected: card => Number(card.dataset.rating) === 0,
    singleton: card => Number(card.dataset.groupSize) === 1
  }};
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {{
    const accepts = filters[button.dataset.filter] || filters.all;
    let visible = 0;
    cards.forEach(card => {{ card.hidden = !accepts(card); if (!card.hidden) visible += 1; }});
    groups.forEach(group => {{ group.hidden = ![...group.querySelectorAll('.card')].some(card => !card.hidden); }});
    document.querySelectorAll('[data-filter]').forEach(item => {{
      const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active));
    }});
    document.getElementById('visible-count').textContent = `已显示 ${{visible}} 张`;
  }}));
}})();
</script></body></html>"""
    path.write_text(document, encoding="utf-8")
