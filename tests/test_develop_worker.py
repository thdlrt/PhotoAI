from __future__ import annotations

import json
from pathlib import Path

import pytest

from landscape_culler import develop, develop_worker
from landscape_culler.progress import PROGRESS_PREFIX
from landscape_culler.util import read_json, write_json


def _input(path: Path, revision: int = 3) -> None:
    write_json(
        path,
        {
            "schema_version": develop_worker.DEVELOP_INPUT_SCHEMA,
            "review_revision": revision,
            "payload": {"run_id": "run-1", "results": []},
        },
    )


def test_develop_worker_owns_progress_and_delegates_heavy_plan(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    input_path = tmp_path / "input.json"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _input(input_path)
    calls: list[tuple[dict, Path, int]] = []

    def fake_create(payload, target, revision, progress=None):
        calls.append((payload, target, revision))
        assert progress is not None
        progress(
            {
                "status": "running",
                "phase": "rank",
                "stage_label": "评估构图候选",
                "current": 1,
                "completed": 0,
                "total": 2,
                "overall_percent": 37.5,
                "nodes": [
                    {"key": "rank", "label": "AI 复评", "status": "active"}
                ],
            }
        )
        return {"run_id": "run-1", "plan_id": "develop-1", "items": [{}, {}]}

    monkeypatch.setattr(develop, "create_develop_plan", fake_create)
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")

    result = develop_worker.run_develop_plan_job(input_path, run_dir, 3)

    assert result == {
        "run_id": "run-1",
        "plan_id": "develop-1",
        "review_revision": 3,
        "eligible_count": 2,
    }
    assert calls == [({"run_id": "run-1", "results": []}, run_dir, 3)]
    completed = read_json(run_dir / "develop-progress.json")
    assert completed["status"] == "completed"
    assert completed["overall_percent"] == 100.0
    events = [
        json.loads(line[len(PROGRESS_PREFIX) :])
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(PROGRESS_PREFIX)
    ]
    assert events[0]["phase"] == "develop"
    assert events[0]["current"] == 375
    assert events[-1]["current"] == events[-1]["total"] == 1000


def test_develop_worker_rejects_managed_paths_outside_content_root(
    tmp_path: Path, monkeypatch
) -> None:
    content = tmp_path / "content"
    jobs = content / "state" / "web" / "jobs"
    run_dir = content / "projects" / "run-1"
    jobs.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    input_path = jobs / "input.json"
    _input(input_path)
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(content))
    monkeypatch.setenv("PHOTO_AI_PROJECTS_DIR", str(content / "projects"))

    with pytest.raises(ValueError, match="state/web/jobs"):
        develop_worker.run_develop_plan_job(tmp_path / "outside.json", run_dir, 3)
    with pytest.raises(ValueError, match="projects"):
        develop_worker.run_develop_plan_job(input_path, tmp_path / "outside-run", 3)


def test_develop_worker_records_failure_without_replacing_cached_plan(
    tmp_path: Path, monkeypatch
) -> None:
    input_path = tmp_path / "input.json"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _input(input_path)
    cached_plan = run_dir / "develop.json"
    cached_plan.write_text('{"existing":true}', encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic crop failure")

    monkeypatch.setattr(develop, "create_develop_plan", fail)
    with pytest.raises(RuntimeError, match="synthetic crop failure"):
        develop_worker.run_develop_plan_job(input_path, run_dir, 3)

    assert cached_plan.read_text(encoding="utf-8") == '{"existing":true}'
    progress = read_json(run_dir / "develop-progress.json")
    assert progress["status"] == "failed"
    assert "synthetic crop failure" in progress["message"]


@pytest.mark.parametrize("failure", [False, True])
def test_locked_progress_does_not_abort_plan_or_hide_real_failure(
    tmp_path, monkeypatch, capsys, failure
):
    input_path = tmp_path / "input.json"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _input(input_path)
    progress_path = run_dir / "develop-progress.json"
    write_json(progress_path, {"status": "running", "completed": 7})
    before = progress_path.read_bytes()
    persistence_attempts = []

    def locked_write(path, payload):
        if path == progress_path:
            persistence_attempts.append(payload)
            raise PermissionError("synthetic locked progress")
        write_json(path, payload)

    def create(payload, target, revision, progress):
        for position in range(34):
            progress({"status": "running", "current": position + 1, "total": 34,
                      "overall_percent": (position + 1) / 34 * 100})
        if failure:
            raise RuntimeError("real crop failure")
        plan = {"run_id": "run-1", "plan_id": "develop-locked",
                "items": [{"index": index} for index in range(34)]}
        write_json(target / "develop.json", plan)
        return plan

    monkeypatch.setattr(develop_worker, "write_json", locked_write)
    monkeypatch.setattr(develop_worker, "monotonic", lambda: 0.0)
    monkeypatch.setattr(develop, "create_develop_plan", create)
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    if failure:
        with pytest.raises(RuntimeError, match="real crop failure"):
            develop_worker.run_develop_plan_job(input_path, run_dir, 3)
    else:
        result = develop_worker.run_develop_plan_job(input_path, run_dir, 3)
        assert result["eligible_count"] == 34
        assert len(read_json(run_dir / "develop.json")["items"]) == 34
        assert '1000' in capsys.readouterr().out
    assert progress_path.read_bytes() == before
    assert len(persistence_attempts) == 2  # First and terminal event, no repeated stalls.


def test_real_34_photo_crop_plan_completes_despite_intermittent_progress_locks(tmp_path, monkeypatch):
    from PIL import Image
    preview = tmp_path / "preview.jpg"
    Image.new("RGB", (160, 100), "#719baf").save(preview)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    input_path = tmp_path / "input.json"
    write_json(input_path, {"schema_version": 1, "review_revision": 3,
                           "payload": {"run_id": "run-34", "results": [
                               {"path": str(tmp_path / f"{i}.ARW"), "preview": str(preview),
                                "rating": 4, "group_id": 1} for i in range(34)]}})
    writes = 0

    def intermittently_locked(path, payload):
        nonlocal writes
        writes += 1
        if payload.get("status") == "running" and writes % 8 == 0:
            raise PermissionError("synthetic intermittent progress lock")
        write_json(path, payload)

    monkeypatch.setenv("PHOTO_AI_SMART_CROP_MODE", "heuristic")
    monkeypatch.setattr(develop_worker, "write_json", intermittently_locked)
    result = develop_worker.run_develop_plan_job(input_path, run_dir, 3)
    plan = read_json(run_dir / "develop.json")
    assert result["eligible_count"] == len(plan["items"]) == 34
    assert all(Path(item["preview_path"]).is_file() for item in plan["items"])
    assert read_json(run_dir / "develop-progress.json")["status"] == "completed"


def test_develop_result_save_failure_still_fails_job(tmp_path, monkeypatch):
    input_path = tmp_path / "input.json"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _input(input_path)

    def fail_result(*args, **kwargs):
        raise PermissionError("develop.json denied")

    monkeypatch.setattr(develop, "create_develop_plan", fail_result)
    with pytest.raises(PermissionError, match="develop.json denied"):
        develop_worker.run_develop_plan_job(input_path, run_dir, 3)
    assert read_json(run_dir / "develop-progress.json")["status"] == "failed"
