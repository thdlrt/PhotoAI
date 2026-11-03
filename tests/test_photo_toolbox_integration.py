"""Boundary checks for the integrated photography toolbox, using real routers."""

from __future__ import annotations

import pytest

from landscape_culler.timelapse import TimelapseManager
from landscape_culler.util import read_json, write_json
from test_web import RUN_ID, _client


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    with client:
        headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
        yield client, data, raw, headers


def test_home_is_toolbox_with_selection_review_and_timelapse(integrated):
    client, _data, _raw, _headers = integrated
    page = client.get("/")
    assert page.status_code == 200
    assert "摄影后期工具箱" in page.text and 'id="view-toolbox" class="view active"' in page.text
    for marker in ('data-open-cull', 'data-toolbox-open="review"', 'data-toolbox-open="timelapse"',
                   'id="collaborative-review-root"', 'id="timelapse-root"', 'id="selection-output-root"'):
        assert marker in page.text
    for marker in ('data-workflow-stage="crop"', 'data-workflow-stage="base"',
                   'data-workflow-stage="style"', 'id="view-develop"', 'id="style-library-manage"'):
        assert marker not in page.text
    for script in ("toolbox-review.js", "toolbox-timelapse.js", "selection-output.js"):
        assert script in page.text
        assert client.get(f"/static/{script}").status_code == 200


@pytest.mark.parametrize("method,path", [
    ("POST", f"/api/runs/{RUN_ID}/develop"),
    ("POST", f"/api/runs/{RUN_ID}/develop/skip-analysis"),
    ("POST", f"/api/runs/{RUN_ID}/develop/skip-crop"),
    ("POST", f"/api/runs/{RUN_ID}/develop/options"),
    ("PATCH", f"/api/runs/{RUN_ID}/develop/0"),
    ("POST", f"/api/runs/{RUN_ID}/style-recommendations"),
    ("POST", f"/api/runs/{RUN_ID}/style-preview"),
    ("POST", f"/api/runs/{RUN_ID}/exports/prepare"),
    ("POST", "/api/exports/legacy-export/execute"),
    ("POST", "/api/style-library/import"),
    ("POST", "/api/style-library/sync"),
])
def test_retired_authoring_cannot_replay_old_adjustments(integrated, method, path):
    client, data, raw, headers = integrated
    folder = data / "runs" / RUN_ID
    write_json(folder / "develop.json", {"schema_version": 1, "items": [], "original": "retained"})
    before = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    response = client.request(method, path, headers=headers, json={"base_revision": 0})
    assert response.status_code == 410, response.text
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert raw.read_bytes() == b"raw"


def test_old_run_manual_choices_and_develop_are_read_only_when_opened(integrated):
    client, data, raw, headers = integrated
    folder = data / "runs" / RUN_ID
    write_json(folder / "review.json", {"revision": 7, "ratings": {"0": 5}, "groups": {},
                                        "excluded": {}, "custom_metadata": {"must_keep": "old note"}})
    write_json(folder / "develop.json", {"schema_version": 1, "source_review_revision": 7,
        "revision": 3, "items": [{"index": 0, "group_id": 1, "confirmed": True,
                                  "crop": {"left": 0.1, "right": 0.9}, "base": {"Exposure2012": 2.3}}],
        "custom_metadata": {"unrecognized": True}})
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir() if p.is_file()}
    detail = client.get(f"/api/runs/{RUN_ID}")
    assert detail.status_code == 200 and detail.json()["results"][0]["rating"] == 5
    assert client.get("/api/projects").status_code == 200
    plan = client.get(f"/api/runs/{RUN_ID}/develop")
    assert plan.status_code == 200
    exported = client.get(f"/api/runs/{RUN_ID}/selection-export/download?format=json")
    assert exported.status_code == 200
    assert exported.json()["items"][0]["rating"] == 5
    assert "develop" not in exported.json()["items"][0]
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == contents for p, contents in before.items())
    assert raw.read_bytes() == b"raw"


def test_three_new_routers_mounted_and_use_shared_mutation_token(integrated, monkeypatch):
    client, _data, raw, headers = integrated
    assert client.get("/api/review-workspaces").json() == {"workspaces": []}
    assert client.get("/api/toolbox/timelapse/sessions").json() == []
    assert client.get(f"/api/runs/{RUN_ID}/selection-export").json()["count"] == 1

    writes = [
        ("/api/review-workspaces", {"input_root": str(raw.parent)}),
        ("/api/toolbox/timelapse/scan", {"input_root": str(raw.parent)}),
        (f"/api/runs/{RUN_ID}/selection-export/copy",
         {"base_revision": 0, "output_dir": str(raw.parent.parent / "new-output")}),
    ]
    for path, body in writes:
        assert client.post(path, json=body).status_code == 403
        assert client.post(path, json=body, headers={"X-Photo-AI-Token": "wrong"}).status_code == 403
    assert not (raw.parent.parent / "new-output").exists()

    workspace = client.post(writes[0][0], json=writes[0][1], headers=headers)
    assert workspace.status_code == 200, workspace.text
    item = workspace.json()["items"][0]
    patch_path = f"/api/review-workspaces/{workspace.json()['workspace_id']}/items/{item['id']}"
    decision = {"revision": 0, "status": "keep", "quality": 0, "beauty": 3, "memory": 2,
                "notes": "manual decision", "order": 0, "chapter": "first"}
    assert client.patch(patch_path, json=decision).status_code == 403
    saved = client.patch(patch_path, json=decision, headers=headers)
    assert saved.status_code == 200 and saved.json()["items"][0]["user"]["status"] == "keep"
    assert client.get(f"/api/review-workspaces/{workspace.json()['workspace_id']}/export").status_code == 200

    monkeypatch.setattr(TimelapseManager, "submit", lambda _self, action, params, session_id=None:
                        {"id": "test-scan", "action": action, "status": "queued"})
    scan = client.post(writes[1][0], json=writes[1][1], headers=headers)
    assert scan.status_code == 200 and scan.json()["action"] == "scan"


@pytest.mark.parametrize("context", [{}, {"run_id": RUN_ID, "selection_only": False},
                                     {"run_id": RUN_ID, "selection_only": "true"}])
def test_legacy_xmp_job_cannot_replay_hidden_develop_recipe(integrated, monkeypatch, context):
    client, data, raw, headers = integrated
    frozen_path = data / "runs" / RUN_ID / "historical-export.json"
    write_json(frozen_path, {"results": [{"path": str(raw), "rating": 5, "score": 1,
                                         "develop": {"confirmed": True, "base": {"Exposure2012": 2.5}}}]})
    before = frozen_path.read_bytes()
    client.app.state.jobs.jobs["historical-xmp"] = {
        "id": "historical-xmp", "kind": "xmp_commit", "status": "failed",
        "context": {"results_path": str(frozen_path), **context},
        "retry_spec": {"kind": "xmp_commit", "cli_args": ["write-xmp", "--results", str(frozen_path), "--commit"]},
    }
    monkeypatch.setattr(client.app.state.jobs, "retry", lambda *_args: pytest.fail("old XMP task must not execute"))
    response = client.post("/api/jobs/historical-xmp/retry", headers=headers)
    assert response.status_code == 410, response.text
    assert frozen_path.read_bytes() == before and raw.read_bytes() == b"raw"
    assert not raw.with_suffix(".xmp").exists()


def test_new_selection_only_xmp_job_can_retry(integrated, monkeypatch):
    client, _data, _raw, headers = integrated
    client.app.state.jobs.jobs["selection-xmp"] = {
        "id": "selection-xmp", "kind": "xmp_commit", "status": "failed",
        "context": {"run_id": RUN_ID, "selection_only": True}, "retry_spec": {},
    }
    retried = []
    def retry(job_id):
        retried.append(job_id)
        return {"id": "retried-selection-xmp", "kind": "xmp_commit", "status": "queued"}
    monkeypatch.setattr(client.app.state.jobs, "retry", retry)
    response = client.post("/api/jobs/selection-xmp/retry", headers=headers)
    assert response.status_code == 200 and retried == ["selection-xmp"]
