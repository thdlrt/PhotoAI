import pytest

from test_web import RUN_ID, _client
from landscape_culler.util import read_json, write_json


def setup_groups(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    result_file = data / "runs" / RUN_ID / "results.json"
    payload = read_json(result_file)
    payload["results"] = [{**payload["results"][0], "group_id": i} for i in (1, 2, 3)]
    write_json(result_file, payload)
    token = client.get("/api/bootstrap").json()["token"]
    return client, data, raw, {"X-Photo-AI-Token": token}


def test_order_persists_without_invalidating_scores_and_moves_follow_order(tmp_path, monkeypatch):
    client, data, raw, headers = setup_groups(tmp_path, monkeypatch)
    endpoint = f"/api/runs/{RUN_ID}"
    source = data / "runs" / RUN_ID / "results.json"
    before = source.read_bytes()
    response = client.patch(endpoint + "/group-order", headers=headers, json={
        "group_ids": [3, 1, 2], "base_revision": 0, "base_order_revision": 0,
    })
    assert response.status_code == 200
    detail = client.get(endpoint).json()
    assert detail["group_order"] == [3, 1, 2]
    assert detail["group_order_revision"] == 1
    assert detail["review_revision"] == 0
    assert detail["xmp_ready"] and not detail["needs_rescore"]
    response = client.patch(endpoint + "/groups", headers=headers, json={
        "indexes": [0], "direction": "previous", "base_revision": 0,
    })
    assert response.status_code == 200
    assert response.json()["items"][0]["group_id"] == 3
    assert client.get(endpoint).json()["group_order"] == [3, 2]
    assert read_json(source.parent / "review.json")["group_order_revision"] == 1
    assert source.read_bytes() == before and raw.read_bytes() == b"raw"


@pytest.mark.parametrize("order", [[1, 1, 2], [1, 2], [1, 2, 4]])
def test_invalid_orders_are_rejected(tmp_path, monkeypatch, order):
    client, _, _, headers = setup_groups(tmp_path, monkeypatch)
    assert client.patch(f"/api/runs/{RUN_ID}/group-order", headers=headers,
                        json={"group_ids": order}).status_code == 422


def test_legacy_project_opens_without_rewriting_existing_data(tmp_path, monkeypatch):
    client, data, raw, headers = setup_groups(tmp_path, monkeypatch)
    folder = data / "runs" / RUN_ID
    write_json(folder / "review.json", {"revision": 3, "ratings": {"0": 5},
                                       "groups": {}, "excluded": {}, "custom_metadata": {"keep": True}})
    before = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    detail = client.get(f"/api/runs/{RUN_ID}")
    assert detail.status_code == 200
    assert detail.json()["group_order"] == [1, 2, 3]
    assert detail.json()["results"][0]["rating"] == 5
    assert client.get("/api/projects").status_code == 200
    assert all(path.read_bytes() == content for path, content in before.items())
    assert client.patch(f"/api/runs/{RUN_ID}/group-order", headers=headers,
                        json={"group_ids": [3, 2, 1], "base_revision": 3}).status_code == 200
    review = read_json(folder / "review.json")
    assert review["ratings"] == {"0": 5}
    assert review["custom_metadata"] == {"keep": True}
    assert (folder / "results.json").read_bytes() == before[folder / "results.json"]
    assert raw.read_bytes() == b"raw"


def test_order_requires_auth_and_rejects_stale_writes(tmp_path, monkeypatch):
    client, _, _, headers = setup_groups(tmp_path, monkeypatch)
    endpoint = f"/api/runs/{RUN_ID}/group-order"
    body = {"group_ids": [2, 3, 1]}
    assert client.patch(endpoint, json=body).status_code == 403
    assert client.patch(endpoint, json=body, headers=headers).status_code == 200
    assert client.patch(endpoint, json=body, headers=headers).status_code == 409
    assert client.patch(endpoint, json={**body, "base_order_revision": 1}, headers=headers).json()["group_order_revision"] == 1


def test_order_survives_rating_and_exclusion_changes(tmp_path, monkeypatch):
    client, data, _, headers = setup_groups(tmp_path, monkeypatch)
    endpoint = f"/api/runs/{RUN_ID}"
    assert client.patch(endpoint + "/group-order", headers=headers,
                        json={"group_ids": [3, 1, 2]}).status_code == 200
    # Exercise the same persistence path used by the rating endpoint.
    from landscape_culler.web import _edit_review
    import threading
    _edit_review(data, RUN_ID, [0], 4, 0, threading.RLock())
    assert client.patch(endpoint + "/excluded", headers=headers,
                        json={"indexes": [1], "excluded": True, "base_revision": 1}).status_code == 200
    assert client.get(endpoint).json()["group_order"] == [3, 1]
