import pytest

from test_web import RUN_ID, _append_result, _client
from landscape_culler.util import read_json, write_json
from landscape_culler.scoring import _raw_paths, _paths_from_grouping, _groups_from_file


def setup(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    _append_result(data, raw.parent / "keep.ARW", 2)
    token = client.get("/api/bootstrap").json()["token"]
    return client, data, raw, {"X-Photo-AI-Token": token}


def delete(client, headers, **changes):
    return client.post(f"/api/runs/{RUN_ID}/photos/delete", headers=headers,
                       json={"indexes": [0], "base_revision": 0,
                             "confirmation": "delete-source-files", **changes})


def test_delete_recycles_source_preserves_sidecar_and_manual_edits(tmp_path, monkeypatch):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    folder = data / "runs" / RUN_ID
    write_json(folder / "review.json", {"revision": 0, "ratings": {"1": 5},
               "groups": {"1": 7}, "group_order": [7, 1], "custom": "keep"})
    original_run = (folder / "results.json").read_bytes()
    raw.with_suffix(".xmp").write_bytes(b"user sidecar")
    response = delete(client, headers, indexes=[0, 0])
    assert response.status_code == 200, response.text
    recycled = list(raw.parent.glob(".photo-ai-trash/selection/*/DSC0001.ARW"))
    assert len(recycled) == 1 and recycled[0].read_bytes() == b"raw"
    assert not raw.exists()
    assert raw.with_suffix(".xmp").read_bytes() == b"user sidecar"
    assert (raw.parent / "keep.ARW").read_bytes() == b"raw"
    assert (folder / "results.json").read_bytes() == original_run
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["results"][0]["deleted"] and detail["results"][0]["excluded"]
    assert detail["deleted_count"] == 1 and detail["excluded_count"] == 0
    assert detail["active_image_count"] == 1 and detail["group_order"] == [7]
    assert detail["results"][1]["manual_rating"] == 5 and detail["needs_rescore"]
    assert read_json(folder / "review.json")["custom"] == "keep"
    assert delete(client, headers).status_code == 409
    assert client.patch(f"/api/runs/{RUN_ID}/excluded", headers=headers,
                        json={"indexes": [0], "excluded": False, "base_revision": 1}).status_code == 422


@pytest.mark.parametrize("changes,status", [
    ({"base_revision": 1}, 409), ({"indexes": [-1]}, 404),
    ({"indexes": [99]}, 404), ({"indexes": [0, 1]}, 422),
    ({"confirmation": ""}, 422),
])
def test_rejected_requests_do_not_touch_photos(tmp_path, monkeypatch, changes, status):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    assert delete(client, headers, **changes).status_code == status
    assert raw.read_bytes() == b"raw" and (raw.parent / "keep.ARW").exists()
    assert not (data / "runs" / RUN_ID / "review.json").exists()


def test_auth_and_outside_root_are_rejected(tmp_path, monkeypatch):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    assert delete(client, {}).status_code == 403
    outside = tmp_path / "outside.ARW"
    outside.write_bytes(b"protected")
    file = data / "runs" / RUN_ID / "results.json"
    payload = read_json(file)
    payload["results"][0]["path"] = str(outside)
    write_json(file, payload)
    assert delete(client, headers).status_code == 422
    assert outside.read_bytes() == b"protected" and raw.read_bytes() == b"raw"


def test_file_move_failure_rolls_back_batch(tmp_path, monkeypatch):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    _append_result(data, raw.parent / "second.ARW", 3)
    from pathlib import Path
    rename = Path.rename
    def fail_second(source, target):
        if source.name == "second.ARW" and ".photo-ai-trash" not in source.parts:
            raise PermissionError("locked test file")
        return rename(source, target)
    monkeypatch.setattr(Path, "rename", fail_second)
    assert delete(client, headers, indexes=[0, 2]).status_code == 409
    assert raw.read_bytes() == b"raw" and (raw.parent / "second.ARW").read_bytes() == b"raw"
    assert not (data / "runs" / RUN_ID / "review.json").exists()


def test_review_save_failure_restores_source(tmp_path, monkeypatch):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    import landscape_culler.web as web
    save = web.write_json
    def fail_review(path, payload):
        if path.name == "review.json":
            raise OSError("disk full")
        return save(path, payload)
    monkeypatch.setattr(web, "write_json", fail_review)
    assert delete(client, headers).status_code == 409
    assert raw.read_bytes() == b"raw"


def test_open_legacy_run_is_read_only(tmp_path, monkeypatch):
    client, data, raw, _ = setup(tmp_path, monkeypatch)
    folder = data / "runs" / RUN_ID
    write_json(folder / "review.json", {"revision": 3, "ratings": {"1": 5}, "custom": "keep"})
    before = {file: file.read_bytes() for file in folder.iterdir() if file.is_file()}
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["deleted_count"] == 0 and not detail["results"][0]["deleted"]
    assert all(file.read_bytes() == content for file, content in before.items())
    assert raw.read_bytes() == b"raw"


def test_delete_then_score_keeps_manual_groups_without_reclassification(tmp_path, monkeypatch):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    folder = data / "runs" / RUN_ID
    write_json(folder / "review.json", {"revision": 0, "ratings": {"1": 5}, "groups": {"1": 7}})
    assert delete(client, headers).status_code == 200
    captured = {}
    monkeypatch.setattr(client.app.state.jobs, "start", lambda kind, args, context:
                        captured.update(kind=kind, args=args, context=context) or {"id":"score", "status":"queued"})
    response = client.post(f"/api/runs/{RUN_ID}/score", headers=headers,
                           json={"base_revision": 1, "mode": "fast"})
    assert response.status_code == 200, response.text
    assert captured["kind"] == "score"
    snapshot = captured["context"]["grouping_snapshot"]
    from pathlib import Path
    active, excluded = _paths_from_grouping(_raw_paths(raw.parent), Path(snapshot))
    groups, labels = _groups_from_file(active, Path(snapshot))
    assert active == [raw.parent / "keep.ARW"] and not excluded
    assert groups == [[0]] and labels == [7]
    assert read_json(folder / "review.json")["ratings"] == {"1": 5}
    assert len(list(raw.parent.glob(".photo-ai-trash/selection/*/*.ARW"))) == 1
    failed = {"id":"old-score", "kind":"score", "status":"failed", "retry_spec":{},
              "message":"照片目录内容已变化，请重新分类后再评分。",
              "context":{"source_run_id":RUN_ID}}
    assert client.app.state.jobs.public(failed)["requires_reclassification"] is False
    (raw.parent / "new.ARW").write_bytes(b"new")
    assert client.app.state.jobs.public(failed)["requires_reclassification"] is True


def test_legacy_trash_records_are_hidden_without_writing_project_and_remaining_photos_delete(tmp_path, monkeypatch):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    archived = raw.parent / ".photo-ai-trash/selection/old/DSC0001.ARW"
    archived.parent.mkdir(parents=True)
    raw.rename(archived)
    folder = data / "runs" / RUN_ID
    payload = read_json(folder / "results.json")
    payload["results"][0]["path"] = str(archived)
    write_json(folder / "results.json", payload)
    _append_result(data, raw.parent / "another.ARW", 3)
    write_json(folder / "review.json", {"revision": 0, "ratings": {"1": 5}, "groups": {"1": 8}})
    before = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["active_image_count"] == 2 and detail["deleted_count"] == 1
    assert detail["results"][0]["deleted"] and detail["results"][0]["excluded"]
    assert detail["results"][1]["group_id"] == 8 and detail["results"][1]["manual_rating"] == 5
    assert all(path.read_bytes() == text for path, text in before.items())
    from landscape_culler.web import _materialize_grouping
    snapshot = _materialize_grouping(folder / "results.json")
    active, excluded = _paths_from_grouping(_raw_paths(raw.parent), snapshot)
    assert len(active) == 2 and not excluded
    assert _groups_from_file(active, snapshot)[1] == [3, 8]
    assert delete(client, headers, indexes=[2]).status_code == 200
    assert archived.read_bytes() == b"raw"  # Never delete or restore an archived file.


def test_recreated_project_classifies_only_remaining_sources(tmp_path, monkeypatch):
    import numpy as np
    import landscape_culler.scoring as scoring
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    assert delete(client, headers).status_code == 200
    project = client.get("/api/projects").json()[0]
    assert client.request("DELETE", f"/api/projects/{project['project_id']}", headers=headers,
                          json={"confirmation":"删除工程"}).status_code == 200
    created = client.post("/api/projects", headers=headers, json={"input_path":str(raw.parent)})
    assert created.status_code == 200
    assert not created.json()["versions"]

    class Extractor:
        def __init__(self, *args, **kwargs): pass
        def extract(self, paths):
            assert paths == [raw.parent / "keep.ARW"]
            return np.ones((len(paths), 8), dtype=np.float32), paths
        def release(self): pass

    monkeypatch.setattr(scoring, "FeatureExtractor", Extractor)
    result = scoring.classify_directory(raw.parent, data)
    classified = read_json(data / "runs" / result["run_id"] / "results.json")
    assert classified["image_count"] == 1
    assert [item["path"] for item in classified["results"]] == [str(raw.parent / "keep.ARW")]
    detail = client.get(f"/api/runs/{result['run_id']}").json()
    assert detail["project_id"] == created.json()["project_id"]
