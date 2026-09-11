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
