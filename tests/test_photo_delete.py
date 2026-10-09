import pytest

from test_web import RUN_ID, _append_result, _client
from landscape_culler.util import read_json, write_json


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
