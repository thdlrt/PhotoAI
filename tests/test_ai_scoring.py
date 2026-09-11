from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from landscape_culler.constants import SCORING_PIPELINE_VERSION
from landscape_culler.fusion import (
    DEEP_WEIGHTS,
    FAST_WEIGHTS,
    _external_01,
    fuse_scores,
)
from landscape_culler.group_critic import OllamaGroupCritic
from landscape_culler.scoring import (
    _group_ids,
    _groups_from_file,
    _paths_from_grouping,
    assign_global_ratings,
    score_directory,
)
from landscape_culler.util import cache_key, write_json


def _features(count: int) -> np.ndarray:
    matrix = np.zeros((count, 8), dtype=np.float32)
    matrix[:, 0] = np.linspace(4.0, 7.0, count)
    matrix[:, 1] = 0.48
    matrix[:, 2] = 0.5
    matrix[:, 6] = 0.75
    return matrix


def _critique(identifier: str, rank: int, score: float) -> dict:
    return {
        "id": identifier,
        "composition": score,
        "light": score,
        "subject_layers": score,
        "color": score,
        "technical_quality": score,
        "edit_potential": score,
        "distraction": 100 - score,
        "rank": rank,
        "confidence": 0.8,
        "strengths": ["主体明确"],
        "issues": [],
        "summary": "主体和层次清楚",
    }


def test_fast_and_deep_fusion_are_finite_and_keep_components() -> None:
    general = [
        {"quality": 3.0, "aesthetic": 2.5},
        {"quality": 4.0, "aesthetic": 4.5},
        {"quality": 3.5, "aesthetic": 3.0},
    ]
    groups = [[0, 1], [2]]

    fast, components, reasons = fuse_scores(_features(3), groups, general, mode="fast")
    assert np.isfinite(fast).all()
    assert (fast >= 0).all() and (fast <= 1).all()
    assert fast[1] > fast[0]
    assert set(components[0]) == {"technical", "aesthetic", "quality"}
    assert reasons[0]["summary"]

    critiques = {
        0: _critique("IMG_01", 2, 35),
        1: _critique("IMG_02", 1, 90),
        2: _critique("IMG_01", 1, 65),
    }
    deep, deep_components, deep_reasons = fuse_scores(
        _features(3), groups, general, mode="deep", critiques=critiques
    )
    assert np.isfinite(deep).all()
    assert deep[1] > deep[0]
    assert "vlm" in deep_components[0]
    assert deep_reasons[0]["strengths"] == ["主体明确"]


def test_qrealign_zero_to_one_scores_are_not_remapped_as_one_to_five() -> None:
    assert _external_01(0.8) == 0.8
    assert _external_01(3.0) == 0.5


def test_tiny_qrealign_difference_is_not_amplified_to_rank_step() -> None:
    features = _features(2)
    features[1] = features[0]
    general = [
        {"quality": 0.71, "aesthetic": 0.608066},
        {"quality": 0.71, "aesthetic": 0.608727},
    ]
    scores, components, _reasons = fuse_scores(features, [[0, 1]], general, mode="fast")
    assert components[1]["aesthetic"] - components[0]["aesthetic"] < 0.001
    assert abs(float(scores[1] - scores[0])) < 0.001


def test_vlm_dimension_score_wins_when_model_rank_contradicts_it() -> None:
    features = _features(2)
    features[1] = features[0]
    general = [{"quality": 0.7, "aesthetic": 0.7}] * 2
    critiques = {
        0: _critique("IMG_01", 2, 90),
        1: _critique("IMG_02", 1, 55),
    }
    scores, _components, _reasons = fuse_scores(
        features, [[0, 1]], general, mode="deep", critiques=critiques
    )
    assert scores[0] > scores[1]


def test_general_signal_weights_are_exactly_normalized() -> None:
    assert np.isclose(sum(FAST_WEIGHTS.values()), 1.0)
    assert np.isclose(sum(DEEP_WEIGHTS.values()), 1.0)
    assert "personal" not in FAST_WEIGHTS
    assert "personal" not in DEEP_WEIGHTS


def test_score_directory_has_no_personal_model_dependency(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    paths = [input_root / "DSC0001.ARW", input_root / "DSC0002.ARW"]
    for path in paths:
        path.write_bytes(b"raw")
    data_dir = tmp_path / "data"
    assert not (data_dir / "models" / "personal-v1").exists()
    grouping_path = tmp_path / "legacy-v4-results.json"
    write_json(
        grouping_path,
        {
            "review_revision": 4,
            "results": [
                {
                    "path": str(paths[0]),
                    "source_key": cache_key(paths[0]),
                    "group_id": 1,
                    "excluded": False,
                },
                {
                    "path": str(paths[1]),
                    "source_key": cache_key(paths[1]),
                    "group_id": 2,
                    "excluded": True,
                    "personal_score": 0.9,
                    "components": {"personal": 0.9},
                    "keywords": ["AI|来源|个人偏好融合"],
                },
            ],
        },
    )

    class FakeExtractor:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def extract(self, requested):
            ordered = list(requested)
            return _features(len(ordered)), ordered

        def release(self) -> None:
            pass

    class FakeAestheticScorer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def score(self, requested):
            return [
                {"model": "test", "quality": 0.65, "aesthetic": 0.60}
                for _path in requested
            ]

        def release(self) -> None:
            pass

    class FakeCritic:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def unload(self) -> None:
            pass

    monkeypatch.setattr("landscape_culler.scoring.FeatureExtractor", FakeExtractor)
    monkeypatch.setattr(
        "landscape_culler.scoring.GeneralAestheticScorer", FakeAestheticScorer
    )
    monkeypatch.setattr("landscape_culler.scoring.OllamaGroupCritic", FakeCritic)

    published = score_directory(
        input_root,
        data_dir,
        retain_ratio=0.5,
        mode="fast",
        grouping_path=grouping_path,
    )
    payload = json.loads(Path(published["results_path"]).read_text(encoding="utf-8"))

    assert payload["pipeline_version"] == SCORING_PIPELINE_VERSION
    assert SCORING_PIPELINE_VERSION == "landscape-ai-v5-general"
    for item in payload["results"]:
        assert "personal_score" not in item
        assert "personal" not in item["components"]
        assert "AI|来源|个人偏好融合" not in item["keywords"]


def test_group_critic_uses_schema_and_cache(tmp_path: Path, monkeypatch) -> None:
    photos = [tmp_path / "one.jpg", tmp_path / "two.jpg"]
    for index, path in enumerate(photos):
        Image.new("RGB", (64, 48), (40 + index * 20, 80, 120)).save(path)
    critic = OllamaGroupCritic(tmp_path / "data")
    calls: list[dict] = []
    payload = {"items": [_critique("IMG_01", 2, 60), _critique("IMG_02", 1, 80)]}

    def fake_request(path: str, body: dict | None = None, timeout: float = 0) -> dict:
        calls.append(body or {})
        return {"message": {"content": json.dumps(payload, ensure_ascii=False)}}

    monkeypatch.setattr(critic, "_request", fake_request)
    first = critic.critique(photos)
    second = critic.critique(photos)

    assert [item["id"] for item in first] == ["IMG_01", "IMG_02"]
    assert second == first
    assert len(calls) == 1
    assert calls[0]["format"]["properties"]["items"]["minItems"] == 2
    assert calls[0]["think"] is False


def test_group_critic_rejects_duplicate_rank() -> None:
    payload = {"items": [_critique("IMG_01", 1, 60), _critique("IMG_02", 1, 80)]}
    try:
        OllamaGroupCritic._validate(payload, 2)
    except ValueError as exc:
        assert "名次" in str(exc)
    else:
        raise AssertionError("重复名次应被拒绝")


def test_group_critic_uses_only_content_root_ollama(
    tmp_path: Path, monkeypatch
) -> None:
    content_root = tmp_path / "PhotoAI"
    tools = content_root / "tools"
    executable = tools / "ollama-v-test" / "ollama.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ")
    external = tmp_path / "system" / "ollama.exe"
    external.parent.mkdir()
    external.write_bytes(b"MZ")
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(content_root))
    monkeypatch.setenv("PHOTO_AI_TOOLS_DIR", str(tools))
    monkeypatch.setenv("PHOTO_AI_OLLAMA", str(external))

    assert OllamaGroupCritic._portable_executable() == executable

    monkeypatch.setenv("PHOTO_AI_TOOLS_DIR", str(tmp_path / "outside"))
    assert OllamaGroupCritic._portable_executable() is None


def test_group_critic_repairs_duplicate_model_ranks(tmp_path: Path, monkeypatch) -> None:
    photos = [tmp_path / "one.jpg", tmp_path / "two.jpg"]
    for index, path in enumerate(photos):
        Image.new("RGB", (64, 48), (40 + index * 20, 80, 120)).save(path)
    critic = OllamaGroupCritic(tmp_path / "data")
    calls = 0
    payload = {"items": [_critique("IMG_01", 1, 60), _critique("IMG_02", 1, 80)]}

    def fake_request(path: str, body: dict | None = None, timeout: float = 0) -> dict:
        nonlocal calls
        calls += 1
        return {"message": {"content": json.dumps(payload, ensure_ascii=False)}}

    monkeypatch.setattr(critic, "_request", fake_request)
    result = critic.critique(photos)

    assert calls == 1
    assert [item["rank"] for item in result] == [2, 1]
    assert result[1]["composition"] == 80


def test_large_group_rebuilds_one_global_rank_with_anchor_calibration(tmp_path: Path, monkeypatch) -> None:
    paths = [tmp_path / f"{index}.jpg" for index in range(7)]
    critic = OllamaGroupCritic(tmp_path / "data")
    monkeypatch.setattr(critic, "ensure_ready", lambda: None)

    def fake_critique(chunk: list[Path]) -> list[dict]:
        if len(chunk) == 6:
            return [_critique(f"IMG_{index:02d}", index, 80 - index) for index in range(1, 7)]
        # The anchor drifts down by 10 in this context.  IMG_02 should be
        # adjusted by that same offset before the global ordering is rebuilt.
        return [_critique("IMG_01", 1, 69), _critique("IMG_02", 2, 75)]

    monkeypatch.setattr(critic, "critique", fake_critique)
    output = critic.critique_groups(paths, [list(range(7))], list(reversed(range(7))))
    ranks = [output[index]["rank"] for index in range(7)]
    assert sorted(ranks) == list(range(1, 8))
    assert output[6]["composition"] == 85
    assert output[6]["rank"] == 1


def test_global_rating_budget_is_not_one_winner_per_group() -> None:
    local = np.asarray([0.90, 0.20, 0.85, 0.80, 0.10], dtype=np.float64)
    global_scores = np.asarray([0.70, 0.20, 0.99, 0.30, 0.10], dtype=np.float64)
    ratings, pool, ranks = assign_global_ratings(local, global_scores, [[0, 1], [2], [3, 4]], 0.40)

    assert set(pool) == {0, 2, 3}
    assert ratings.tolist() == [3, 0, 4, 0, 0]
    assert ranks[2] == 1
    assert int((ratings >= 3).sum()) == 2


def test_singleton_groups_share_one_global_budget() -> None:
    scores = np.linspace(0.0, 0.9, 10)
    groups = [[index] for index in range(10)]
    ratings, pool, _ranks = assign_global_ratings(scores, scores, groups, 0.30)

    assert len(pool) == 10
    assert int((ratings >= 3).sum()) == 3
    assert ratings[9] == 4


def test_global_critic_prompt_uses_cross_group_rubric() -> None:
    prompt = OllamaGroupCritic._prompt(3, "global")
    assert "来自不同相似组" in prompt
    assert "跨组比较" in prompt


def test_excluded_group_round_trip_preserves_real_group_labels(tmp_path: Path) -> None:
    removed = tmp_path / "removed.ARW"
    active = tmp_path / "active.ARW"
    removed.write_bytes(b"removed")
    active.write_bytes(b"active")
    snapshot = tmp_path / "grouping.json"
    write_json(snapshot, {"results": [
        {"path": str(removed), "source_key": cache_key(removed), "group_id": 1, "excluded": True},
        {"path": str(active), "source_key": cache_key(active), "group_id": 2, "excluded": False},
    ]})

    active_paths, excluded = _paths_from_grouping([active, removed], snapshot)
    groups, labels = _groups_from_file(active_paths, snapshot)
    assert active_paths == [active]
    assert [Path(item["path"]) for item in excluded] == [removed]
    assert labels == [2]
    assert _group_ids(groups, len(active_paths), labels).tolist() == [2]

    payload = {"results": [
        {"path": str(removed), "source_key": cache_key(removed), "group_id": 1, "excluded": False},
        {"path": str(active), "source_key": cache_key(active), "group_id": 2, "excluded": False},
    ]}
    write_json(snapshot, payload)
    restored_paths, excluded = _paths_from_grouping([active, removed], snapshot)
    groups, labels = _groups_from_file(restored_paths, snapshot)
    mapping = dict(zip((path.name for path in restored_paths), _group_ids(groups, len(restored_paths), labels).tolist()))
    assert excluded == []
    assert mapping == {"active.ARW": 2, "removed.ARW": 1}
