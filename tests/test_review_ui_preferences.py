import pytest

from test_web import _client, RUN_ID
from landscape_culler.settings_transfer import read_local_preferences, write_local_preferences


def test_thumbnail_size_persists_without_changing_project_or_other_preferences(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    preferences = read_local_preferences(data)
    preferences["workflow_defaults"] = {"retain_ratio": 0.45, "mode": "fast"}
    preferences["model_profile_preference"] = "16gb"
    write_local_preferences(data, preferences)
    source = data / "runs" / RUN_ID / "results.json"
    before = source.read_bytes()
    token = client.get("/api/bootstrap").json()["token"]
    assert client.patch("/api/preferences/ui", json={"thumbnail_size": 320}).status_code == 403
    response = client.patch("/api/preferences/ui", headers={"X-Photo-AI-Token": token},
                            json={"thumbnail_size": 320})
    assert response.status_code == 200 and response.json()["thumbnail_size"] == 320
    loaded = read_local_preferences(data)
    assert loaded["workflow_defaults"] == preferences["workflow_defaults"]
    assert loaded["model_profile_preference"] == "16gb"
    assert client.get("/api/bootstrap").json()["preferences"]["ui"]["thumbnail_size"] == 320
    assert client.get("/api/settings-transfer/export").json()["ui"]["thumbnail_size"] == 320
    assert source.read_bytes() == before and raw.read_bytes() == b"raw"


@pytest.mark.parametrize("size", [0, 150, 430, 211, True, "320", 320.1])
def test_thumbnail_size_rejects_invalid_values(tmp_path, monkeypatch, size):
    client, data, _ = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    before = read_local_preferences(data)
    assert client.patch("/api/preferences/ui", headers={"X-Photo-AI-Token": token},
                        json={"thumbnail_size": size}).status_code == 422
    assert read_local_preferences(data) == before
