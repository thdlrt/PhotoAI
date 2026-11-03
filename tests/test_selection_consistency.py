from pathlib import Path

import numpy as np
import pytest

from landscape_culler import scoring
from landscape_culler.fusion import fuse_scores, relative_sharpness_flags, technical_quality
from landscape_culler.group_critic import OllamaGroupCritic
from landscape_culler.util import cache_key, read_json, write_json


def test_strict_budget_keeps_all_top_photos_even_when_they_share_one_group():
    scores = np.array([.99, .98, .97, .2, .1, .05])
    groups = [[0, 1, 2], [3], [4], [5]]
    ratings, pool, ranks = scoring.assign_global_ratings(scores, scores, groups, .5)
    assert np.flatnonzero(ratings >= 3).tolist() == [0, 1, 2]
    assert pool == list(range(6))
    assert ranks == {index: index+1 for index in range(6)}
    # Changing grouping cannot displace a higher-scoring photo with a weaker
    # singleton or expand the budget to guarantee scene coverage.
    other, _, _ = scoring.assign_global_ratings(scores, scores, [[index] for index in range(6)], .5)
    np.testing.assert_array_equal(ratings, other)


@pytest.mark.parametrize("count,ratio", [(1,.05),(10,.3),(41,.3),(95,.2),(8,1.0)])
def test_global_budget_is_exact_and_deterministic(count, ratio):
    scores = np.full(count, .7)
    first, _, ranks = scoring.assign_global_ratings(scores, scores, [list(range(count))], ratio)
    expected = int(np.ceil(count*ratio))
    assert np.flatnonzero(first >= 3).tolist() == list(range(expected))
    assert len(ranks) == count


def test_global_vision_pass_covers_every_photo_with_bounded_anchored_requests(tmp_path, monkeypatch):
    photos = [tmp_path / f"{index}.jpg" for index in range(95)]
    critic = OllamaGroupCritic(tmp_path / "state")
    monkeypatch.setattr(critic, "ensure_ready", lambda: None)
    calls = []
    def critique(chunk, context):
        calls.append((list(chunk), context))
        result = []
        for rank, path in enumerate(chunk, 1):
            score = 50 + int(path.stem) / 95 * 40
            result.append({"id": f"IMG_{rank:02d}", "rank": rank,
                "composition": score, "light": score, "subject_layers": score,
                "color": score, "technical_quality": score, "edit_potential": score,
                "distraction": 100-score, "confidence": .8, "summary": "fixture"})
        return result
    monkeypatch.setattr(critic, "critique", critique)
    output = critic.critique_groups(photos, [list(range(95))], list(range(95)), context="global")
    assert set(output) == set(range(95))
    assert len(calls) == 19
    assert all(len(chunk) <= 6 and chunk[0] == photos[-1] and context == "global" for chunk,context in calls)
    assert sorted(item["rank"] for item in output.values()) == list(range(1, 96))
    assert output[94]["rank"] == 1


def batch_features():
    features = np.zeros((6, 8), dtype=np.float64)
    features[:, 0] = [1, 5, 6, 4, 8, 9]
    features[:, 1] = .48
    features[:, 2] = .5
    features[:, 6] = .75
    return features


def test_candidate_membership_cannot_change_measured_technical_score():
    features = batch_features()
    groups = [[0, 1, 2], [3, 4, 5]]
    selected = [2, 1, 5, 4]
    technical = technical_quality(features, groups)
    # Demonstrates the regression: calibrating the reduced set changes scores.
    recalibrated = technical_quality(features[selected], [[0, 1, 2, 3]])
    assert np.max(np.abs(recalibrated - technical[selected])) > .02
    general = [{"aesthetic": .7, "quality": .7}] * len(features)
    full, components, _ = fuse_scores(features, groups, general, mode="fast", technical_scores=technical)
    subset, subset_components, _ = fuse_scores(features[selected], [[0, 1, 2, 3]],
        [general[index] for index in selected], mode="fast", technical_scores=technical[selected])
    np.testing.assert_allclose(subset, full[selected], rtol=0, atol=0)
    assert subset_components == [components[index] for index in selected]


@pytest.mark.parametrize("invalid", [[.5], [.5] * 5 + [float("nan")], [.5] * 5 + [1.1]])
def test_invalid_reused_calibration_is_rejected(invalid):
    with pytest.raises(ValueError, match="技术评分"):
        fuse_scores(batch_features(), [[0, 1, 2, 3, 4, 5]],
                    [{"aesthetic": .7, "quality": .7}] * 6, mode="fast", technical_scores=invalid)


def test_sharpness_warning_requires_a_meaningful_similar_frame_reference():
    features = batch_features()
    features[:, 0] = 6
    assert not relative_sharpness_flags(features, [[0, 1, 2], [3, 4, 5]]).any()
    features[:, 0] = [1, 1.01, 1.02, 7, 7.01, 7.02]
    # A low-texture scene must not be marked because another scene has foliage.
    assert not relative_sharpness_flags(features, [[0, 1, 2], [3, 4, 5]]).any()
    features[0, 0] = 0
    assert relative_sharpness_flags(features, [[0, 1, 2], [3, 4, 5]]).tolist() == [True, False, False, False, False, False]
    assert not relative_sharpness_flags(features, [[index] for index in range(6)]).any()


@pytest.mark.parametrize("mode", ["fast", "deep"])
def test_whole_scoring_run_reuses_calibration_and_preserves_grouping(tmp_path: Path, monkeypatch, mode):
    inputs = tmp_path / "photos"
    inputs.mkdir()
    paths = [inputs / f"DSC{index:04d}.ARW" for index in range(6)]
    for path in paths:
        path.write_bytes(b"QA RAW source")
    features = batch_features()
    groups = [[0, 1, 2], [3, 4, 5]]
    general = [{"aesthetic": .65 + index * .01, "quality": .7, "model": "fixture"} for index in range(6)]
    snapshot = tmp_path / "grouping.json"
    write_json(snapshot, {"review_revision": 3, "group_order": [2, 1], "results": [
        {"path": str(path), "source_key": cache_key(path), "group_id": index // 3 + 1}
        for index, path in enumerate(paths)]})
    original_snapshot = snapshot.read_bytes()
    class Extractor:
        def __init__(self, *args, **kwargs): pass
        def extract(self, requested): return features, list(requested)
        def release(self): pass
    class Scorer:
        def __init__(self, *args, **kwargs): pass
        def score(self, requested): return general
        def release(self): pass
    critiques = []
    class Critic:
        def __init__(self, *args, **kwargs): pass
        def unload(self): pass
        def critique_groups(self, requested, selected_groups, scores, context="group"):
            critiques.append(context)
            return {index: {"composition": 75, "light": 75, "subject_layers": 75,
                "color": 75, "technical_quality": 75, "edit_potential": 75, "distraction": 25,
                "summary": "fixture", "confidence": .8} for group in selected_groups for index in group}
    monkeypatch.setattr(scoring, "FeatureExtractor", Extractor)
    monkeypatch.setattr(scoring, "GeneralAestheticScorer", Scorer)
    monkeypatch.setattr(scoring, "OllamaGroupCritic", Critic)
    published = scoring.score_directory(inputs, tmp_path / "state", mode=mode, retain_ratio=.5, grouping_path=snapshot)
    result = read_json(Path(published["results_path"]))
    expected_technical = technical_quality(features, groups)
    for index, item in enumerate(result["results"]):
        assert item["components"]["technical"] == round(float(expected_technical[index]), 6)
        # Identical VLM dimensions on pass two cannot alter score via recalibration.
        assert item["score"] == item["local_score"]
    assert result["candidate_count"] == 3
    assert result["group_order"] == [2, 1]
    assert snapshot.read_bytes() == original_snapshot
    assert all(path.read_bytes() == b"QA RAW source" for path in paths)
    assert critiques == (["global"] if mode == "deep" else [])
    assert result["selection_strategy"] == "strict_global_budget_v2"
    assert result["selection_pool_count"] == len(paths)
