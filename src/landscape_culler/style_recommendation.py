from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import read_json, write_json

STYLE_RECOMMENDATION_SCHEMA_VERSION = 5
RECOMMENDATION_WEIGHTS = {
    "style_match": 0.40,
    "aesthetic_improvement": 0.25,
    "technical_quality": 0.15,
    "group_consistency": 0.20,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _creative_look_fields(entry: Mapping[str, Any]) -> dict[str, Any]:
    if str(entry.get("look_kind") or "") != "lightroom_profile":
        return {}
    descriptor = entry.get("look_descriptor")
    return {
        "look_descriptor": dict(descriptor)
        if isinstance(descriptor, Mapping)
        else None,
        "look_descriptor_hash": entry.get("look_descriptor_hash"),
        "look_uuid": entry.get("look_uuid") or entry.get("uuid"),
    }


def _item_path(item: dict[str, Any]) -> str:
    # Recommendation probes must retain the original photo identity.  The
    # browser JPEG is useful for display, but Lightroom must receive the RAW.
    return str(
        item.get("path")
        or item.get("source_path")
        or item.get("preview")
        or item.get("preview_path")
        or ""
    )


def select_group_probes(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Choose a deterministic representative plus brightness extremes.

    The caller can mark the DINO medoid with ``is_representative``.  Without
    DINO output the first stable path is used and the result explicitly records
    the fallback, rather than presenting it as an AI medoid.
    """

    values = [dict(item) for item in items]
    values.sort(key=_item_path)
    if not values:
        return {
            "representative": None,
            "brightest": None,
            "darkest": None,
            "basis": "empty",
        }
    representative = next(
        (item for item in values if item.get("is_representative")), values[0]
    )
    with_brightness = [
        item
        for item in values
        if isinstance((item.get("technical") or {}).get("brightness"), (int, float))
    ]
    brightest = (
        max(with_brightness, key=lambda item: float(item["technical"]["brightness"]))
        if with_brightness
        else values[-1]
    )
    darkest = (
        min(with_brightness, key=lambda item: float(item["technical"]["brightness"]))
        if with_brightness
        else values[0]
    )
    return {
        "representative": _item_path(representative),
        "brightest": _item_path(brightest),
        "darkest": _item_path(darkest),
        "basis": "dinov2_medoid"
        if representative.get("is_representative")
        else "stable_fallback",
    }


def preview_cache_key(
    *,
    source_path: str,
    preset_id: str,
    preset_hash: str,
    amount: float,
    base_hash: str = "",
    source_fingerprint: str = "",
    crop_revision: str = "",
    basic_color_revision: str = "",
    lightroom_version: str = "",
    catalog_version: str = "",
    plugin_version: str = "",
    look_renderer_version: str = "lr-look-v1",
    size: int = 1024,
) -> str:
    payload = "|".join(
        (
            source_path.casefold(),
            preset_id,
            preset_hash,
            f"{float(amount):.4f}",
            base_hash,
            source_fingerprint,
            crop_revision,
            basic_color_revision,
            lightroom_version,
            catalog_version,
            plugin_version,
            str(size),
            # The creative-Look renderer has its own identity because these
            # resources are nested Lightroom Look settings, not CameraProfile
            # names or ordinary develop presets. Older previews could succeed
            # operationally while rendering the neutral fallback.
            look_renderer_version,
        )
    )
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


def _catalog_entry(catalog: dict[str, Any], preset_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in catalog.get("entries", [])
            if item.get("preset_id") == preset_id
        ),
        None,
    )


def canonical_pool_entries(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one runtime-resolvable catalog entry per enabled preset id.

    A managed local copy can share its source UUID with the installed Adobe
    preset that made that UUID eligible.  Filtering only by ``default_pool``
    therefore admits both rows.  Respect the per-entry eligibility decision
    and de-duplicate by id before recall.
    """

    allowed = {str(value) for value in catalog.get("default_pool") or []}
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in catalog.get("entries", []):
        if not isinstance(raw, dict):
            continue
        preset_id = str(raw.get("preset_id") or "")
        if not preset_id or preset_id not in allowed or preset_id in seen:
            continue
        if raw.get("duplicate_of") or raw.get("hidden"):
            continue
        # Old/test catalogs predate the explicit flag.  A present false value
        # is authoritative; an absent value falls back to default_pool.
        if "ai_eligible" in raw and not raw.get("ai_eligible"):
            continue
        # A preset file can be installed on disk yet absent from Lightroom's
        # SDK catalog.  When a runtime enumeration has been applied, its false
        # result is authoritative.  Absence preserves compatibility with old
        # indexes and tests that have not run the Lightroom bridge yet.
        if raw.get("runtime_resolvable") is False:
            continue
        seen.add(preset_id)
        output.append(dict(raw))
    return output


_SCENE_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "landscape": (
        "landscape",
        "mountain",
        "forest",
        "lake",
        "river",
        "water",
        "waterfall",
        "coast",
        "湿地",
        "山",
        "湖",
        "江",
        "河",
        "海",
        "树林",
        "风光",
    ),
    "season": (
        "spring",
        "summer",
        "autumn",
        "winter",
        "春",
        "夏",
        "秋",
        "冬",
        "snow",
        "雪",
        "foliage",
    ),
    "tone": (
        "blue hour",
        "golden hour",
        "sunrise",
        "sunset",
        "night",
        "overcast",
        "mist",
        "fog",
        "蓝调",
        "日出",
        "日落",
        "夜景",
        "阴天",
        "雾",
    ),
    "region": (
        "sky",
        "water",
        "subject",
        "foreground",
        "天空",
        "水面",
        "主体",
        "前景",
    ),
    "film": ("cinematic", "film", "movie", "胶片", "电影"),
    "creative": ("creative", "vivid", "matte", "复古", "创意", "鲜艳"),
}


def _scene_text(scene: dict[str, Any] | None) -> str:
    if not scene:
        return ""
    values: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, (list, tuple, set)):
            for nested in value:
                visit(nested)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            values.append(str(value))

    visit(scene)
    return " ".join(values).casefold()


def scene_preset_affinity(
    entry: dict[str, Any],
    scene: dict[str, Any] | None,
) -> float:
    """Score deterministic scene/name/category agreement for local recall."""

    text = _scene_text(scene)
    if not text:
        return 0.0
    category = str(entry.get("category") or "").casefold()
    name = " ".join(
        str(entry.get(key) or "") for key in ("name", "group", "source")
    ).casefold()
    score = 0.0
    terms = _SCENE_CATEGORY_TERMS.get(category, ())
    if any(term in text for term in terms):
        score += 0.55
    name_terms = {
        token
        for token in name.replace("-", " ").replace("_", " ").split()
        if len(token) >= 3
    }
    matches = sum(token in text for token in name_terms)
    score += min(0.30, matches * 0.10)
    if category == "landscape":
        score += 0.10
    if entry.get("adaptive") or entry.get("black_and_white"):
        score -= 0.30
    return _bounded(score)


def recall_candidates(
    catalog: dict[str, Any],
    *,
    clip_scores: dict[str, float] | None = None,
    scene: dict[str, Any] | None = None,
    limit: int = 12,
) -> tuple[list[dict[str, Any]], str]:
    """Return CLIP-ranked candidates or an explicitly unscored fallback queue."""

    entries = canonical_pool_entries(catalog)
    scene_text = _scene_text(scene)
    if clip_scores:
        entries.sort(
            key=lambda item: (
                -_bounded(clip_scores.get(str(item.get("preset_id")), 0.0)),
                -scene_preset_affinity(item, scene),
                str(item.get("name", "")),
            )
        )
        basis = "clip_image_text"
    elif scene_text:
        # The hash is only a deterministic tie-break.  It avoids serving every
        # scene the same alphabetic queue when category/name evidence ties.
        entries.sort(
            key=lambda item: (
                -scene_preset_affinity(item, scene),
                hashlib.sha256(
                    f"{scene_text}|{item.get('preset_id', '')}".encode(
                        "utf-8", "surrogatepass"
                    )
                ).hexdigest(),
                str(item.get("name") or "").casefold(),
            )
        )
        basis = "scene_heuristic"
    else:
        entries.sort(
            key=lambda item: (
                str(item.get("name") or "").casefold(),
                str(item.get("preset_id") or ""),
            )
        )
        basis = "unscored_fallback"
    return [dict(item) for item in entries[: max(0, limit)]], basis


def build_render_tasks(
    group_id: str | int,
    probes: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
    *,
    amount: int = 100,
    base_hash: str = "",
    source_fingerprint: str = "",
    crop_revision: str = "",
    basic_color_revision: str = "",
    lightroom_version: str = "",
    catalog_version: str = "",
    plugin_version: str = "",
    look_renderer_version: str = "lr-look-v1",
    limit: int = 6,
) -> list[dict[str, Any]]:
    source = str(probes.get("representative") or "")
    tasks: list[dict[str, Any]] = []
    if not source:
        return tasks
    for entry in list(candidates)[: max(0, limit)]:
        look_kind = str(entry.get("look_kind") or "lightroom_preset")
        is_creative_look = look_kind == "lightroom_profile"
        tasks.append(
            {
                "group_id": str(group_id),
                "source_path": source,
                "preset_id": entry["preset_id"],
                "preset_hash": entry["file_hash"],
                "preset_uuid": None
                if is_creative_look
                else entry.get("runtime_preset_uuid") or entry.get("uuid"),
                "preset_scope": entry.get("preset_scope") or "catalog",
                "look_kind": look_kind,
                **_creative_look_fields(entry),
                "profile_name": entry.get("profile_name"),
                "profile_hash": entry.get("profile_hash") or entry.get("file_hash"),
                "xmp_compatible": bool(entry.get("xmp_compatible")),
                "amount": amount,
                "size": 1024,
                "cache_key": preview_cache_key(
                    source_path=source,
                    preset_id=str(entry["preset_id"]),
                    preset_hash=str(entry["file_hash"]),
                    amount=amount,
                    base_hash=base_hash,
                    source_fingerprint=source_fingerprint,
                    crop_revision=crop_revision,
                    basic_color_revision=basic_color_revision,
                    lightroom_version=lightroom_version,
                    catalog_version=catalog_version,
                    plugin_version=plugin_version,
                    look_renderer_version=look_renderer_version,
                ),
                "status": "pending",
            }
        )
    return tasks


def rank_rendered_candidates(
    candidates: Iterable[dict[str, Any]],
    *,
    neutral_score: float,
    confidence_gap: float = 0.08,
    minimum_improvement: float = 0.03,
) -> dict[str, Any]:
    """Apply the declared four-signal ranking and compare it with no filter."""

    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        metrics = candidate.get("metrics") or {}
        required = set(RECOMMENDATION_WEIGHTS)
        if candidate.get("render_status") != "ready" or not required.issubset(metrics):
            continue
        score = sum(
            RECOMMENDATION_WEIGHTS[key] * _bounded(metrics[key]) for key in required
        )
        ranked.append({**candidate, "score": round(score, 6)})
    ranked.sort(
        key=lambda item: (-float(item["score"]), str(item.get("preset_id") or ""))
    )
    top3 = ranked[:3]
    if not ranked:
        return {
            "status": "pending",
            "selected": {"kind": "neutral", "preset_id": None, "amount": 0},
            "top3": [],
            "confidence": 0.0,
            "reason": "等待 Lightroom 真实预览与质量评分",
        }
    best = float(ranked[0]["score"])
    second = float(ranked[1]["score"]) if len(ranked) > 1 else float(neutral_score)
    improvement = best - float(neutral_score)
    gap = best - second
    confidence = _bounded(
        min(
            improvement / max(minimum_improvement * 4, 1e-6),
            gap / max(confidence_gap * 2, 1e-6),
        )
    )
    top_two_close = len(ranked) > 1 and gap < confidence_gap
    clearly_better_than_neutral = improvement >= minimum_improvement * 2
    use_neutral = improvement < minimum_improvement or (
        top_two_close and not clearly_better_than_neutral
    )
    if use_neutral:
        reason = (
            "推荐没有明显优于自然版本"
            if improvement < minimum_improvement
            else "改善较轻且前两名接近，暂时保持自然"
        )
        selected = {"kind": "neutral", "preset_id": None, "amount": 0}
    else:
        winner = ranked[0]
        reason = (
            "最佳结果明显优于自然版本，但备选效果接近；已采用最高分"
            if top_two_close
            else "通过 Lightroom 真实预览与四项质量复验"
        )
        selected = {
            "kind": "preset",
            "preset_id": winner["preset_id"],
            "preset_hash": winner.get("preset_hash"),
            "amount": winner.get("amount", 100),
            "look_kind": winner.get("look_kind") or "develop_preset",
            **_creative_look_fields(winner),
            "profile_name": winner.get("profile_name"),
            "profile_hash": winner.get("profile_hash"),
            "xmp_compatible": bool(winner.get("xmp_compatible")),
        }
    return {
        "status": "complete",
        "selected": selected,
        "top3": top3,
        "confidence": round(confidence, 4),
        "reason": reason,
        "neutral_score": round(float(neutral_score), 6),
    }


def create_recommendation_plan(
    run_id: str,
    groups: dict[str | int, list[dict[str, Any]]],
    catalog: dict[str, Any],
    run_dir: Path,
    *,
    scene_analyses: dict[str, dict[str, Any]] | None = None,
    clip_scores: dict[str, dict[str, float]] | None = None,
    probe_selections: dict[str, dict[str, Any]] | None = None,
    ai_stages: dict[str, dict[str, dict[str, Any]]] | None = None,
    pipeline_stages: dict[str, dict[str, Any]] | None = None,
    lightroom_available: bool = False,
) -> dict[str, Any]:
    """Create a resumable cascade plan without pretending missing AI ran."""

    scene_analyses = scene_analyses or {}
    clip_scores = clip_scores or {}
    probe_selections = probe_selections or {}
    ai_stages = ai_stages or {}
    result_groups: list[dict[str, Any]] = []
    for raw_group_id in sorted(groups, key=lambda value: str(value)):
        group_id = str(raw_group_id)
        probes = dict(
            probe_selections.get(group_id) or select_group_probes(groups[raw_group_id])
        )
        scene = scene_analyses.get(group_id)
        group_clip_scores = clip_scores.get(group_id)
        candidates, basis = recall_candidates(
            catalog,
            clip_scores=group_clip_scores,
            scene=scene,
        )
        render_tasks = build_render_tasks(group_id, probes, candidates)
        stages = dict(ai_stages.get(group_id) or {})
        missing: list[str] = []
        dino_stage = stages.get("dinov2") or {}
        qwen_stage = stages.get("qwen3_vl") or {}
        clip_stage = stages.get("clip") or {}
        if not bool(dino_stage.get("used", probes.get("basis") == "dinov2_medoid")):
            missing.append("dinov2_medoid")
        if not bool(qwen_stage.get("used", bool(scene))):
            missing.append("qwen3_vl_scene")
        if not bool(clip_stage.get("used", bool(group_clip_scores))):
            missing.append("clip_recall")
        if not lightroom_available:
            missing.append("lightroom_exact_preview")
        missing.append("qrealign_rerank")
        result_groups.append(
            {
                "group_id": group_id,
                "probes": probes,
                "scene": scene,
                "recall_basis": basis,
                "ai_stages": stages,
                "candidates": [
                    {
                        "preset_id": item["preset_id"],
                        "preset_hash": item["file_hash"],
                        "name": item["name"],
                        "category": item["category"],
                        "source": item.get("source"),
                        "preset_uuid": None
                        if item.get("look_kind") == "lightroom_profile"
                        else item.get("runtime_preset_uuid") or item.get("uuid"),
                        "preset_scope": item.get("preset_scope") or "catalog",
                        "look_kind": item.get("look_kind") or "lightroom_preset",
                        **_creative_look_fields(item),
                        "profile_name": item.get("profile_name"),
                        "profile_hash": item.get("profile_hash")
                        or item.get("file_hash"),
                        "xmp_compatible": bool(item.get("xmp_compatible")),
                        "amount_supported": bool(item.get("supports_amount")),
                        "scene_affinity": scene_preset_affinity(item, scene),
                        "clip_score": _bounded(
                            (group_clip_scores or {}).get(
                                str(item.get("preset_id")), 0.0
                            )
                        )
                        if group_clip_scores
                        else None,
                    }
                    for item in candidates
                ],
                "render_tasks": render_tasks,
                "status": "waiting" if missing else "ready_to_rank",
                "missing_stages": missing,
                "selected": {"kind": "neutral", "preset_id": None, "amount": 0},
                "top3": [],
                "confidence": 0.0,
                "manual_override": False,
            }
        )
    plan = {
        "schema_version": STYLE_RECOMMENDATION_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": _now(),
        "updated_at": _now(),
        "catalog_generated_at": catalog.get("generated_at"),
        "catalog_size": len(catalog.get("default_pool") or []),
        "ai_pipeline": {
            "order": ["dinov2", "qwen3_vl", "clip", "lightroom", "qrealign"],
            "stages": dict(pipeline_stages or {}),
        },
        "status": "waiting"
        if any(item["status"] == "waiting" for item in result_groups)
        else "ready_to_rank",
        "groups": result_groups,
    }
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "style-recommendations.json", plan)
    return plan


def load_recommendation_plan(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir) / "style-recommendations.json"
    return read_json(path) if path.is_file() else None


def update_group_selection(
    plan: dict[str, Any],
    run_dir: Path,
    group_id: str | int,
    *,
    preset_id: str | None,
    preset_hash: str | None = None,
    amount: int = 100,
    skipped: bool = False,
) -> dict[str, Any]:
    target = next(
        (
            item
            for item in plan.get("groups", [])
            if str(item.get("group_id")) == str(group_id)
        ),
        None,
    )
    if target is None:
        raise KeyError(f"未知照片组：{group_id}")
    if skipped or not preset_id:
        target["selected"] = {"kind": "neutral", "preset_id": None, "amount": 0}
        target["status"] = "skipped" if skipped else "confirmed"
    else:
        known = next(
            (
                item
                for item in target.get("candidates", [])
                if item.get("preset_id") == preset_id
            ),
            None,
        )
        if known is None:
            raise ValueError("选择的预设不属于该组当前候选。")
        expected_hash = str(known.get("preset_hash") or "")
        if preset_hash and expected_hash != preset_hash:
            raise ValueError("预设版本已变化，请重新生成预览。")
        target["selected"] = {
            "kind": "preset",
            "preset_id": preset_id,
            "preset_hash": expected_hash,
            "amount": min(200, max(0, int(amount))),
            "look_kind": known.get("look_kind") or "develop_preset",
            **_creative_look_fields(known),
            "profile_name": known.get("profile_name"),
            "profile_hash": known.get("profile_hash"),
            "xmp_compatible": bool(known.get("xmp_compatible")),
        }
        target["status"] = "confirmed"
    target["manual_override"] = True
    target["updated_at"] = _now()
    plan["updated_at"] = _now()
    write_json(Path(run_dir) / "style-recommendations.json", plan)
    return target
