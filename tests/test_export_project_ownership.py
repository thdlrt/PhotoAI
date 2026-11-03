import copy
from pathlib import Path

import pytest

from landscape_culler.export_state import create_export_spec, export_spec_path, latest_export_for_run
from landscape_culler.util import read_json, write_json
from test_job_photo_scope import active_job
from test_web import RUN_ID, _client


def test_latest_export_is_project_scoped_timezone_aware_and_read_only(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    assert client.get(f"/api/runs/{RUN_ID}/exports/latest").json() is None
    def create(run_id, created):
        spec = create_export_spec(data, run_id=run_id, input_root=raw.parent,
            items=[{"path": str(raw), "rating": 4}], xmp=True, jpeg=False)
        spec["created_at"] = created
        write_json(export_spec_path(data, spec["export_spec_id"]), spec)
        return spec
    first = create(RUN_ID, "2026-10-10T00:00:00+08:00")
    latest = create(RUN_ID, "2026-10-09T18:00:00+00:00")
    create("another-project", "2027-01-01T00:00:00+00:00")
    bad = copy.deepcopy(first)
    bad.update(export_spec_id="export-" + "a" * 32, created_at="2028-01-01T00:00:00+00:00", items="corrupt")
    write_json(export_spec_path(data, bad["export_spec_id"]), bad)
    (data / "exports" / ("export-" + "b" * 32 + ".json")).write_text("broken", encoding="utf-8")
    snapshot = {path: path.read_bytes() for path in data.rglob("*.json")}
    response = client.get(f"/api/runs/{RUN_ID}/exports/latest")
    assert response.status_code == 200, response.text
    assert response.json()["export_spec_id"] == latest["export_spec_id"]
    assert all(path.read_bytes() == before for path, before in snapshot.items())
    assert raw.read_bytes() == b"raw"


def test_latest_lookup_does_not_create_a_data_directory(tmp_path):
    data = tmp_path / "missing"
    assert latest_export_for_run(data, "run") is None
    assert not data.exists()


@pytest.mark.parametrize("action", ["group", "global", "confirm", "prepare-export"])
@pytest.mark.parametrize("same_project", [True, False])
def test_style_and_export_preparation_are_locked_only_for_the_affected_project(tmp_path, monkeypatch, action, same_project):
    client, data, raw = _client(tmp_path, monkeypatch)
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    response = client.post(f"/api/runs/{RUN_ID}/develop/skip-analysis", headers=headers, json={"base_revision": 0})
    assert response.status_code == 200
    response = client.post(f"/api/runs/{RUN_ID}/develop/options", headers=headers,
                           json={"base_revision": response.json()["revision"], "mode": "skip"})
    assert response.status_code == 200
    revision = response.json()["revision"]
    folder = data / "runs" / RUN_ID
    if action == "confirm":
        plan = read_json(folder / "develop.json")
        plan["color_mode"] = "style"
        plan["creative_style"] = {"scope": "group", "status": "pending", "groups": {"1": {
            "group_id": 1, "status": "pending", "recommendation_status": "complete",
            "recommended_kind": "neutral", "neutral_preview_key": "a" * 64, "top3": []}}}
        write_json(folder / "develop.json", plan)
    elif action == "global":
        plan = read_json(folder / "develop.json")
        plan["creative_style"]["global_selection"] = {"status": "pending", "recommendation_status": "complete",
            "recommended_kind": "neutral", "neutral_preview_key": "a" * 64, "top3": []}
        write_json(folder / "develop.json", plan)
    before = {path: path.read_bytes() for path in folder.glob("*.json")}
    active_job(client, {"run_id": RUN_ID} if same_project else
               {"run_id": "other", "input_root": str(tmp_path / "other-photos")})
    if action == "prepare-export":
        response = client.post(f"/api/runs/{RUN_ID}/exports/prepare", headers=headers,
            json={"base_revision": 0, "develop_revision": revision, "xmp": True, "jpeg": False})
    elif action == "confirm":
        response = client.post(f"/api/runs/{RUN_ID}/develop/style/confirm-all", headers=headers,
                               json={"base_revision": revision})
    else:
        path = "groups/1" if action == "group" else "global"
        response = client.put(f"/api/runs/{RUN_ID}/develop/style/{path}", headers=headers,
                              json={"base_revision": revision, "status": "skipped"})
    assert response.status_code == (409 if same_project else 200), response.text
    if same_project:
        assert all(path.read_bytes() == content for path, content in before.items())
    assert raw.read_bytes() == b"raw"
    assert (folder / "results.json").read_bytes() == before[folder / "results.json"]
