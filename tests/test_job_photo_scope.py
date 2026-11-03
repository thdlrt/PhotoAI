import subprocess
import sys

import pytest

from landscape_culler.util import read_json, write_json
from test_photo_delete import setup, delete
from test_web import RUN_ID


def active_job(client, context, kind="develop", status="running"):
    job = {"id": "scope-test", "title": "test task", "kind": kind,
           "status": status, "context": context, "created_at": "2026-10-09"}
    client.app.state.jobs.jobs[job["id"]] = job
    return job


@pytest.mark.parametrize("action", ["edit", "confirm-all", "skip-crop", "options"])
@pytest.mark.parametrize("same_project", [False, True])
def test_develop_edits_follow_project_scope(tmp_path, monkeypatch, action, same_project):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    plan = client.post(f"/api/runs/{RUN_ID}/develop/skip-analysis", headers=headers,
                       json={"base_revision": 0})
    assert plan.status_code == 200, plan.text
    run_dir = data / "runs" / RUN_ID
    before = {file: file.read_bytes() for file in run_dir.glob("*.json")}
    active_job(client, {"run_id": RUN_ID} if same_project else
               {"run_id": "different-run", "input_root": str(tmp_path / "unrelated-photos")})
    body = {"base_revision": plan.json()["revision"]}
    if action == "edit":
        response = client.patch(f"/api/runs/{RUN_ID}/develop/0", headers=headers,
                                json={**body, "crop_id": "original", "confirmed": True})
    else:
        response = client.post(f"/api/runs/{RUN_ID}/develop/{action}", headers=headers,
                               json={**body, **({"mode": "skip"} if action == "options" else {})})
    assert response.status_code == (409 if same_project else 200), response.text
    if same_project:
        assert all(file.read_bytes() == content for file, content in before.items())
    assert raw.read_bytes() == b"raw"
    assert client.app.state.jobs.jobs["scope-test"]["status"] == "running"


@pytest.mark.parametrize("action", ["delete", "exclude", "group", "project"])
def test_other_project_task_does_not_lock_local_edits(tmp_path, monkeypatch, action):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    other_id = "20260831-130000-123456"
    payload = read_json(data / "runs" / RUN_ID / "results.json")
    payload.update(run_id=other_id, input_root=str(tmp_path / "other-input"))
    write_json(data / "runs" / other_id / "results.json", payload)
    active_job(client, {"run_id": other_id})  # Old records did not store input_root.
    assert client.get("/api/jobs").json()[0]["context"]["input_root"] == str(tmp_path / "other-input")
    before = (data / "runs" / other_id / "results.json").read_bytes()
    if action == "delete":
        response = delete(client, headers)
        assert not raw.exists()
    elif action == "project":
        project = client.get(f"/api/runs/{RUN_ID}").json()["project_id"]
        response = client.request("DELETE", f"/api/projects/{project}", headers=headers,
                                  json={"confirmation": "删除工程"})
        assert raw.exists()
    else:
        suffix, body = ("excluded", {"excluded": True}) if action == "exclude" else ("groups", {"new_group": True})
        response = client.patch(f"/api/runs/{RUN_ID}/{suffix}", headers=headers,
                                json={"indexes": [0], "base_revision": 0, **body})
    assert response.status_code == 200, response.text
    assert client.app.state.jobs.jobs["scope-test"]["status"] == "running"
    assert (data / "runs" / other_id / "results.json").read_bytes() == before


@pytest.mark.parametrize("scope", ["run", "same-folder", "parent", "child", "unknown", "group-argv"])
def test_own_or_overlapping_photos_stay_locked(tmp_path, monkeypatch, scope):
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    context = {"run_id": RUN_ID} if scope == "run" else {}
    if scope in {"same-folder", "parent", "child"}:
        path = {"same-folder": raw.parent, "parent": raw.parent.parent,
                "child": raw.parent / "nested"}[scope]
        context = {"run_id": "different-version", "input_root": str(path)}
    job = active_job(client, context)
    if scope == "group-argv":
        job.update(kind="group", retry_spec={"argv": ["group", "--input", str(raw.parent)]})
    before = (data / "runs" / RUN_ID / "results.json").read_bytes()
    response = delete(client, headers)
    assert response.status_code == 409, response.text
    assert raw.read_bytes() == b"raw"
    assert not (data / "runs" / RUN_ID / "review.json").exists()
    assert (data / "runs" / RUN_ID / "results.json").read_bytes() == before


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed", "interrupted"])
def test_terminal_tasks_release_photo_lock(tmp_path, monkeypatch, status):
    client, _, _, headers = setup(tmp_path, monkeypatch)
    active_job(client, {"run_id": RUN_ID}, status=status)
    assert delete(client, headers).status_code == 200


def test_cancel_real_owned_process_keeps_checkpoint_and_releases_lock(tmp_path, monkeypatch):
    import landscape_culler.web as web
    client, data, raw, headers = setup(tmp_path, monkeypatch)
    manager = client.app.state.jobs
    job = active_job(client, {"run_id": RUN_ID})
    checkpoint = data / "runs" / RUN_ID / "develop-checkpoint.json"
    checkpoint.write_bytes(b'{"finished": "keep"}')
    write_json(checkpoint.with_name("develop-progress.json"), {"status": "running", "completed": 1})
    monkeypatch.setattr(web, "_unload_local_vlm", lambda: None)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    manager.processes[job["id"]] = process
    try:
        progress = client.get(f"/api/runs/{RUN_ID}/develop/progress").json()
        assert progress["job_id"] == job["id"] and progress["cancellable"]
        assert delete(client, headers).status_code == 409
        response = client.delete(f"/api/jobs/{job['id']}", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert process.poll() is not None  # No lock release with a live process.
        assert checkpoint.read_bytes() == b'{"finished": "keep"}'
        assert client.get(f"/api/runs/{RUN_ID}/develop/progress").json()["status"] == "cancelled"
        assert delete(client, headers).status_code == 200
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_cancel_during_environment_setup_is_not_overwritten_by_launch_error(tmp_path, monkeypatch):
    import landscape_culler.web as web

    class IdleThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    client, _, _, _ = setup(tmp_path, monkeypatch)
    manager = client.app.state.jobs
    monkeypatch.setattr(web.threading, "Thread", IdleThread)
    job = manager.start("develop", [], {"run_id": RUN_ID})

    def cancelled_setup(item):
        manager.cancel(item["id"])
        raise OSError("environment setup stopped")

    monkeypatch.setattr(manager, "_job_environment", cancelled_setup)
    manager._run(job["id"])
    assert manager.get(job["id"])["status"] == "cancelled"
    assert manager.active() is None
