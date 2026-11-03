from pathlib import Path

import numpy as np
import pytest

from landscape_culler import scoring
from landscape_culler.util import read_json, write_json
from test_photo_delete import setup
from test_web import RUN_ID


def test_classification_retains_unreadable_snapshot_and_scores_remaining(tmp_path, monkeypatch):
    folder = tmp_path / "photos"
    folder.mkdir()
    paths = [folder / f"DSC000{i}.ARW" for i in range(3)]
    for path in paths:
        path.write_bytes(b"unchanged source")
    requests = []

    class Extractor:
        def __init__(self, *args, **kwargs): self.failures = []
        def extract(self, requested):
            requests.append(list(requested))
            valid = [path for path in requested if path != paths[1]]
            self.failures = [(paths[1], "invalid RAW data")] if paths[1] in requested else []
            features = np.tile([6, .48, .5, 0, 0, 0, .75, 0], (len(valid), 1))
            return features, valid
        def release(self): pass

    class Scorer:
        def __init__(self, *args): pass
        def score(self, requested): return [{"aesthetic": .7, "quality": .7, "model": "fixture"}] * len(requested)
        def release(self): pass

    monkeypatch.setattr(scoring, "FeatureExtractor", Extractor)
    monkeypatch.setattr(scoring, "GeneralAestheticScorer", Scorer)
    monkeypatch.setattr(scoring.OllamaGroupCritic, "unload", lambda self: None)
    classified = scoring.classify_directory(folder, tmp_path / "state", mode="fast")
    snapshot = Path(classified["results_path"])
    before = snapshot.read_bytes()
    payload = read_json(snapshot)
    assert payload["image_count"] == 3 and payload["active_image_count"] == 2
    assert payload["excluded_count"] == 1
    assert payload["results"][-1]["read_error"] == "invalid RAW data"
    scored = scoring.score_directory(folder, tmp_path / "state", mode="fast", grouping_path=snapshot)
    assert requests[-1] == [paths[0], paths[2]]
    assert scored["active_image_count"] == 2 and scored["candidate_count"] == 1
    assert read_json(Path(scored["results_path"]))["results"][-1]["read_error"] == "invalid RAW data"
    assert snapshot.read_bytes() == before
    assert all(path.read_bytes() == b"unchanged source" for path in paths)
    with pytest.raises(RuntimeError, match="无法读取照片：DSC0001.ARW"):
        scoring.score_directory(folder, tmp_path / "state", mode="fast")


def test_unreadable_photo_cannot_be_restored_without_reclassification(tmp_path, monkeypatch):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    file = data / "runs" / RUN_ID / "results.json"
    payload = read_json(file)
    payload["results"][0].update(excluded=True, read_error="invalid RAW data")
    write_json(file, payload)
    before = file.read_bytes()
    response = client.patch(f"/api/runs/{RUN_ID}/excluded", headers=headers,
                            json={"indexes": [0], "excluded": False, "base_revision": 0})
    assert response.status_code == 422 and "修复源文件后重新分类" in response.text
    assert file.read_bytes() == before and raw.read_bytes() == b"raw"
    assert not (file.parent / "review.json").exists()
