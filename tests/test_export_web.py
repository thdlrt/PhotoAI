from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from landscape_culler.util import read_json, write_json
from landscape_culler.web import create_app

RUN_ID = "20260901-120000-123456"


def _ready_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path, Path, dict[str, str]]:
    data_dir = tmp_path / "data"
    input_dir = tmp_path / "input"
    preview_dir = data_dir / "cache" / "previews"
    run_dir = data_dir / "runs" / RUN_ID
    input_dir.mkdir()
    preview_dir.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    raw = input_dir / "DSC0001.ARW"
    raw.write_bytes(b"camera raw")
    preview = preview_dir / "preview.jpg"
    Image.new("RGB", (640, 400), "#6989a0").save(preview, "JPEG")
    write_json(
        run_dir / "results.json",
        {
            "run_id": RUN_ID,
            "input_root": str(input_dir),
            "workflow_state": "scored",
            "results": [
                {
                    "path": str(raw),
                    "preview": str(preview),
                    "rating": 4,
                    "score": 0.8,
                    "group_id": 1,
                    "group_size": 1,
                    "keywords": ["AI|候选"],
                    "can_write_sidecar": True,
                }
            ],
        },
    )
    monkeypatch.setattr("landscape_culler.web._gpu", lambda: {"available": False})
    monkeypatch.setattr("landscape_culler.web._lightroom_state", lambda: "closed")
    monkeypatch.setattr(
        "landscape_culler.web.active_model_profile_readiness",
        lambda *_args: {
            "ready": True,
            "active_profile": "16gb",
            "label": "16GB 显存",
            "missing": [],
            "invalid": [],
            "component_ready": True,
            "message": "模型校验通过。",
        },
    )
    app = create_app(
        data_dir=data_dir,
        project_root=Path(__file__).parents[1],
        pending_root=input_dir,
    )
    client = TestClient(app)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    generated = client.post(
        f"/api/runs/{RUN_ID}/develop",
        json={"base_revision": 0},
        headers=headers,
    )
    assert generated.status_code == 200
    skipped = client.post(
        f"/api/runs/{RUN_ID}/develop/skip-crop",
        json={"base_revision": 0},
        headers=headers,
    )
    assert skipped.status_code == 200
    color = client.post(
        f"/api/runs/{RUN_ID}/develop/options",
        json={"base_revision": 1, "mode": "skip"},
        headers=headers,
    )
    assert color.status_code == 200
    assert color.json()["revision"] == 2
    return client, data_dir, raw, headers


def _prepare(
    client: TestClient,
    headers: dict[str, str],
    *,
    xmp: bool,
    jpeg: bool,
) -> dict:
    response = client.post(
        f"/api/runs/{RUN_ID}/exports/prepare",
        json={
            "base_revision": 0,
            "develop_revision": 2,
            "xmp": xmp,
            "jpeg": jpeg,
            "jpeg_settings": {
                "color_space": "sRGB",
                "size": "original",
                "quality": 90,
                "output_sharpening": "screen",
                "output_sharpening_amount": "standard",
                "collision": "suffix",
            },
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_prepare_freezes_workflow_revisions_recipe_and_raw_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, raw, headers = _ready_client(tmp_path, monkeypatch)
    public = _prepare(client, headers, xmp=True, jpeg=False)
    spec = read_json(data_dir / "exports" / f"{public['export_spec_id']}.json")

    assert spec["review_revision"] == 0
    assert spec["develop_revision"] == 2
    assert spec["develop_plan_id"].startswith("develop-")
    assert spec["workflow"] == {
        "crop": "skipped",
        "color_mode": "skip",
        "basic_color": "skipped",
        "creative_style": "skipped",
    }
    assert spec["output_dir"] == str((raw.parent / "成片").resolve())
    assert spec["items"][0]["rating"] == 4
    assert spec["items"][0]["recipe"]["confirmed"] is True
    assert spec["items"][0]["recipe"]["base"] == {}
    assert spec["items"][0]["source_fingerprint"]["quick_sha256"]
    assert spec["source_snapshot_sha256"]


def test_direct_xmp_export_reconciles_to_completed_api_state(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, raw, headers = _ready_client(tmp_path, monkeypatch)
    exiftool = tmp_path / "exiftool.exe"
    exiftool.write_bytes(b"test")
    monkeypatch.setenv("PHOTO_AI_EXIFTOOL", str(exiftool))
    public = _prepare(client, headers, xmp=True, jpeg=False)
    calls: list[dict] = []

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        calls.append({"kind": kind, "args": args, "context": context})
        return {"id": "job-direct", "kind": kind, "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    response = client.post(
        f"/api/exports/{public['export_spec_id']}/execute",
        json={},
        headers=headers,
    )
    assert response.status_code == 200
    assert calls[0]["kind"] == "xmp_commit"
    assert calls[0]["args"][0] == "write-xmp"

    spec = read_json(data_dir / "exports" / f"{public['export_spec_id']}.json")
    execution = spec["executions"][-1]
    manifest = Path(execution["results_path"]).parent / "xmp-commit-manifest-test.json"
    write_json(
        manifest,
        {
            "records": [
                {
                    "raw_path": str(raw),
                    "xmp_path": str(raw.with_suffix(".xmp")),
                    "status": "created",
                }
            ]
        },
    )
    client.app.state.jobs.jobs["job-direct"] = {
        "id": "job-direct",
        "status": "completed",
        "message": "任务已完成。",
    }
    reconciled = client.get(f"/api/exports/{public['export_spec_id']}")
    assert reconciled.status_code == 200
    payload = reconciled.json()
    assert payload["status"] == "completed"
    assert payload["results"]["xmp"]["succeeded"] == 1
    assert payload["results"]["jpeg"]["skipped"] == 1


def test_lightroom_partial_result_preserves_xmp_and_retries_only_failed_jpeg(
    tmp_path: Path, monkeypatch
) -> None:
    client, _data_dir, raw, headers = _ready_client(tmp_path, monkeypatch)
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(
        "landscape_culler.web._lightroom_status",
        lambda *_args: {
            "configured": True,
            "plugin": {"points_to_this_bridge": True},
            "lightroom": {"compatible": True, "executable": str(executable)},
        },
    )
    public = _prepare(client, headers, xmp=True, jpeg=True)
    calls: list[dict] = []

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        job_id = f"job-{len(calls) + 1}"
        calls.append({"id": job_id, "kind": kind, "args": args, "context": context})
        return {"id": job_id, "kind": kind, "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    batches: dict[str, dict] = {}
    monkeypatch.setattr(
        "landscape_culler.web.read_lightroom_batch_status",
        lambda _data, batch_id: batches[batch_id],
    )
    first = client.post(
        f"/api/exports/{public['export_spec_id']}/execute",
        json={},
        headers=headers,
    )
    assert first.status_code == 200
    assert calls[0]["kind"] == "lightroom_apply"
    assert calls[0]["args"][0] == "lightroom-export"
    first_attempt = read_json(Path(calls[0]["args"][2]))
    assert first_attempt["targets"] == {"xmp": True, "jpeg": True}
    batch_id = calls[0]["context"]["batch_id"]
    batches[batch_id] = {
        "status": "failed",
        "tasks": [
            {
                "photo_path": str(raw),
                "status": "failed",
                "result": {
                    "xmp_status": "done",
                    "xmp_path": str(raw.with_suffix(".xmp")),
                    "jpeg_status": "failed",
                    "message": "JPEG render failed",
                },
            }
        ],
    }
    client.app.state.jobs.jobs["job-1"] = {
        "id": "job-1",
        "status": "failed",
        "message": "Lightroom 导出失败。",
    }
    partial = client.get(f"/api/exports/{public['export_spec_id']}").json()
    assert partial["status"] == "partial"
    assert partial["results"]["xmp"]["succeeded"] == 1
    assert partial["results"]["jpeg"]["failed"] == 1

    retry = client.post(
        f"/api/exports/{public['export_spec_id']}/execute",
        json={"retry_failed_only": True},
        headers=headers,
    )
    assert retry.status_code == 200
    retry_attempt = read_json(Path(calls[1]["args"][2]))
    assert retry_attempt["targets"] == {"xmp": False, "jpeg": True}
    retry_batch_id = calls[1]["context"]["batch_id"]
    jpeg_path = raw.parent / "成片" / "DSC0001.jpg"
    batches[retry_batch_id] = {
        "status": "complete",
        "tasks": [
            {
                "photo_path": str(raw),
                "status": "done",
                "result": {
                    "xmp_status": "not_requested",
                    "jpeg_status": "done",
                    "jpeg_path": str(jpeg_path),
                },
            }
        ],
    }
    client.app.state.jobs.jobs["job-2"] = {
        "id": "job-2",
        "status": "completed",
        "message": "任务已完成。",
    }
    completed = client.get(f"/api/exports/{public['export_spec_id']}").json()
    assert completed["status"] == "completed"
    assert completed["results"]["xmp"]["succeeded"] == 1
    assert completed["results"]["jpeg"]["succeeded"] == 1
    assert completed["items"][0]["targets"]["xmp"]["attempts"] == 1
    assert completed["items"][0]["targets"]["jpeg"]["attempts"] == 2
