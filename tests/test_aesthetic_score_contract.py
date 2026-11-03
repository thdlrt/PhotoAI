import copy
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from landscape_culler import general_aesthetic, style_worker
from landscape_culler.aesthetic_scores import qrealign_score
from landscape_culler.constants import AESTHETIC_MODEL_ID, AESTHETIC_PIPELINE_VERSION, PREVIEW_VERSION
from landscape_culler.fusion import fuse_scores
from landscape_culler.general_aesthetic import GeneralAestheticScorer
from landscape_culler.util import read_json, write_json
from test_ai_scoring import _features


def _cached(quality=0.6, aesthetic=0.7):
    return dict(model=AESTHETIC_MODEL_ID, pipeline_version=AESTHETIC_PIPELINE_VERSION,
                preview_version=PREVIEW_VERSION, quality=quality, aesthetic=aesthetic)


@pytest.mark.parametrize("invalid", [-5, -0.01, 1.05, 1.06, 5.5, 90, float("nan"), float("inf"), True, "0.8", None])
def test_invalid_scores_are_never_reinterpreted_as_another_scale(invalid):
    assert not GeneralAestheticScorer._valid(_cached(aesthetic=invalid))
    with pytest.raises(ValueError, match="Q-ReAlign"):
        qrealign_score(invalid)
    with pytest.raises(ValueError, match="Q-ReAlign"):
        fuse_scores(_features(1), [[0]], [_cached(quality=invalid)], mode="fast")


def test_valid_scores_are_continuous_and_keep_global_order():
    values = np.linspace(0, 1, 101)
    features = np.repeat(_features(1), len(values), axis=0)
    scores, components, _ = fuse_scores(features, [list(range(len(values)))],
        [_cached(quality=float(value), aesthetic=float(value)) for value in values], mode="fast")
    assert np.all(np.diff(scores) > 0)
    assert all(qrealign_score(value) == value for value in values)
    assert components[-1]["aesthetic"] == 1 and components[0]["aesthetic"] == 0


def test_float32_endpoint_roundoff_is_normalized_without_scale_guessing():
    low = float(np.float32(-1e-7))
    high = float(np.nextafter(np.float32(1), np.float32(2)))
    assert qrealign_score(low) == 0 and qrealign_score(high) == 1
    assert GeneralAestheticScorer._valid(_cached(quality=low, aesthetic=high))


def test_model_tensor_must_contain_exactly_one_valid_score():
    import torch
    assert GeneralAestheticScorer._scalar(torch.tensor([0.75])) == 0.75
    for value in (torch.tensor([]), torch.tensor([0.6, 0.8])):
        with pytest.raises(RuntimeError, match="一个评分"):
            GeneralAestheticScorer._scalar(value)
    with pytest.raises(ValueError):
        GeneralAestheticScorer._scalar(torch.tensor([True]))
    with pytest.raises(ValueError):
        GeneralAestheticScorer._scalar(torch.tensor([90.0]))


def test_valid_old_cache_stays_read_only_and_bad_photo_alone_is_rescored(tmp_path, monkeypatch):
    scorer = GeneralAestheticScorer(tmp_path / "data")
    paths = [tmp_path / f"{i}.ARW" for i in range(3)]
    for path in paths:
        path.write_bytes(b"untouched original")
        write_json(scorer._cache_path(path), _cached())
    first = scorer._cache_path(paths[0])
    write_json(first, _cached(quality=1.0000001))
    write_json(scorer._cache_path(paths[1]), _cached(aesthetic=5.5))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in [first, scorer._cache_path(paths[2]), *paths]}
    calls = []
    scorer._metric = lambda path, task_: (calls.append((path, task_)), 0.75)[1]
    monkeypatch.setattr(scorer, "_ensure_metric", lambda: None)
    monkeypatch.setattr(general_aesthetic, "load_preview", lambda *a: Image.new("RGB", (10, 10)))
    result = scorer.score(paths)
    assert len(calls) == 2 and len({path for path, task in calls}) == 1
    assert result[0]["quality"] == 1 and result[1]["aesthetic"] == 0.75
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == data for p, data in before.items())
    assert read_json(scorer._cache_path(paths[1]))["aesthetic"] == 0.75
    calls.clear()
    scorer.score(paths)
    assert calls == []


def test_invalid_model_response_never_publishes_cache_and_retry_recovers(tmp_path, monkeypatch):
    path = tmp_path / "photo.ARW"
    path.write_bytes(b"original")
    scorer = GeneralAestheticScorer(tmp_path / "data")
    scorer._metric = lambda path, task_: 0.7 if task_ == "quality" else 90.0
    monkeypatch.setattr(scorer, "_ensure_metric", lambda: None)
    monkeypatch.setattr(general_aesthetic, "load_preview", lambda *a: Image.new("RGB", (10, 10)))
    with pytest.raises(RuntimeError, match="photo.ARW.*0–1"):
        scorer.score([path])
    assert not scorer._cache_path(path).exists()
    scorer._metric = lambda path, task_: 0.7
    assert scorer.score([path])[0]["aesthetic"] == 0.7
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("count", [0, 2])
def test_fusion_rejects_missing_or_extra_photo_scores(count):
    with pytest.raises(ValueError, match="一一对应"):
        fuse_scores(_features(1), [[0]], [_cached()] * count, mode="fast")


def test_appearance_ranking_rejects_invalid_scores_without_changing_old_choice(tmp_path):
    group = dict(group_id=3, selected_preset_id="manual", confirmed=True,
        neutral_render_task=dict(render_status="ready", preview_path=str(tmp_path / "neutral.jpg")),
        candidates=[dict(preset_id="candidate")],
        render_tasks=[dict(render_status="ready", preview_path=str(tmp_path / "look.jpg"), preset_id="candidate")])
    previous = copy.deepcopy(group)
    class Scorer:
        def score(self, paths):
            return [_cached(), _cached(aesthetic=5.5)]
    with pytest.raises(style_worker.StyleWorkerError, match="照片组 3.*0–1"):
        style_worker._score_group(group, Scorer())
    assert group == previous
