from pathlib import Path

import pytest
from PIL import Image

from landscape_culler import develop, web
from landscape_culler.util import read_json, write_json
from test_web import RUN_ID, _append_result, _client


def test_skip_from_review_requires_no_models_and_allows_color_and_export(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(web, "active_model_profile_readiness", lambda *_a: pytest.fail("skip checked AI readiness"))
    monkeypatch.setattr(client.app.state.jobs, "start", lambda *_a: pytest.fail("skip launched worker"))
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *_a, **_kw: pytest.fail("skip ran AI crop"))
    source = data / "runs" / RUN_ID / "results.json"
    original = source.read_bytes()
    preview = Path(read_json(source)["results"][0]["preview"])
    preview_before = preview.read_bytes()
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    response = client.post(f"/api/runs/{RUN_ID}/develop/skip-analysis", headers=headers, json={"base_revision": 0})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["crop_skipped"] and plan["crop"]["status"] == "skipped"
    assert plan["confirmed_count"] == plan["eligible_count"] == 1
    assert plan["basic_color"]["status"] == "pending" and plan["color_mode"] == "pending"
    assert plan["items"][0]["crop_id"] == "original" and plan["items"][0]["angle"] == 0
    preview_response = client.get(plan["items"][0]["preview_url"])
    assert preview_response.status_code == 200
    import io
    with Image.open(io.BytesIO(preview_response.content)) as rendered:
        assert rendered.size == (640, 400)
    color = client.post(f"/api/runs/{RUN_ID}/develop/options", headers=headers,
                        json={"base_revision": plan["revision"], "mode": "skip"})
    assert color.status_code == 200, color.text
    assert color.json()["creative_style"]["status"] == "skipped"
    merged = develop.merge_confirmed_develop(read_json(source), read_json(source.parent / "develop.json"), 0)
    assert merged["results"][0]["develop"]["crop_id"] == "original"
    assert merged["results"][0]["develop"]["crop_status"] == "skipped"
    exported = client.post(f"/api/runs/{RUN_ID}/exports/prepare", headers=headers,
                           json={"base_revision": 0, "develop_revision": color.json()["revision"], "xmp": True, "jpeg": False})
    assert exported.status_code == 200, exported.text
    assert source.read_bytes() == original and preview.read_bytes() == preview_before and raw.read_bytes() == b"raw"
    assert not (source.parent / "develop-checkpoint.json").exists()
    assert not (source.parent / "develop-checkpoints").exists()


def test_skip_preserves_existing_color_choices_after_rating_change(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    source = data / "runs" / RUN_ID / "results.json"
    previous = develop.create_original_develop_plan(read_json(source), source.parent, 0)
    previous.update(color_mode="auto", basic_color={"status": "enabled"}, custom="preserve")
    previous["items"][0]["base"] = {"Exposure2012": .3}
    previous["items"][0]["style_strength"] = 42
    write_json(source.parent / "develop.json", previous)
    write_json(source.parent / "review.json", {"revision": 1, "ratings": {"0": 5}})
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *_a, **_kw: pytest.fail("AI called"))
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    result = client.post(f"/api/runs/{RUN_ID}/develop/skip-analysis", headers=headers, json={"base_revision": 1})
    assert result.status_code == 200, result.text
    current = read_json(source.parent / "develop.json")
    assert current["plan_id"] == previous["plan_id"] and current["custom"] == "preserve"
    assert current["basic_color"] == previous["basic_color"] and current["color_mode"] == "auto"
    assert current["items"][0]["base"] == {"Exposure2012": .3}
    assert current["items"][0]["style_strength"] == 42
    assert current["items"][0]["rating"] == 5 and current["source_review_revision"] == 1


@pytest.mark.parametrize("failure", ["revision", "active", "missing", "excluded", "unscored"])
def test_skip_rejects_invalid_state_without_replacing_plan(tmp_path, monkeypatch, failure):
    client, data, raw = _client(tmp_path, monkeypatch)
    source = data / "runs" / RUN_ID / "results.json"
    payload = read_json(source)
    develop.create_original_develop_plan(payload, source.parent, 0)
    before = (source.parent / "develop.json").read_bytes()
    if failure == "active":
        client.app.state.jobs.jobs["running"] = {"id": "running", "kind": "develop", "status": "running",
            "title": "test", "context": {"run_id": RUN_ID}}
    elif failure == "missing":
        Path(payload["results"][0]["preview"]).unlink()
    elif failure == "excluded":
        write_json(source.parent / "review.json", {"revision": 0, "excluded": {"0": True}})
    elif failure == "unscored":
        payload["workflow_state"] = "grouped"
        write_json(source, payload)
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    response = client.post(f"/api/runs/{RUN_ID}/develop/skip-analysis", headers=headers,
                           json={"base_revision": 9 if failure == "revision" else 0})
    assert response.status_code == 409, response.text
    assert (source.parent / "develop.json").read_bytes() == before


def test_skip_includes_only_current_candidates(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    _append_result(data, raw.parent / "zero.ARW", 2)
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    response = client.post(f"/api/runs/{RUN_ID}/develop/skip-analysis", headers=headers, json={"base_revision": 0})
    assert response.status_code == 200, response.text
    assert [item["index"] for item in response.json()["items"]] == [0]
