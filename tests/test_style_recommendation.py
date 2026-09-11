from __future__ import annotations

from pathlib import Path

import pytest

from landscape_culler.style_recommendation import (
    build_render_tasks,
    canonical_pool_entries,
    create_recommendation_plan,
    preview_cache_key,
    rank_rendered_candidates,
    recall_candidates,
    select_group_probes,
    update_group_selection,
)


def _catalog() -> dict:
    return {
        "generated_at": "now",
        "default_pool": ["uuid:a", "uuid:b", "uuid:c"],
        "entries": [
            {
                "preset_id": "uuid:a",
                "file_hash": "hash-a",
                "name": "Alpine",
                "category": "landscape",
            },
            {
                "preset_id": "uuid:b",
                "file_hash": "hash-b",
                "name": "Blue Hour",
                "category": "tone",
            },
            {
                "preset_id": "uuid:c",
                "file_hash": "hash-c",
                "name": "Cinema",
                "category": "film",
            },
        ],
    }


def test_probe_selection_uses_marked_medoid_and_brightness_extremes() -> None:
    probes = select_group_probes(
        [
            {"path": "b.arw", "technical": {"brightness": 0.9}},
            {"path": "a.arw", "technical": {"brightness": 0.1}},
            {
                "path": "c.arw",
                "is_representative": True,
                "technical": {"brightness": 0.5},
            },
        ]
    )
    assert probes == {
        "representative": "c.arw",
        "brightest": "b.arw",
        "darkest": "a.arw",
        "basis": "dinov2_medoid",
    }


def test_probe_selection_prefers_original_raw_over_browser_preview() -> None:
    probes = select_group_probes(
        [
            {
                "path": "original.ARW",
                "preview": "cache.jpg",
                "preview_path": "develop.jpg",
                "is_representative": True,
            }
        ]
    )
    assert probes["representative"] == "original.ARW"


def test_recall_uses_canonical_rows_and_changes_with_scene() -> None:
    catalog = {
        "default_pool": ["uuid:a", "uuid:b", "uuid:c"],
        "entries": [
            {
                "preset_id": "uuid:a",
                "file_hash": "a",
                "name": "Alpine",
                "category": "landscape",
                "ai_eligible": False,
                "source_kind": "managed",
            },
            {
                "preset_id": "uuid:a",
                "file_hash": "a",
                "name": "Alpine",
                "category": "landscape",
                "ai_eligible": True,
                "source_kind": "adobe-installed",
            },
            {
                "preset_id": "uuid:b",
                "file_hash": "b",
                "name": "Blue Hour",
                "category": "tone",
                "ai_eligible": True,
            },
            {
                "preset_id": "uuid:c",
                "file_hash": "c",
                "name": "Cinema",
                "category": "film",
                "ai_eligible": True,
            },
        ],
    }
    assert [item["preset_id"] for item in canonical_pool_entries(catalog)] == [
        "uuid:a",
        "uuid:b",
        "uuid:c",
    ]
    mountain, mountain_basis = recall_candidates(
        catalog, scene={"summary": "mountain lake landscape"}
    )
    blue_hour, blue_basis = recall_candidates(
        catalog, scene={"summary": "quiet blue hour city"}
    )
    assert mountain_basis == blue_basis == "scene_heuristic"
    assert mountain[0]["preset_id"] == "uuid:a"
    assert blue_hour[0]["preset_id"] == "uuid:b"


def test_canonical_pool_excludes_runtime_enumeration_miss() -> None:
    catalog = _catalog()
    catalog["entries"][1]["runtime_resolvable"] = False

    assert [item["preset_id"] for item in canonical_pool_entries(catalog)] == [
        "uuid:a",
        "uuid:c",
    ]


def test_missing_models_create_resumable_neutral_plan_not_fake_ai_result(
    tmp_path: Path,
) -> None:
    plan = create_recommendation_plan(
        "run-1",
        {2: [{"path": "b.arw"}, {"path": "a.arw"}]},
        _catalog(),
        tmp_path,
        lightroom_available=False,
    )
    group = plan["groups"][0]
    assert plan["status"] == "waiting"
    assert group["selected"]["kind"] == "neutral"
    assert group["top3"] == []
    assert group["confidence"] == 0
    assert set(group["missing_stages"]) >= {
        "qwen3_vl_scene",
        "clip_recall",
        "lightroom_exact_preview",
        "qrealign_rerank",
    }
    assert len(group["render_tasks"]) == 3
    assert all(item["status"] == "pending" for item in group["render_tasks"])


def test_creative_look_candidates_use_descriptor_identity_not_preset_uuid() -> None:
    descriptor = {
        "SchemaVersion": 1,
        "UUID": "LOOK-UUID",
        "Name": "Film Look",
        "Group": "Film",
        "Cluster": "Adobe",
        "SupportsAmount": True,
        "Parameters": {"Saturation": -12},
        "TableDigests": {},
        "ComplexParameterDigests": {},
        "Hash": "descriptor-hash",
    }
    tasks = build_render_tasks(
        1,
        {"representative": "E:/photos/a.arw"},
        [
            {
                "preset_id": "uuid:look",
                "file_hash": "file-hash",
                "runtime_preset_uuid": "MUST-NOT-BE-USED",
                "uuid": "LOOK-UUID",
                "look_kind": "lightroom_profile",
                "look_descriptor": descriptor,
                "look_descriptor_hash": "descriptor-hash",
            }
        ],
        limit=1,
    )

    assert tasks[0]["preset_uuid"] is None
    assert tasks[0]["look_descriptor"] == descriptor
    assert tasks[0]["look_descriptor_hash"] == "descriptor-hash"
    assert tasks[0]["look_uuid"] == "LOOK-UUID"


def test_ranking_selects_real_improvement_and_low_confidence_keeps_natural() -> None:
    strong = rank_rendered_candidates(
        [
            {
                "preset_id": "uuid:a",
                "preset_hash": "a",
                "amount": 80,
                "render_status": "ready",
                "metrics": {
                    "style_match": 0.95,
                    "aesthetic_improvement": 0.90,
                    "technical_quality": 0.85,
                    "group_consistency": 0.92,
                },
            },
            {
                "preset_id": "uuid:b",
                "render_status": "ready",
                "metrics": {
                    "style_match": 0.40,
                    "aesthetic_improvement": 0.45,
                    "technical_quality": 0.50,
                    "group_consistency": 0.45,
                },
            },
        ],
        neutral_score=0.5,
    )
    assert strong["selected"]["preset_id"] == "uuid:a"
    assert strong["selected"]["amount"] == 80
    assert strong["confidence"] > 0

    uncertain = rank_rendered_candidates(
        [
            {
                "preset_id": "uuid:a",
                "render_status": "ready",
                "metrics": dict.fromkeys(
                    (
                        "style_match",
                        "aesthetic_improvement",
                        "technical_quality",
                        "group_consistency",
                    ),
                    0.64,
                ),
            },
            {
                "preset_id": "uuid:b",
                "render_status": "ready",
                "metrics": dict.fromkeys(
                    (
                        "style_match",
                        "aesthetic_improvement",
                        "technical_quality",
                        "group_consistency",
                    ),
                    0.635,
                ),
            },
        ],
        neutral_score=0.6,
    )
    assert uncertain["selected"]["kind"] == "neutral"
    assert "改善较轻" in uncertain["reason"]


def test_ranking_keeps_clear_winner_when_top_two_are_nearly_tied() -> None:
    result = rank_rendered_candidates(
        [
            {
                "preset_id": "uuid:best",
                "preset_hash": "best-hash",
                "render_status": "ready",
                "metrics": dict.fromkeys(
                    (
                        "style_match",
                        "aesthetic_improvement",
                        "technical_quality",
                        "group_consistency",
                    ),
                    0.658427,
                ),
            },
            {
                "preset_id": "uuid:close-second",
                "render_status": "ready",
                "metrics": dict.fromkeys(
                    (
                        "style_match",
                        "aesthetic_improvement",
                        "technical_quality",
                        "group_consistency",
                    ),
                    0.658321,
                ),
            },
        ],
        neutral_score=0.584191,
        confidence_gap=0.02,
        minimum_improvement=0.015,
    )

    assert result["selected"]["preset_id"] == "uuid:best"
    assert result["top3"][0]["score"] == pytest.approx(0.658427)
    assert result["confidence"] < 0.01
    assert "备选效果接近" in result["reason"]


def test_cache_version_and_manual_selection_guard_preset_hash(tmp_path: Path) -> None:
    first = preview_cache_key(
        source_path="A.ARW", preset_id="uuid:a", preset_hash="v1", amount=100
    )
    assert first == preview_cache_key(
        source_path="a.arw", preset_id="uuid:a", preset_hash="v1", amount=100
    )
    assert first != preview_cache_key(
        source_path="a.arw", preset_id="uuid:a", preset_hash="v2", amount=100
    )
    plan = create_recommendation_plan(
        "run", {1: [{"path": "a.arw"}]}, _catalog(), tmp_path
    )
    with pytest.raises(ValueError, match="版本"):
        update_group_selection(plan, tmp_path, 1, preset_id="uuid:a", preset_hash="old")
    selected = update_group_selection(
        plan, tmp_path, 1, preset_id="uuid:a", preset_hash="hash-a", amount=75
    )
    assert selected["selected"]["amount"] == 75
    assert selected["manual_override"] is True


def test_preview_cache_key_covers_source_revisions_and_lightroom_runtime() -> None:
    baseline = {
        "source_path": "E:/photos/A.ARW",
        "preset_id": "uuid:a",
        "preset_hash": "preset-v1",
        "amount": 100,
        "base_hash": "base-v1",
        "source_fingerprint": "raw-v1",
        "crop_revision": "crop-v1",
        "basic_color_revision": "basic-v1",
        "lightroom_version": "15.3.0.0",
        "catalog_version": "catalog-v1",
        "plugin_version": "0.3.1",
        "look_renderer_version": "creative-look-v1",
    }
    first = preview_cache_key(**baseline)

    mutations = {
        "source_fingerprint": "raw-v2",
        "crop_revision": "crop-v2",
        "basic_color_revision": "basic-v2",
        "lightroom_version": "15.4.0.0",
        "catalog_version": "catalog-v2",
        "plugin_version": "0.3.2",
        "look_renderer_version": "creative-look-v2",
    }
    changed = {
        preview_cache_key(**{**baseline, field: value})
        for field, value in mutations.items()
    }

    assert len(changed) == len(mutations)
    assert first not in changed
