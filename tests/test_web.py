from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

import landscape_culler.lightroom_bridge as bridge
import landscape_culler.web as web_module
from landscape_culler.content_root import ContentRootLayout, initialize_content_root
from landscape_culler.creative_lut import CreativeLutEngine
from landscape_culler.lightroom_bridge import (
    LightroomTask,
    bridge_paths,
    create_lightroom_batch,
    read_lightroom_batch_status,
)
from landscape_culler.util import read_json, write_json
from landscape_culler.web import (
    JobManager,
    StyleGroupBody,
    StylePreviewBody,
    _apply_job_progress,
    _configured_style_catalog,
    _enrich_frozen_style_recipe,
    _ensure_export_style_targets,
    _export_style_catalog_by_id,
    _job_progress_plan,
    _worker_command,
    _worker_protocol_event,
    create_app,
)

RUN_ID = "20260831-120000-123456"


def test_worker_protocol_event_accepts_only_versioned_ndjson() -> None:
    assert _worker_protocol_event(
        '{"protocol":"PHOTO_AI_WORKER/1","event":"started","job_id":"job-1"}'
    )["event"] == "started"
    assert _worker_protocol_event('{"protocol":"OTHER/1","event":"started"}') is None
    assert _worker_protocol_event("ordinary CLI output") is None


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path, Path]:
    data_dir = tmp_path / "data"
    input_dir = tmp_path / "input"
    preview_dir = data_dir / "cache" / "previews"
    run_dir = data_dir / "runs" / RUN_ID
    input_dir.mkdir()
    preview_dir.mkdir(parents=True)
    run_dir.mkdir(parents=True)
    raw = input_dir / "DSC0001.ARW"
    raw.write_bytes(b"raw")
    preview = preview_dir / "preview.jpg"
    Image.new("RGB", (640, 400), "#6989a0").save(preview, "JPEG")
    write_json(
        run_dir / "results.json",
        {
            "run_id": RUN_ID,
            "input_root": str(input_dir),
            "retain_ratio": 0.3,
            "image_count": 1,
            "candidate_count": 1,
            "strong_count": 0,
            "results": [
                {
                    "path": str(raw),
                    "preview": str(preview),
                    "rating": 3,
                    "score": 0.5,
                    "group_id": 1,
                    "group_size": 1,
                    "keywords": [],
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
    return TestClient(app), data_dir, raw


def _append_result(data_dir: Path, raw: Path, group_id: int) -> None:
    raw.write_bytes(b"raw")
    path = data_dir / "runs" / RUN_ID / "results.json"
    payload = read_json(path)
    item = dict(payload["results"][0])
    item.update(path=str(raw), group_id=group_id, group_size=1, rating=0, score=0.0)
    payload["results"].append(item)
    payload["image_count"] = len(payload["results"])
    write_json(path, payload)


def test_minimal_page_and_bootstrap(tmp_path: Path, monkeypatch) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    page = client.get("/")
    assert page.status_code == 200
    assert "127.0.0.1" not in page.text
    assert 'data-view="cull"' in page.text
    assert 'data-view="toolbox"' in page.text
    assert 'data-view="settings"' in page.text
    assert 'data-view="projects"' not in page.text
    assert 'data-view="train"' not in page.text
    assert 'data-view="history"' not in page.text
    assert 'id="cull-form"' in page.text
    assert 'id="view-project"' in page.text
    assert 'id="project-group-form"' in page.text
    assert 'id="raw-jpeg-form"' in page.text
    assert 'id="delete-project-dialog"' in page.text
    assert 'id="review-back"' in page.text
    assert 'id="score-settings"' in page.text
    assert 'id="score-run"' in page.text
    assert 'id="selection-toolbar"' in page.text
    assert 'id="develop-open"' in page.text
    assert 'id="view-develop"' in page.text
    assert 'id="view-export"' in page.text
    assert 'id="export-action"' in page.text
    assert 'data-color-mode="auto"' in page.text
    assert 'data-color-mode="skip"' in page.text
    assert 'data-style-scope="global"' in page.text
    assert 'data-style-scope="group"' in page.text
    for stage in ("review", "crop", "base", "style", "export"):
        assert f'data-workflow-stage="{stage}"' in page.text
    assert page.text.count('id="job-progress-track"') == 1
    assert 'id="job-progress-fill"' in page.text
    assert 'id="job-nodes"' in page.text
    assert 'id="job-retry"' in page.text
    assert 'id="xmp-commit-open"' not in page.text
    assert 'id="develop-xmp"' not in page.text
    assert 'id="commit-dialog"' not in page.text
    assert 'id="style-progress"' not in page.text
    assert 'id="lightroom-config-form"' in page.text
    assert 'id="lightroom-refresh"' in page.text
    assert 'id="lightroom-auto-configure"' in page.text
    assert 'id="lightroom-config-status"' in page.text
    assert 'id="style-library-files"' in page.text
    assert 'id="style-library-import"' in page.text
    assert 'id="style-library-manage"' in page.text
    assert 'id="style-library-manager-dialog"' in page.text
    assert 'data-style-source-open="lightroom"' in page.text
    assert 'data-style-source-open="user"' in page.text
    assert 'id="style-library-source-toggle"' in page.text
    assert 'id="style-library-source-rescan"' in page.text
    assert 'id="model-resource-open"' in page.text
    assert 'id="model-setup-gate"' in page.text
    assert 'id="project-model-setup-gate"' in page.text
    assert 'id="view-resources"' in page.text
    assert 'id="model-profile-list"' in page.text
    assert 'id="model-resource-list"' in page.text
    assert 'id="settings-export"' in page.text
    assert 'id="settings-import-open"' in page.text
    assert 'id="settings-import-file"' in page.text
    assert 'id="style-enable-bw"' not in page.text
    assert 'id="style-enable-adaptive"' not in page.text
    assert 'id="style-include-adobe-copy"' not in page.text

    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "data-shift-index=" in script.text
    assert "data-new-group-index=" in script.text
    assert "function goWorkflowStage" in script.text
    assert "function renderExport" in script.text
    assert "function recommendStyles" in script.text
    assert "function submitProjectGroup" in script.text
    assert "/api/style-library/import" in script.text
    assert "data-style-library-toggle" in script.text
    assert "function requireModelSetupUi" in script.text
    assert "function retryFailedJob" in script.text
    assert "/retry`" in script.text
    assert "重试失败项" in script.text
    assert "function exportSettingsFile" in script.text
    assert "function importSettingsFile" in script.text
    assert "/api/settings-transfer/import" in script.text
    assert "data-style-recommend-group" in script.text
    assert "saveXmpAtStage" not in script.text
    assert "function openXmpDialog" not in script.text
    assert "X: 照片盘当前不可用" not in script.text
    assert "E 盘" not in script.text
    assert "E 盘" not in page.text

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    payload = bootstrap.json()
    assert payload["runs"][0]["run_id"] == RUN_ID
    assert payload["projects"][0]["version_count"] == 1
    assert payload["toolbox_transactions"] == []
    assert payload["xmp_cleanup_transactions"] == []
    assert "model" not in payload
    assert "audit" not in payload
    assert "library" not in payload["defaults"]
    assert payload["preferences"]["workflow_defaults"]["mode"] == "deep"
    assert payload["system"]["photos_online"] is True
    assert payload["system"]["content_root_configured"] is True


def test_bootstrap_storage_is_read_only_until_content_root_is_selected(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("landscape_culler.web._gpu", lambda: {"available": False})
    monkeypatch.setattr("landscape_culler.web._lightroom_state", lambda: "closed")
    data_dir = tmp_path / "bootstrap" / "state"
    project_root = tmp_path / "application"
    project_root.mkdir()
    app = create_app(
        data_dir=data_dir,
        project_root=project_root,
        bootstrap_storage=True,
    )
    with TestClient(app) as client:
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        payload = bootstrap.json()
        assert payload["system"]["content_root_configured"] is False

        blocked = client.post(
            "/api/model-resources/configure",
            headers={"X-Photo-AI-Token": payload["token"]},
            json={"profile_id": "8gb"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "content_root_required"
        assert client.get("/api/jobs").json() == []
        assert not list((project_root / ".runtime").rglob("*.safetensors"))


def test_settings_transfer_api_is_path_free_and_never_installs_resources(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    monkeypatch.setattr(web_module, "sync_style_library", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        web_module,
        "model_resources_status",
        lambda *_args: {"settings": {"active_profile": "16gb"}},
    )
    monkeypatch.setattr(
        web_module,
        "_lightroom_status",
        lambda *_args: {
            "lightroom": {"compatible": False},
            "heartbeat": {"state": "offline"},
        },
    )
    monkeypatch.setattr(
        client.app.state.jobs,
        "start",
        lambda *_args, **_kwargs: pytest.fail("settings import started a job"),
    )
    protected = {
        "lightroom": data_dir / "lightroom" / "settings.json",
        "model": data_dir / "models" / "weights.bin",
        "style": data_dir / "style-library" / "user-upload" / "one.xmp",
    }
    for path in protected.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"unchanged:{path.name}".encode())
    before = {key: path.read_bytes() for key, path in protected.items()}

    exported = client.get("/api/settings-transfer/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith(
        "application/vnd.photoai.settings+json"
    )
    assert "PhotoAI.photoai-settings" in exported.headers["content-disposition"]
    exported_payload = exported.json()
    assert exported_payload["model_profile_preference"] == "16gb"

    response = client.post(
        "/api/settings-transfer/import",
        headers=headers,
        json={
            "settings": {
                "schema_version": 1,
                "workflow_defaults": {
                    "retain_ratio": 0.4,
                    "mode": "fast",
                    "input_root": r"X:\camera",
                },
                "model_profile_preference": "8gb",
                "style_sources": {
                    "include_lightroom_presets": False,
                    "include_user_uploads": True,
                    "hidden_resource_ids": ["xmp-deadbeef"],
                },
                "export_defaults": {"xmp": False, "jpeg": True},
                "projects": [{"path": r"X:\photos"}],
                "models": [{"path": r"E:\models\weights.bin"}],
                "lightroom": {"executable_path": r"C:\Lightroom.exe"},
                "gpu_info": {"name": "RTX"},
                "token": "secret",
            }
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["model_download_started"] is False
    assert result["lightroom_configuration_changed"] is False
    assert result["settings"]["model_profile_preference"] == "8gb"
    assert set(result["ignored_fields"]) == {
        "gpu_info",
        "lightroom",
        "models",
        "projects",
        "token",
    }
    serialized = json.dumps(result["settings"], ensure_ascii=False)
    assert not any(value in serialized for value in ("X:\\", "E:\\", "C:\\"))
    assert "secret" not in serialized
    assert read_json(data_dir / "settings" / "preferences.json") == {
        "schema_version": 1,
        "ui": {"theme": "dark"},
        "workflow_defaults": {"retain_ratio": 0.4, "mode": "fast"},
        "model_profile_preference": "8gb",
        "export_defaults": {
            "xmp": False,
            "jpeg": True,
            "jpeg_settings": {
                "color_space": "sRGB",
                "size": "original",
                "quality": 90,
                "sharpening": "screen_standard",
                "collision": "suffix",
            },
        },
        "updated_utc": read_json(data_dir / "settings" / "preferences.json")[
            "updated_utc"
        ],
    }
    style_settings = read_json(data_dir / "style-library" / "settings.json")
    assert style_settings["include_lightroom_presets"] is False
    assert style_settings["hidden_resource_ids"] == ["xmp-deadbeef"]
    assert all(path.read_bytes() == before[key] for key, path in protected.items())

    current = client.get("/api/settings-transfer").json()
    assert current["workflow_defaults"] == {"retain_ratio": 0.4, "mode": "fast"}
    assert current["model_profile_preference"] == "8gb"
    assert client.get("/api/bootstrap").json()["preferences"] == current


def test_settings_transfer_import_requires_token_and_rolls_back_sync_failure(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    payload = {
        "settings": {
            "schema_version": 1,
            "style_sources": {"include_lightroom_presets": False},
        }
    }
    assert client.post("/api/settings-transfer/import", json=payload).status_code == 403
    token = client.get("/api/bootstrap").json()["token"]
    monkeypatch.setattr(
        web_module,
        "sync_style_library",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index failed")),
    )

    failed = client.post(
        "/api/settings-transfer/import",
        json=payload,
        headers={"X-Photo-AI-Token": token},
    )

    assert failed.status_code == 409
    restored = read_json(data_dir / "style-library" / "settings.json")
    assert restored["include_lightroom_presets"] is True
    preferences = read_json(data_dir / "settings" / "preferences.json")
    assert preferences["workflow_defaults"] == {"retain_ratio": 0.3, "mode": "deep"}


def test_bootstrap_without_default_photo_directory_has_no_false_offline_state(
    tmp_path: Path,
) -> None:
    app = create_app(
        data_dir=tmp_path / "data",
        project_root=Path(__file__).parents[1],
        pending_root=None,
    )

    payload = TestClient(app).get("/api/bootstrap").json()

    assert payload["defaults"]["pending"] == ""
    assert payload["system"]["photos_online"] is None


def test_managed_core_does_not_probe_legacy_ollama_port(monkeypatch) -> None:
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", "D:\\PhotoAI")
    monkeypatch.delenv("PHOTO_AI_OLLAMA_ENDPOINT", raising=False)
    monkeypatch.setattr(
        web_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("managed core probed legacy Ollama"),
    )

    web_module._unload_local_vlm()


def test_model_resource_api_starts_profile_job_and_deletes_exact_model(
    tmp_path: Path, monkeypatch
) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    payload = {
        "runtime_root": "E:\\app\\.runtime",
        "settings": {"active_profile": None},
        "profiles": [],
        "resources": [],
        "installed_count": 0,
        "installed_bytes": 0,
    }
    monkeypatch.setattr(web_module, "model_resources_status", lambda *_args: payload)
    started: dict[str, Any] = {}

    def fake_start(kind: str, args: list[str], context: dict[str, Any]) -> dict[str, Any]:
        started.update(kind=kind, args=args, context=context)
        return {"id": "model-job", "kind": kind, "status": "queued"}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    assert client.get("/api/model-resources").json() == payload
    response = client.post(
        "/api/model-resources/configure",
        json={"profile_id": "16gb"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["id"] == "model-job"
    assert started["kind"] == "model_download"
    assert "model-resources-configure" in started["args"]
    assert started["context"]["profile_id"] == "16gb"

    monkeypatch.setattr(client.app.state.jobs, "active", lambda: None)
    monkeypatch.setattr(web_module, "_unload_local_vlm", lambda: None)
    monkeypatch.setattr(
        web_module,
        "delete_model_resource",
        lambda resource_id, *_args: {**payload, "deleted": resource_id},
    )
    deleted = client.delete(
        "/api/model-resources/dinov2-base", headers=headers
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] == "dinov2-base"


def test_model_download_progress_has_runtime_models_verification_and_apply() -> None:
    plan = _job_progress_plan(
        "model_download", {"profile_id": "8gb", "managed_engine": True}
    )

    assert [item["key"] for item in plan[:7]] == [
        "launch",
        "preflight",
        "python",
        "venv",
        "dependencies",
        "worker",
        "ollama",
    ]
    assert [item["key"] for item in plan][-3:] == ["verify", "smoke", "activate"]
    assert len(plan) == 17
    assert sum(float(item["weight"]) for item in plan) == pytest.approx(100.0)


def test_offline_model_import_uses_local_package_and_dedicated_progress(
    tmp_path: Path, monkeypatch
) -> None:
    environment_before = os.environ.copy()
    layout = initialize_content_root(
        tmp_path / "content",
        install_dir=tmp_path / "program",
        apply_environment=False,
        persist_registry=False,
    )
    package = tmp_path / "PhotoAI-16GB.photoai-offline"
    package.write_bytes(b"offline")
    try:
        app = create_app(
            data_dir=layout.state,
            project_root=Path(__file__).parents[1],
            content_root=layout.root,
        )
        client = TestClient(app)
        started: dict[str, Any] = {}

        def fake_start(
            kind: str, args: list[str], context: dict[str, Any]
        ) -> dict[str, Any]:
            started.update(kind=kind, args=args, context=context)
            return {"id": "offline-job", "kind": kind, "status": "queued"}

        monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
        token = client.get("/api/bootstrap").json()["token"]
        response = client.post(
            "/api/model-resources/import-offline",
            json={"package_path": str(package)},
            headers={"X-Photo-AI-Token": token},
        )

        assert response.status_code == 200
        assert started["kind"] == "model_download"
        assert started["args"][0] == "ai-runtime-import"
        assert started["args"][-1] == str(package.resolve())
        assert started["context"]["offline_import"] is True
        plan = _job_progress_plan("model_download", started["context"])
        assert [item["key"] for item in plan] == [
            "launch",
            "preflight",
            "offline_import",
            "offline_dependencies",
            "verify",
            "smoke",
            "activate",
        ]
        assert sum(float(item["weight"]) for item in plan) == pytest.approx(100.0)
    finally:
        os.environ.clear()
        os.environ.update(environment_before)


def test_model_install_cancel_finishes_immediately_before_worker_spawn(
    tmp_path: Path, monkeypatch
) -> None:
    class IdleThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(web_module.threading, "Thread", IdleThread)
    manager = JobManager(tmp_path / "data", Path(__file__).parents[1])
    started = manager.start(
        "model_download",
        ["model-resources-configure", "--profile", "8gb"],
        {"title": "配置 8GB", "profile_id": "8gb", "managed_engine": False},
    )

    cancelled = manager.cancel(started["id"])

    assert cancelled["status"] == "cancelled"
    assert cancelled["finished_at"]
    assert "断点缓存会保留" in cancelled["message"]


def test_failed_job_retry_uses_private_recipe_and_fresh_lightroom_batch(
    tmp_path: Path, monkeypatch
) -> None:
    class IdleThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(web_module.threading, "Thread", IdleThread)
    manager = JobManager(tmp_path / "data", Path(__file__).parents[1])
    started = manager.start(
        "style_recommend",
        [
            "style-recommend",
            "--run-dir",
            str(tmp_path / "run"),
            "--batch-id",
            "batch-old",
        ],
        {
            "title": "生成风格推荐",
            "run_id": RUN_ID,
            "batch_id": "batch-old",
        },
    )
    with manager.lock:
        manager.jobs[started["id"]].update(status="failed", message="bridge failed")
        manager._save(manager.jobs[started["id"]])

    failed = manager.get(started["id"])
    assert failed["retryable"] is True
    assert failed["retry_mode"] == "job"
    assert "command" not in failed
    assert "retry_spec" not in failed

    retried = manager.retry(started["id"])

    assert retried["id"] != started["id"]
    assert retried["context"]["retry_of_job_id"] == started["id"]
    assert retried["context"]["batch_id"] != "batch-old"
    retry_argv = manager.jobs[retried["id"]]["retry_spec"]["argv"]
    batch_index = retry_argv.index("--batch-id")
    assert retry_argv[batch_index + 1] == retried["context"]["batch_id"]


def test_job_retry_rejects_non_failed_and_export_jobs(tmp_path: Path, monkeypatch) -> None:
    class IdleThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(web_module.threading, "Thread", IdleThread)
    manager = JobManager(tmp_path / "data", Path(__file__).parents[1])
    started = manager.start("group", ["group", "--input", "photos"], {"title": "分类"})
    with pytest.raises(RuntimeError, match="只有失败"):
        manager.retry(started["id"])
    with manager.lock:
        manager.jobs[started["id"]].update(
            status="failed", context={"export_spec_id": "export-one"}
        )
    with pytest.raises(RuntimeError, match="失败项清单"):
        manager.retry(started["id"])


def test_partial_style_job_can_retry_failed_groups(tmp_path: Path, monkeypatch) -> None:
    class IdleThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(web_module.threading, "Thread", IdleThread)
    manager = JobManager(tmp_path / "data", Path(__file__).parents[1])
    started = manager.start(
        "style_recommend",
        ["style-recommend", "--batch-id", "partial-old"],
        {"title": "全部风格", "batch_id": "partial-old"},
    )
    with manager.lock:
        manager.jobs[started["id"]].update(
            status="completed",
            result={"style_status": "partial", "failed_group_count": 2},
        )

    retried = manager.retry(started["id"])

    assert retried["status"] == "queued"
    assert retried["context"]["batch_id"] != "partial-old"


def test_retry_job_api_requires_token_and_returns_new_attempt(
    tmp_path: Path, monkeypatch
) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    previous = {
        "id": "failed-one",
        "kind": "group",
        "status": "failed",
        "title": "照片分类",
        "context": {},
    }
    retried = {
        "id": "retry-one",
        "kind": "group",
        "status": "queued",
        "title": "照片分类",
        "context": {"retry_of_job_id": "failed-one"},
    }
    monkeypatch.setattr(client.app.state.jobs, "get", lambda job_id: previous)
    monkeypatch.setattr(client.app.state.jobs, "retry", lambda job_id: retried)

    assert client.post("/api/jobs/failed-one/retry").status_code == 403
    token = client.get("/api/bootstrap").json()["token"]
    response = client.post(
        "/api/jobs/failed-one/retry",
        headers={"X-Photo-AI-Token": token},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "retry-one"


@pytest.mark.parametrize("kind", [
    "model_download", "xmp_cleanup_execute", "xmp_cleanup_rollback",
    "raw_jpeg_execute", "raw_jpeg_rollback", "xmp_commit", "rollback", "lightroom_apply",
])
def test_clean_content_root_can_build_non_ai_worker_environment(
    tmp_path: Path, monkeypatch, kind: str,
) -> None:
    layout = initialize_content_root(
        tmp_path / "content", apply_environment=False, persist_registry=False
    )
    manager = JobManager(layout.state, Path(__file__).parents[1], layout)
    monkeypatch.setattr(
        web_module,
        "clip_model_reference",
        lambda _root: pytest.fail("non-AI task must not resolve an installed CLIP model"),
    )
    monkeypatch.setenv("PHOTO_AI_CLIP_MODEL", "stale-model-from-another-root")

    environment = manager._job_environment(
        {"kind": kind, "context": {}}
    )

    assert "PHOTO_AI_CLIP_MODEL" not in environment
    assert environment["PHOTO_AI_CONTENT_ROOT"] == str(layout.root)
    assert "PHOTO_AI_LEGACY_RUNTIME_ROOT" not in environment


@pytest.mark.parametrize("kind", sorted(JobManager.AI_KINDS))
def test_ai_worker_environment_still_requires_models(tmp_path, monkeypatch, kind):
    layout = initialize_content_root(
        tmp_path / "content", apply_environment=False, persist_registry=False,
    )
    manager = JobManager(layout.state, Path(__file__).parents[1], layout)

    def missing(_root):
        raise RuntimeError("CLIP snapshot missing")

    monkeypatch.setattr(web_module, "clip_model_reference", missing)
    with pytest.raises(RuntimeError, match="CLIP snapshot missing"):
        manager._job_environment({"kind": kind, "context": {}})


def test_source_checkout_jobs_receive_explicit_legacy_model_runtime(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "checkout"
    project_root.mkdir()
    manager = JobManager(tmp_path / "data", project_root)

    environment = manager._job_environment({"kind": "group", "context": {}})

    assert environment["PHOTO_AI_LEGACY_RUNTIME_ROOT"] == str(
        (project_root / ".runtime").resolve()
    )
    assert "PHOTO_AI_CONTENT_ROOT" not in environment
    assert "PHOTO_AI_MODELS_DIR" not in environment


def test_model_install_cancel_closes_running_pre_spawn_race(
    tmp_path: Path, monkeypatch
) -> None:
    class IdleThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(web_module.threading, "Thread", IdleThread)
    manager = JobManager(tmp_path / "data", Path(__file__).parents[1])
    started = manager.start(
        "model_download",
        ["model-resources-configure", "--profile", "8gb"],
        {"title": "配置 8GB", "profile_id": "8gb", "managed_engine": False},
    )
    with manager.lock:
        manager.jobs[started["id"]]["status"] = "running"
        manager._save(manager.jobs[started["id"]])

    cancelled = manager.cancel(started["id"])

    assert cancelled["status"] == "cancelled"
    assert manager.active() is None


def test_frozen_application_reuses_its_own_executable_for_workers(
    monkeypatch,
) -> None:
    monkeypatch.setattr(web_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(web_module.sys, "executable", r"E:\PhotoAI\PhotoAI.exe")

    assert _worker_command(["group", "--input", r"X:\photos"]) == [
        r"E:\PhotoAI\PhotoAI.exe",
        "--worker",
        "group",
        "--input",
        r"X:\photos",
    ]


def test_managed_ai_jobs_use_versioned_worker_job_spec(
    tmp_path: Path, monkeypatch
) -> None:
    layout = ContentRootLayout.from_root(tmp_path / "content")
    layout.state.mkdir(parents=True, exist_ok=True)
    layout.logs.mkdir(parents=True, exist_ok=True)
    engine = layout.runtimes / "engines" / "engine-test"
    python = engine / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"managed python")
    monkeypatch.setattr(web_module, "active_engine", lambda _layout: engine)
    manager = JobManager(layout.state, Path(__file__).parents[1], layout)

    command = manager._job_command("score", ["score", "--run", "run-1"], "job-1")

    assert command[:4] == [
        str(python),
        "-m",
        "landscape_culler.core_worker",
        "--job-spec",
    ]
    spec = read_json(layout.state / "web" / "jobs" / "job-1.spec.json")
    assert spec == {
        "protocol": "PHOTO_AI_WORKER/1",
        "job_id": "job-1",
        "command": "cli",
        "argv": ["score", "--run", "run-1"],
        "result_path": str(
            layout.state / "web" / "jobs" / "job-1.worker-result.json"
        ),
    }

    develop_command = manager._job_command(
        "develop",
        [
            "develop-plan",
            "--input",
            str(layout.state / "web" / "jobs" / "develop-input.json"),
            "--run-dir",
            str(layout.projects / "run-1"),
            "--review-revision",
            "2",
        ],
        "develop-job-1",
    )
    assert develop_command[:4] == [
        str(python),
        "-m",
        "landscape_culler.core_worker",
        "--job-spec",
    ]
    develop_spec = read_json(
        layout.state / "web" / "jobs" / "develop-job-1.spec.json"
    )
    assert develop_spec["protocol"] == "PHOTO_AI_WORKER/1"
    assert develop_spec["command"] == "cli"
    assert develop_spec["argv"][0] == "develop-plan"
    assert develop_spec["result_path"].endswith(
        "develop-job-1.worker-result.json"
    )


def test_incomplete_model_profile_blocks_ai_workflow_and_returns_setup_code(
    tmp_path: Path, monkeypatch
) -> None:
    client, _data_dir, raw = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        web_module,
        "active_model_profile_readiness",
        lambda *_args: {
            "ready": False,
            "active_profile": "16gb",
            "label": "16GB 显存",
            "missing": ["dinov2-base"],
            "invalid": [],
            "component_ready": True,
            "message": "16GB 显存模型不完整：缺少 1 个，需修复 0 个。",
        },
    )
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    project = client.post(
        "/api/projects",
        json={"input_path": str(raw.parent)},
        headers=headers,
    )
    develop = client.post(
        f"/api/runs/{RUN_ID}/develop",
        json={"base_revision": 0},
        headers=headers,
    )

    assert project.status_code == develop.status_code == 409
    assert project.json()["detail"]["code"] == "model_profile_incomplete"
    assert "缺少 1 个" in project.json()["detail"]["message"]

def test_raw_jpeg_preview_is_scoped_and_execute_starts_safe_job(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    payload = {
        "layout": "mixed",
        "direction": "jpeg",
        "mixed_path": str(raw.parent),
        "raw_extensions": ["arw"],
        "jpeg_extensions": ["jpg", "jpeg"],
        "recursive": False,
    }

    assert client.post("/api/tools/raw-jpeg/preview", json=payload).status_code == 403
    preview = client.post("/api/tools/raw-jpeg/preview", json=payload, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["candidate_count"] == 1
    assert Path(preview.json()["candidates"][0]["path"]) == raw
    assert (
        data_dir
        / "toolbox"
        / "raw-jpeg"
        / "plans"
        / f"{preview.json()['plan_id']}.json"
    ).is_file()

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_response = client.post(
        "/api/tools/raw-jpeg/preview",
        json={**payload, "mixed_path": str(outside)},
        headers=headers,
    )
    assert outside_response.status_code == 422

    monkeypatch.setattr("landscape_culler.web._lightroom_state", lambda: "running")
    blocked = client.post(
        "/api/tools/raw-jpeg/execute",
        json={"plan_id": preview.json()["plan_id"]},
        headers=headers,
    )
    assert blocked.status_code == 409
    monkeypatch.setattr("landscape_culler.web._lightroom_state", lambda: "closed")

    captured: dict = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {"id": "toolbox-test", "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    execute = client.post(
        "/api/tools/raw-jpeg/execute",
        json={"plan_id": preview.json()["plan_id"]},
        headers=headers,
    )
    assert execute.status_code == 200
    assert captured["kind"] == "raw_jpeg_execute"
    assert captured["args"][0] == "raw-jpeg-execute"
    assert Path(captured["context"]["result_path"]).is_relative_to(data_dir)


def test_xmp_cleanup_four_api_contract_is_scoped_and_uses_safe_jobs(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, raw = _client(tmp_path, monkeypatch)
    xmp = raw.with_suffix(".xmp")
    xmp.write_text("existing user metadata", encoding="utf-8")
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    payload = {"root_path": str(raw.parent), "recursive": False}

    assert client.post("/api/tools/xmp-cleanup/preview", json=payload).status_code == 403
    preview = client.post(
        "/api/tools/xmp-cleanup/preview", json=payload, headers=headers
    )
    assert preview.status_code == 200
    assert preview.json()["xmp_count"] == 1
    assert preview.json()["complete"] is True
    assert preview.json()["operation"] == "delete"
    assert Path(preview.json()["candidates"][0]["path"]) == xmp
    plan_id = preview.json()["plan_id"]
    assert (
        data_dir / "toolbox" / "xmp-cleanup" / "plans" / f"{plan_id}.json"
    ).is_file()
    assert client.get("/api/tools/xmp-cleanup/transactions").json() == []

    outside = tmp_path / "outside-xmp"
    outside.mkdir()
    outside_response = client.post(
        "/api/tools/xmp-cleanup/preview",
        json={"root_path": str(outside), "recursive": False},
        headers=headers,
    )
    assert outside_response.status_code == 422

    calls: list[tuple[str, list[str], dict]] = []

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        calls.append((kind, args, context))
        return {"id": f"xmp-{len(calls)}", "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    monkeypatch.setattr("landscape_culler.web._lightroom_state", lambda: "running")
    execute = client.post(
        "/api/tools/xmp-cleanup/execute",
        json={"plan_id": plan_id},
        headers=headers,
    )
    assert execute.status_code == 200
    assert calls[-1][0] == "xmp_cleanup_execute"
    assert calls[-1][1][0] == "xmp-cleanup-execute"
    assert calls[-1][2]["title"] == "永久删除 XMP · 1 个文件"
    assert Path(calls[-1][2]["result_path"]).is_relative_to(data_dir)

    manifest = data_dir / "toolbox" / "xmp-cleanup" / "transactions" / f"{plan_id}.json"
    monkeypatch.setattr(
        web_module,
        "load_xmp_cleanup_transaction",
        lambda *_args, **_kwargs: (
            {
                "transaction_id": plan_id,
                "root_path": str(raw.parent),
                "remaining": 1,
                "conflict": 0,
                "rollbackable": True,
                "needs_attention": False,
            },
            manifest,
        ),
    )
    monkeypatch.setattr("landscape_culler.web._lightroom_state", lambda: "closed")
    rollback = client.post(
        "/api/tools/xmp-cleanup/rollback",
        json={"transaction_id": plan_id},
        headers=headers,
    )
    assert rollback.status_code == 200
    assert calls[-1][0] == "xmp_cleanup_rollback"
    assert calls[-1][1] == ["xmp-cleanup-rollback", "--manifest", str(manifest)]


def test_mutation_token_and_review_revision(tmp_path: Path, monkeypatch) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    assert (
        client.patch(
            f"/api/runs/{RUN_ID}/items/0", json={"rating": 5, "base_revision": 0}
        ).status_code
        == 403
    )
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    updated = client.patch(
        f"/api/runs/{RUN_ID}/items/0",
        json={"rating": 5, "base_revision": 0},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["review_revision"] == 1
    assert (
        client.get(f"/api/runs/{RUN_ID}").json()["results"][0]["effective_rating"] == 5
    )
    stale = client.patch(
        f"/api/runs/{RUN_ID}/items/0",
        json={"rating": 4, "base_revision": 0},
        headers=headers,
    )
    assert stale.status_code == 409
    assert (
        read_json(data_dir / "runs" / RUN_ID / "results.json")["results"][0]["rating"]
        == 3
    )


def test_preview_is_confined_to_cache(tmp_path: Path, monkeypatch) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    assert client.get(f"/api/runs/{RUN_ID}/preview/0").status_code == 200
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not allowed")
    results_path = data_dir / "runs" / RUN_ID / "results.json"
    payload = read_json(results_path)
    payload["results"][0]["preview"] = str(outside)
    write_json(results_path, payload)
    assert client.get(f"/api/runs/{RUN_ID}/preview/0").status_code == 404


def test_develop_plan_can_be_generated_edited_confirmed_and_invalidated(
    tmp_path: Path, monkeypatch
) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    generated = client.post(
        f"/api/runs/{RUN_ID}/develop",
        json={"base_revision": 0},
        headers=headers,
    )
    assert generated.status_code == 200
    plan = generated.json()
    assert plan["eligible_count"] == 1
    assert plan["confirmed_count"] == 0
    assert plan["plan_id"].startswith("develop-")
    assert plan["crop_skipped"] is False
    assert plan["color_enabled"] is True
    progress = client.get(f"/api/runs/{RUN_ID}/develop/progress")
    assert progress.status_code == 200
    assert progress.json()["status"] == "completed"
    assert progress.json()["overall_percent"] == 100.0
    assert progress.json()["completed"] == 1
    assert progress.json()["total"] == 1
    assert all(node["status"] == "completed" for node in progress.json()["nodes"])
    assert client.get(f"/api/runs/{RUN_ID}/develop/preview/0").status_code == 200

    edited = client.patch(
        f"/api/runs/{RUN_ID}/develop/0",
        json={
            "base_revision": 0,
            "crop_id": "tight",
            "style_id": "lightroom",
            "style_strength": 70,
            "confirmed": True,
        },
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["revision"] == 1
    assert edited.json()["confirmed_count"] == 1
    stale_edit = client.patch(
        f"/api/runs/{RUN_ID}/develop/0",
        json={"base_revision": 0, "style_strength": 30},
        headers=headers,
    )
    assert stale_edit.status_code == 409

    confirmed = client.post(
        f"/api/runs/{RUN_ID}/develop/confirm-all",
        json={"base_revision": 1},
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["revision"] == 2

    changed = client.patch(
        f"/api/runs/{RUN_ID}/items/0",
        json={"rating": 4, "base_revision": 0},
        headers=headers,
    )
    assert changed.status_code == 200
    assert client.get(f"/api/runs/{RUN_ID}/develop").json()["stale"] is True


def test_develop_generation_progress_is_readable_while_request_is_running(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    started = threading.Event()
    release = threading.Event()
    response_holder: dict = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        assert kind == "develop"
        assert args[0] == "develop-plan"
        assert context["run_id"] == RUN_ID
        return {"id": "develop-progress-test", "status": "queued"}

    def slow_wait(job_id: str) -> dict:
        assert job_id == "develop-progress-test"
        write_json(
            data_dir / "runs" / RUN_ID / "develop-progress.json",
            {
                "status": "running",
                "phase": "rank",
                "stage_label": "评估构图候选",
                "current": 0,
                "completed": 0,
                "total": 1,
                "filename": "DSC0001.ARW",
                "overall_percent": 37.0,
                "nodes": [
                    {"key": "rank", "label": "评估构图候选", "status": "running"}
                ],
            },
        )
        started.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test did not release develop generation")
        write_json(
            data_dir / "runs" / RUN_ID / "develop.json",
            {
                "schema_version": 5,
                "plan_id": "develop-progress-test",
                "run_id": RUN_ID,
                "source_review_revision": 0,
                "revision": 0,
                "crop_skipped": False,
                "color_enabled": True,
                "crop": {"status": "pending"},
                "basic_color": {"status": "pending"},
                "creative_style": {"status": "pending", "scope": "global", "groups": {}},
                "color_mode": "pending",
                "items": [],
            },
        )
        write_json(
            data_dir / "runs" / RUN_ID / "develop-progress.json",
            {
                "status": "completed",
                "stage_label": "智能构图完成",
                "overall_percent": 100.0,
                "current": 1,
                "completed": 1,
                "total": 1,
                "nodes": [],
            },
        )
        return {"id": job_id, "status": "completed"}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    monkeypatch.setattr(client.app.state.jobs, "wait", slow_wait)

    def generate() -> None:
        response_holder["response"] = client.post(
            f"/api/runs/{RUN_ID}/develop",
            json={"base_revision": 0},
            headers=headers,
        )

    worker = threading.Thread(target=generate, daemon=True)
    worker.start()
    try:
        assert started.wait(timeout=5), (
            "develop request did not reach the observable running state"
        )
        progress = client.get(f"/api/runs/{RUN_ID}/develop/progress")
        assert progress.status_code == 200
        assert progress.json()["status"] == "running"
        assert progress.json()["overall_percent"] == 37.0
        assert progress.json()["stage_label"] == "评估构图候选"
        assert progress.json()["filename"] == "DSC0001.ARW"
    finally:
        release.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert response_holder["response"].status_code == 200
    completed = client.get(f"/api/runs/{RUN_ID}/develop/progress").json()
    assert completed["status"] == "completed"
    assert completed["overall_percent"] == 100.0


def test_develop_worker_cancellation_is_reported_and_input_snapshot_is_removed(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    captured: dict[str, Any] = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        assert Path(args[2]).is_file()
        return {"id": "develop-cancel-test", "status": "queued"}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    monkeypatch.setattr(
        client.app.state.jobs,
        "wait",
        lambda _job_id: {
            "id": "develop-cancel-test",
            "status": "cancelled",
            "message": "cancelled",
        },
    )

    response = client.post(
        f"/api/runs/{RUN_ID}/develop",
        json={"base_revision": 0},
        headers=headers,
    )

    assert response.status_code == 409
    assert "已取消" in response.json()["detail"]
    assert captured["kind"] == "develop"
    progress = read_json(data_dir / "runs" / RUN_ID / "develop-progress.json")
    assert progress["status"] == "cancelled"
    assert not list((data_dir / "web" / "jobs").glob("develop-input-*.json"))


def test_develop_skip_crop_options_and_plan_identity_are_persisted(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    first = client.post(
        f"/api/runs/{RUN_ID}/develop",
        json={"base_revision": 0},
        headers=headers,
    )
    second = client.post(
        f"/api/runs/{RUN_ID}/develop",
        json={"base_revision": 0},
        headers=headers,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["plan_id"] != second.json()["plan_id"]

    skipped = client.post(
        f"/api/runs/{RUN_ID}/develop/skip-crop",
        json={"base_revision": 0},
        headers=headers,
    )
    assert skipped.status_code == 200
    skipped_plan = skipped.json()
    assert skipped_plan["revision"] == 1
    assert skipped_plan["crop_skipped"] is True
    assert skipped_plan["confirmed_count"] == skipped_plan["eligible_count"] == 1
    assert skipped_plan["items"][0]["crop_id"] == "original"
    assert skipped_plan["items"][0]["confirmed"] is True

    options = client.post(
        f"/api/runs/{RUN_ID}/develop/options",
        json={"base_revision": 1, "color_enabled": False},
        headers=headers,
    )
    assert options.status_code == 200
    assert options.json()["revision"] == 2
    assert options.json()["color_enabled"] is False

    edited = client.patch(
        f"/api/runs/{RUN_ID}/develop/0",
        json={"base_revision": 2, "crop_id": "balanced"},
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["revision"] == 3
    assert edited.json()["crop_skipped"] is False
    persisted = read_json(data_dir / "runs" / RUN_ID / "develop.json")
    assert persisted["plan_id"] == second.json()["plan_id"]
    assert persisted["crop_skipped"] is False
    assert persisted["color_enabled"] is False

    stale = client.post(
        f"/api/runs/{RUN_ID}/develop/options",
        json={"base_revision": 2, "color_enabled": True},
        headers=headers,
    )
    assert stale.status_code == 409


def _prepare_style_plan(
    client: TestClient,
    *,
    token: str,
) -> tuple[dict[str, str], int]:
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
    return headers, int(skipped.json()["revision"])


def test_configured_style_catalog_never_reenables_unregistered_presets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    entries = [
        {
            "preset_id": "valid",
            "compatibility": "compatible",
            "registration_status": "installed",
            "ai_eligible": True,
        },
        {
            "preset_id": "valid-bw",
            "compatibility": "compatible",
            "registration_status": "registered",
            "ai_eligible": True,
            "black_and_white": True,
        },
        {
            "preset_id": "unsupported-bw",
            "compatibility": "compatible",
            "registration_status": "unsupported",
            "ai_eligible": True,
            "black_and_white": True,
        },
        {
            "preset_id": "awaiting-adaptive",
            "compatibility": "compatible",
            "registration_status": "awaiting_registration",
            "ai_eligible": True,
            "adaptive": True,
        },
        {
            "preset_id": "duplicate",
            "compatibility": "compatible",
            "registration_status": "installed",
            "ai_eligible": True,
            "duplicate_of": "valid",
        },
        {
            "preset_id": "not-ai-eligible",
            "compatibility": "compatible",
            "registration_status": "installed",
            "ai_eligible": False,
        },
    ]
    for entry in entries:
        entry["source_kind"] = "adobe-installed"
    monkeypatch.setattr(
        "landscape_culler.web.load_style_index",
        lambda _data_dir: {"entries": entries, "default_pool": ["stale"]},
    )
    write_json(
        tmp_path / "style-library" / "settings.json",
        {"include_black_white": True, "include_adaptive": True},
    )

    catalog = _configured_style_catalog(tmp_path)

    assert catalog["default_pool"] == ["valid"]


def test_frozen_style_uses_runtime_uuid_and_catalog_scope_not_source_uuid() -> None:
    recipe = {
        "creative_style": {
            "status": "confirmed",
            "preset_id": "user-preset",
            "preset_hash": "exact-hash",
        }
    }
    entry = {
        "preset_id": "user-preset",
        "file_hash": "exact-hash",
        "source_kind": "user-installed",
        "registration_status": "installed",
        "uuid": "SOURCE-UUID",
        "runtime_preset_uuid": "RUNTIME-UUID",
        "preset_scope": "catalog",
    }

    frozen = _enrich_frozen_style_recipe(recipe, {"user-preset": entry})

    assert frozen["creative_style"]["preset_uuid"] == "RUNTIME-UUID"
    assert frozen["creative_style"]["preset_scope"] == "catalog"

    with pytest.raises(HTTPException, match="运行时 UUID"):
        _enrich_frozen_style_recipe(
            recipe,
            {"user-preset": {**entry, "runtime_preset_uuid": None}},
        )


def test_style_request_models_accept_lut_strength_zero_to_two_hundred() -> None:
    preview_zero = StylePreviewBody(
        base_revision=3,
        group_id=1,
        lut_id="cube-lut-1",
        lut_hash="a" * 64,
        strength=0,
    )
    preview = StylePreviewBody(
        base_revision=3,
        group_id=1,
        lut_id="cube-lut-1",
        lut_hash="a" * 64,
        strength=150,
    )
    selection = StyleGroupBody(
        base_revision=3,
        lut_id="cube-lut-1",
        lut_hash="a" * 64,
        amount=200,
    )
    selection_zero = StyleGroupBody(
        base_revision=3,
        lut_id="cube-lut-1",
        lut_hash="a" * 64,
        amount=0,
    )

    assert preview_zero.amount == 0
    assert preview.amount == 150
    assert selection.amount == 200
    assert selection_zero.amount == 0
    with pytest.raises(ValidationError, match="less than or equal to 200"):
        StylePreviewBody(
            base_revision=3,
            group_id=1,
            lut_id="cube-lut-1",
            lut_hash="a" * 64,
            strength=201,
        )
    with pytest.raises(ValidationError, match="成对提供"):
        StyleGroupBody(base_revision=3, lut_id="cube-lut-1")


def test_style_library_exposes_imported_lut_as_jpeg_only_resource(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "look.cube"
    source.write_text(
        "LUT_1D_SIZE 2\n0.0 0.0 0.0\n1.0 0.8 0.6\n",
        encoding="utf-8",
    )
    engine = CreativeLutEngine(tmp_path / "runtime", enforce_e_drive=False)
    descriptor = engine.import_lut(source, source_label="Local LUT")
    monkeypatch.setattr(
        CreativeLutEngine,
        "for_project",
        classmethod(lambda _cls, _project_root: engine),
    )

    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    response = client.get("/api/style-library")
    assert response.status_code == 200
    payload = response.json()

    assert payload["stats"]["luts"] == 1
    assert payload["luts"][0]["lut_id"] == descriptor.lut_id
    assert payload["luts"][0]["lut_hash"] == descriptor.lut_hash
    assert payload["luts"][0]["supports_amount"] is True
    assert payload["luts"][0]["strength_max"] == 200
    assert payload["luts"][0]["xmp_compatible"] is False
    assert payload["luts"][0]["ai_enabled"] is False
    assert payload["luts"][0]["manual_preview_enabled"] is True
    assert payload["luts"][0]["render_targets"] == ["jpeg"]
    assert any(item.get("lut_id") == descriptor.lut_id for item in payload["presets"])


def test_style_library_batch_import_rebuilds_incrementally(
    tmp_path: Path, monkeypatch
) -> None:
    engine = CreativeLutEngine(tmp_path / "lut-runtime", enforce_e_drive=False)
    monkeypatch.setattr(
        CreativeLutEngine,
        "for_project",
        classmethod(lambda _cls, _project_root: engine),
    )
    real_sync = web_module.sync_style_library
    monkeypatch.setattr(
        web_module,
        "sync_style_library",
        lambda data_dir: real_sync(data_dir, adobe_roots=[]),
    )
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    xmp = b'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:PresetType="Normal" crs:UUID="USER-UPLOAD-001"
   crs:SupportsAmount="True" crs:Exposure2012="+0.10"
   crs:Version="15.3" crs:ProcessVersion="15.3">
   <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">Uploaded Warm</rdf:li></rdf:Alt></crs:Name>
   <crs:Group><rdf:Alt><rdf:li xml:lang="x-default">User</rdf:li></rdf:Alt></crs:Group>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>'''
    cube = b"LUT_1D_SIZE 2\n0.0 0.0 0.0\n1.0 0.8 0.6\n"
    payload = {
        "files": [
            {
                "name": "warm.xmp",
                "content_base64": base64.b64encode(xmp).decode("ascii"),
            },
            {
                "name": "warm.cube",
                "content_base64": base64.b64encode(cube).decode("ascii"),
            },
        ]
    }

    assert client.post("/api/style-library/import", json=payload).status_code == 403
    imported = client.post(
        "/api/style-library/import", json=payload, headers=headers
    )
    assert imported.status_code == 200
    assert imported.json()["imported_count"] == 2
    assert imported.json()["failed_count"] == 0
    assert imported.json()["index_rebuilt"] is True
    assert imported.json()["registration_pending"] is True
    assert len(list((data_dir / "style-library" / "user-upload" / "presets").glob("*.xmp"))) == 1
    assert (data_dir / "style-library" / "index.json").is_file()
    assert imported.json()["library"]["stats"]["luts"] == 1

    repeated = client.post(
        "/api/style-library/import", json=payload, headers=headers
    )
    assert repeated.status_code == 200
    assert repeated.json()["imported_count"] == 0
    assert repeated.json()["reused_count"] == 2
    assert repeated.json()["index_rebuilt"] is False

    indexed = read_json(data_dir / "style-library" / "index.json")
    uploaded = next(
        item
        for item in indexed["entries"]
        if item.get("source_id") == "user-upload"
    )
    write_json(
        data_dir / "style-library" / "managed-preset-registration.json",
        {
            "registry_hash": indexed["managed_registry"]["registry_hash"],
            "entries": [
                {
                    "preset_id": uploaded["preset_id"],
                    "file_hash": uploaded["file_hash"],
                    "plugin_name": uploaded["plugin_name"],
                    "plugin_uuid": "LIGHTROOM-RUNTIME-UUID",
                    "status": "registered",
                }
            ],
        },
    )
    refreshed = _configured_style_catalog(data_dir)
    assert uploaded["preset_id"] in refreshed["default_pool"]

    library = client.get("/api/style-library").json()
    user_xmp = next(
        item
        for item in library["presets"]
        if item.get("source_id") == "user-upload" and item.get("preset_id")
    )
    user_lut = next(item for item in library["presets"] if item.get("lut_id"))
    assert user_xmp["can_delete"] is True
    assert user_lut["can_delete"] is True

    hidden = client.patch(
        f"/api/style-library/items/{user_xmp['resource_id']}",
        json={"hidden": True},
        headers=headers,
    )
    assert hidden.status_code == 200
    hidden_item = next(
        item
        for item in hidden.json()["library"]["presets"]
        if item["resource_id"] == user_xmp["resource_id"]
    )
    assert hidden_item["user_hidden"] is True
    assert hidden_item["ai_enabled"] is False

    restored = client.patch(
        f"/api/style-library/items/{user_xmp['resource_id']}",
        json={"hidden": False},
        headers=headers,
    )
    assert restored.status_code == 200
    restored_item = next(
        item
        for item in restored.json()["library"]["presets"]
        if item["resource_id"] == user_xmp["resource_id"]
    )
    assert restored_item["user_hidden"] is False

    deleted_xmp = client.delete(
        f"/api/style-library/items/{user_xmp['resource_id']}", headers=headers
    )
    assert deleted_xmp.status_code == 200
    assert not list(
        (data_dir / "style-library" / "user-upload" / "presets").glob("*.xmp")
    )
    assert list(
        (data_dir / "style-library-archive" / "user-upload").glob("*/removed.json")
    )

    deleted_lut = client.delete(
        f"/api/style-library/items/{user_lut['resource_id']}", headers=headers
    )
    assert deleted_lut.status_code == 200
    assert deleted_lut.json()["library"]["stats"]["luts"] == 0
    assert list(
        (data_dir / "style-library-archive" / "user-lut").glob("*/removed.json")
    )


def test_style_library_refuses_to_delete_lightroom_owned_resource(
    tmp_path: Path, monkeypatch
) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    monkeypatch.setattr(
        web_module,
        "_sync_registered_styles_if_newer",
        lambda _data_dir: False,
    )
    monkeypatch.setattr(
        web_module,
        "load_style_index",
        lambda _data_dir: {
            "entries": [
                {
                    "preset_id": "uuid:lightroom-owned",
                    "file_hash": "a" * 64,
                    "locator": "adobe-installed:E:/Adobe/owned.xmp",
                    "name": "Lightroom Owned",
                    "source": "Adobe Lightroom installed",
                    "source_kind": "adobe-installed",
                    "registration_status": "installed",
                    "compatibility": "compatible",
                    "ai_eligible": True,
                    "hidden": False,
                }
            ],
            "summary": {"total": 1},
        },
    )
    library = client.get("/api/style-library").json()
    item = library["presets"][0]
    assert item["source_group"] == "lightroom"
    assert item["can_delete"] is False

    monkeypatch.setattr(web_module, "sync_style_library", lambda _data_dir: {})
    hidden = client.patch(
        f"/api/style-library/items/{item['resource_id']}",
        json={"hidden": True},
        headers={"X-Photo-AI-Token": token},
    )
    assert hidden.status_code == 200
    assert item["resource_id"] in read_json(
        _data_dir / "style-library" / "settings.json"
    )["hidden_resource_ids"]

    restored = client.patch(
        f"/api/style-library/items/{item['resource_id']}",
        json={"hidden": False},
        headers={"X-Photo-AI-Token": token},
    )
    assert restored.status_code == 200
    assert item["resource_id"] not in read_json(
        _data_dir / "style-library" / "settings.json"
    )["hidden_resource_ids"]

    refused = client.delete(
        f"/api/style-library/items/{item['resource_id']}",
        headers={"X-Photo-AI-Token": token},
    )
    assert refused.status_code == 409
    assert "不能删除" in refused.json()["detail"]


def test_export_freeze_keeps_profile_xmp_and_rejects_cube_xmp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "look.cube"
    source.write_text(
        "LUT_1D_SIZE 2\n0.0 0.0 0.0\n1.0 0.8 0.6\n",
        encoding="utf-8",
    )
    engine = CreativeLutEngine(tmp_path / "runtime", enforce_e_drive=False)
    descriptor = engine.import_lut(source)
    lut_recipe = {
        "creative_style": {
            "status": "confirmed",
            "look_kind": "rendered_lut",
            "lut_id": descriptor.lut_id,
            "lut_hash": descriptor.lut_hash,
            "strength": 150,
            "xmp_compatible": True,
        }
    }

    frozen_lut = _enrich_frozen_style_recipe(lut_recipe, {}, engine)

    assert frozen_lut["creative_style"]["xmp_compatible"] is False
    assert frozen_lut["creative_style"]["amount"] == 150
    lut_plan = {
        "creative_style": {
            "groups": {"1": frozen_lut["creative_style"]},
        }
    }
    with pytest.raises(HTTPException, match="不能写入普通 Lightroom XMP"):
        _ensure_export_style_targets(lut_plan, xmp=True)
    _ensure_export_style_targets(lut_plan, xmp=False)

    look_uuid = "9901408EBF8D496E99EFC526805F2F7C"
    look_descriptor = {
        "SchemaVersion": 1,
        "UUID": look_uuid,
        "Name": "Modern 10",
        "Group": "Modern",
        "Cluster": "Adobe",
        "SupportsAmount": True,
        "Parameters": {"Saturation": -5},
        "TableDigests": {},
        "ComplexParameterDigests": {},
    }
    look_descriptor["Hash"] = hashlib.sha256(
        json.dumps(
            look_descriptor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    profile = {
        "preset_id": "profile-1",
        "file_hash": "profile-hash",
        "look_kind": "lightroom_profile",
        "profile_name": "Modern 10",
        "uuid": look_uuid,
        "look_descriptor": look_descriptor,
        "look_descriptor_hash": look_descriptor["Hash"],
        "supports_amount": True,
        "registration_status": "local_copy",
    }
    published_hash = "b" * 64
    monkeypatch.setattr(
        web_module,
        "write_lightroom_look_descriptor",
        lambda data_dir, descriptor, *, look_uuid: {
            "look_descriptor_path": str(
                Path("E:/runtime/lightroom-bridge/presets/looks")
                / f"{published_hash}.look"
            ),
            "look_descriptor_hash": published_hash,
            "look_uuid": look_uuid,
        },
    )
    catalog = _export_style_catalog_by_id({"entries": [profile]})
    frozen_profile = _enrich_frozen_style_recipe(
        {
            "creative_style": {
                "status": "confirmed",
                "preset_id": "profile-1",
                "preset_hash": "profile-hash",
                "amount": 135,
            }
        },
        catalog,
        data_dir=tmp_path / "data",
    )
    assert "profile-1" in catalog
    assert frozen_profile["creative_style"]["xmp_compatible"] is True
    assert frozen_profile["creative_style"]["profile_name"] == "Modern 10"
    assert frozen_profile["creative_style"]["preset_uuid"] is None
    assert frozen_profile["creative_style"]["look_source_hash"] == look_descriptor["Hash"]
    assert frozen_profile["creative_style"]["look_descriptor_hash"] == published_hash
    assert frozen_profile["creative_style"]["look_uuid"] == look_uuid
    assert frozen_profile["creative_style"]["look_amount"] == 135


def test_lightroom_jpeg_reconciliation_trusts_only_managed_lut_worker_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = tmp_path / "photo.ARW"
    raw.write_bytes(b"raw")
    jpeg = tmp_path / "photo.jpg"
    Image.new("RGB", (24, 16), (32, 96, 192)).save(jpeg, "JPEG", quality=95)
    before = jpeg.read_bytes()
    captured: list[dict] = []
    monkeypatch.setattr(
        web_module,
        "record_export_result",
        lambda *_args, **kwargs: captured.append(kwargs),
    )
    monkeypatch.setattr(
        web_module, "finish_export_attempt", lambda *_args, **_kwargs: None
    )
    spec = {
        "jpeg_settings": {"quality": 90},
        "items": [
            {
                "item_id": "1",
                "path": str(raw),
                "recipe": {
                    "creative_style": {
                        "status": "confirmed",
                        "look_kind": "rendered_lut",
                        "lut_id": "lut-1",
                        "lut_hash": "a" * 64,
                        "strength": 150,
                        "xmp_compatible": False,
                    }
                },
            }
        ],
    }
    execution = {
        "attempt_id": "attempt-1",
        "work": [{"item_id": "1", "target": "jpeg"}],
    }
    batch = {
        "tasks": [
            {
                "photo_path": str(raw),
                "result": {
                    "jpeg_status": "done",
                    "jpeg_path": str(jpeg),
                },
            }
        ]
    }

    web_module._reconcile_lightroom_export(
        tmp_path / "data",
        spec,
        execution,
        batch,
        "failed",
        {
            "protocol": "PHOTO_AI_LUT_EXPORT/1",
            "attempt_id": "attempt-1",
            "items": [
                {
                    "item_id": "1",
                    "succeeded": True,
                    "output": str(jpeg),
                }
            ],
        },
    )

    # Reconciliation is lightweight state handling: the managed Worker has
    # already replaced the JPEG and Service must never render it again.
    assert jpeg.read_bytes() == before
    assert captured[0]["target"] == "jpeg"
    assert captured[0]["succeeded"] is True
    assert captured[0]["output"] == str(jpeg)


def test_lut_preview_and_group_selection_api_keep_exact_hash_and_strength(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "look.cube"
    source.write_text(
        "LUT_1D_SIZE 2\n0.0 0.0 0.0\n1.0 0.8 0.6\n",
        encoding="utf-8",
    )
    engine = CreativeLutEngine(tmp_path / "runtime", enforce_e_drive=False)
    descriptor = engine.import_lut(source)
    monkeypatch.setattr(
        CreativeLutEngine,
        "for_project",
        classmethod(lambda _cls, _project_root: engine),
    )
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)
    plan_path = data_dir / "runs" / RUN_ID / "develop.json"
    plan = read_json(plan_path)
    plan["creative_style"] = {
        "status": "pending",
        "groups": {
            "1": {
                "group_id": 1,
                "status": "pending",
                "top3": [
                    {
                        "lut_id": descriptor.lut_id,
                        "lut_hash": descriptor.lut_hash,
                        "look_kind": "rendered_lut",
                        "xmp_compatible": False,
                        "strength": 150,
                        "amount_supported": True,
                        "render_status": "ready",
                        "preview_key": "b" * 64,
                        "preview_path": str(tmp_path / "lut-preview.jpg"),
                    }
                ],
            }
        },
    }
    write_json(plan_path, plan)
    captured: dict = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {"id": "lut-preview", "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    preview = client.post(
        f"/api/runs/{RUN_ID}/style-preview",
        json={
            "base_revision": revision,
            "group_id": 1,
            "lut_id": descriptor.lut_id,
            "lut_hash": descriptor.lut_hash,
            "strength": 150,
        },
        headers=headers,
    )

    assert preview.status_code == 200, preview.text
    assert captured["kind"] == "style_preview"
    assert captured["args"][captured["args"].index("--lut-id") + 1] == descriptor.lut_id
    assert (
        captured["args"][captured["args"].index("--lut-hash") + 1]
        == descriptor.lut_hash
    )
    assert captured["args"][captured["args"].index("--amount") + 1] == "150"
    assert captured["context"]["requires_lightroom"] is False
    assert captured["context"]["lightroom_state"] == "not_required"

    selected = client.put(
        f"/api/runs/{RUN_ID}/develop/style/groups/1",
        json={
            "base_revision": revision,
            "lut_id": descriptor.lut_id,
            "lut_hash": descriptor.lut_hash,
            "amount": 150,
            "strength": 150,
            "status": "confirmed",
        },
        headers=headers,
    )
    assert selected.status_code == 200, selected.text
    group = selected.json()["creative_style"]["groups"]["1"]
    assert group["lut_id"] == descriptor.lut_id
    assert group["lut_hash"] == descriptor.lut_hash
    assert group["strength"] == 150
    assert group["xmp_compatible"] is False


def test_style_recommendation_rejects_loaded_legacy_plugin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(
        "landscape_culler.web._configured_style_catalog",
        lambda *_args: {"default_pool": ["uuid:test"], "entries": []},
    )
    monkeypatch.setattr(
        "landscape_culler.web._lightroom_status",
        lambda *_args: {
            "configured": True,
            "plugin": {"points_to_this_bridge": True},
            "lightroom": {"compatible": True, "executable": str(executable)},
            "heartbeat": {"state": "online", "plugin_version": "0.1.2"},
        },
    )

    response = client.post(
        f"/api/runs/{RUN_ID}/style-recommendations",
        json={"base_revision": revision, "scope": "global"},
        headers=headers,
    )

    assert response.status_code == 409
    assert "旧插件 0.1.2" in response.json()["detail"]
    assert bridge.PLUGIN_VERSION in response.json()["detail"]
    assert "重新加载" in response.json()["detail"]


@pytest.mark.parametrize("heartbeat_state", ["online", "stale"])
def test_style_recommendation_with_current_or_reconnecting_plugin_starts_job(
    tmp_path: Path,
    monkeypatch,
    heartbeat_state: str,
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(
        "landscape_culler.web._configured_style_catalog",
        lambda *_args: {"default_pool": ["uuid:test"], "entries": []},
    )
    monkeypatch.setattr(
        "landscape_culler.web._lightroom_status",
        lambda *_args: {
            "configured": True,
            "plugin": {"points_to_this_bridge": True},
            "lightroom": {"compatible": True, "executable": str(executable)},
            "heartbeat": {
                "state": heartbeat_state,
                "plugin_version": bridge.PLUGIN_VERSION,
            },
        },
    )
    captured: dict = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {"id": "style-test", "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    response = client.post(
        f"/api/runs/{RUN_ID}/style-recommendations",
        json={"base_revision": revision, "scope": "group", "group_id": 1},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["id"] == "style-test"
    assert captured["kind"] == "style_recommend"
    assert captured["args"][0] == "style-recommend"
    assert captured["args"][captured["args"].index("--run-dir") + 1] == str(
        data_dir / "runs" / RUN_ID
    )
    assert captured["args"][captured["args"].index("--base-revision") + 1] == str(
        revision
    )
    assert captured["args"][captured["args"].index("--group-id") + 1] == "1"
    assert captured["context"]["batch_id"].startswith(f"style-{RUN_ID}-")
    assert captured["context"]["base_revision"] == revision
    assert captured["context"]["scope"] == "group"
    assert captured["context"]["requires_lightroom"] is True
    assert captured["context"]["lightroom_state"] == heartbeat_state


def test_all_groups_style_recommendation_starts_one_independent_group_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(
        "landscape_culler.web._configured_style_catalog",
        lambda *_args: {"default_pool": ["uuid:test"], "entries": []},
    )
    monkeypatch.setattr(
        "landscape_culler.web._lightroom_status",
        lambda *_args: {
            "configured": True,
            "plugin": {"points_to_this_bridge": True},
            "lightroom": {"compatible": True, "executable": str(executable)},
            "heartbeat": {
                "state": "online",
                "plugin_version": bridge.PLUGIN_VERSION,
            },
        },
    )
    captured: dict[str, Any] = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {
            "id": "style-all-groups",
            "status": "queued",
            "title": context["title"],
        }

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)

    response = client.post(
        f"/api/runs/{RUN_ID}/style-recommendations/groups",
        json={"base_revision": revision},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert captured["kind"] == "style_recommend"
    assert captured["args"][0] == "style-recommend"
    assert "--all-groups" in captured["args"]
    assert "--group-id" not in captured["args"]
    assert captured["args"][captured["args"].index("--run-dir") + 1] == str(
        data_dir / "runs" / RUN_ID
    )
    assert captured["context"]["scope"] == "groups"
    assert captured["context"]["group_id"] is None
    assert captured["context"]["group_count"] == 1
    assert captured["context"]["batch_id"].startswith(f"style-groups-{RUN_ID}-")
    assert captured["context"]["requires_lightroom"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "group"},
        {"scope": "global", "group_id": 1},
    ],
)
def test_style_recommendation_scope_contract_requires_exact_target(
    tmp_path: Path,
    monkeypatch,
    payload: dict[str, Any],
) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)

    response = client.post(
        f"/api/runs/{RUN_ID}/style-recommendations",
        json={"base_revision": revision, **payload},
        headers=headers,
    )

    assert response.status_code == 422


def test_global_style_recommendation_uses_project_scope_sentinel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"test")
    monkeypatch.setattr(
        "landscape_culler.web._configured_style_catalog",
        lambda *_args: {"default_pool": ["uuid:test"], "entries": []},
    )
    monkeypatch.setattr(
        "landscape_culler.web._lightroom_status",
        lambda *_args: {
            "configured": True,
            "plugin": {"points_to_this_bridge": True},
            "lightroom": {"compatible": True, "executable": str(executable)},
            "heartbeat": {
                "state": "online",
                "plugin_version": bridge.PLUGIN_VERSION,
            },
        },
    )
    captured: dict[str, Any] = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {"id": "style-global", "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    response = client.post(
        f"/api/runs/{RUN_ID}/style-recommendations",
        json={"base_revision": revision, "scope": "global"},
        headers=headers,
    )

    assert response.status_code == 200
    assert captured["args"][captured["args"].index("--group-id") + 1] == "0"
    assert captured["context"]["scope"] == "global"
    assert captured["context"]["group_id"] is None


def test_global_style_preview_uses_global_selection_and_project_scope_sentinel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)
    plan_path = data_dir / "runs" / RUN_ID / "develop.json"
    plan = read_json(plan_path)
    preset_id = "adobe-film-12"
    preset_hash = "f" * 64
    plan["creative_style"] = {
        "scope": "global",
        "status": "pending",
        "groups": {},
        "global_selection": {
            "scope": "global",
            "status": "pending",
            "recommendation_status": "complete",
            "top3": [
                {
                    "preset_id": preset_id,
                    "preset_hash": preset_hash,
                    "preset_uuid": "9901408EBF8D496E99EFC526805F2F7C",
                    "preset_scope": "catalog",
                    "look_kind": "lightroom_profile",
                    "amount_supported": True,
                    "render_status": "ready",
                    "preview_key": "b" * 64,
                }
            ],
        },
    }
    write_json(plan_path, plan)
    executable = tmp_path / "Lightroom.exe"
    executable.write_bytes(b"test")
    catalog_entry = {
        "preset_id": preset_id,
        "file_hash": preset_hash,
        "preset_scope": "catalog",
        "look_kind": "lightroom_profile",
        "supports_amount": True,
    }
    monkeypatch.setattr(
        "landscape_culler.web._configured_style_catalog",
        lambda *_args: {"default_pool": [preset_id], "entries": [catalog_entry]},
    )
    monkeypatch.setattr(
        "landscape_culler.web._lightroom_status",
        lambda *_args: {
            "configured": True,
            "plugin": {"points_to_this_bridge": True},
            "lightroom": {"compatible": True, "executable": str(executable)},
            "heartbeat": {
                "state": "online",
                "plugin_version": bridge.PLUGIN_VERSION,
            },
        },
    )
    captured: dict[str, Any] = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {"id": "style-preview-global", "status": "queued"}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    response = client.post(
        f"/api/runs/{RUN_ID}/style-preview",
        json={
            "base_revision": revision,
            "scope": "global",
            "group_id": None,
            "preset_id": preset_id,
            "preset_hash": preset_hash,
            "amount": 135,
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert captured["kind"] == "style_preview"
    assert captured["args"][captured["args"].index("--group-id") + 1] == "0"
    assert captured["args"][captured["args"].index("--amount") + 1] == "135"
    assert captured["context"]["scope"] == "global"
    assert captured["context"]["group_id"] is None


def test_global_style_selection_api_persists_one_choice_without_copying_groups(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)
    plan_path = data_dir / "runs" / RUN_ID / "develop.json"
    plan = read_json(plan_path)
    plan["creative_style"] = {
        "scope": "global",
        "status": "pending",
        "groups": {},
        "global_selection": {
            "scope": "global",
            "status": "pending",
            "recommendation_status": "complete",
            "recommended_kind": "preset",
            "recommended_preset_id": "profile-1",
            "recommended_preset_hash": "profile-hash",
            "recommended_amount": 135,
            "neutral_preview_key": "a" * 64,
            "top3": [
                {
                    "preset_id": "profile-1",
                    "preset_hash": "profile-hash",
                    "preset_uuid": None,
                    "preset_scope": "catalog",
                    "look_kind": "lightroom_profile",
                    "profile_name": "Modern 10",
                    "profile_hash": "profile-hash",
                    "xmp_compatible": True,
                    "amount": 135,
                    "amount_supported": True,
                    "render_status": "ready",
                    "preview_key": "b" * 64,
                }
            ],
        },
    }
    write_json(plan_path, plan)

    response = client.put(
        f"/api/runs/{RUN_ID}/develop/style/global",
        json={
            "base_revision": revision,
            "preset_id": "profile-1",
            "preset_hash": "profile-hash",
            "amount": 135,
            "status": "confirmed",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    creative = response.json()["creative_style"]
    assert creative["scope"] == "global"
    assert creative["groups"] == {}
    selection = creative["global_selection"]
    assert selection["status"] == "confirmed"
    assert selection["preset_id"] == "profile-1"
    assert selection["preset_hash"] == "profile-hash"
    assert selection["look_kind"] == "lightroom_profile"
    assert selection["profile_name"] == "Modern 10"
    assert selection["amount"] == 135
    assert response.json()["revision"] == revision + 1


def test_style_preview_requires_lowercase_sha256_key_and_serves_run_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    preview_key = hashlib.sha256(b"style preview").hexdigest()
    preview_dir = data_dir / "runs" / RUN_ID / "style-previews"
    preview_dir.mkdir()
    preview_path = preview_dir / f"{preview_key}.jpg"
    Image.new("RGB", (32, 20), "#8f7359").save(preview_path, "JPEG")

    response = client.get(f"/api/runs/{RUN_ID}/style-preview/{preview_key}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == preview_path.read_bytes()
    for invalid in (
        preview_key[:-1],
        preview_key.upper(),
        "g" * 64,
        f"{preview_key}.jpg",
    ):
        assert (
            client.get(f"/api/runs/{RUN_ID}/style-preview/{invalid}").status_code == 404
        )


def test_public_develop_plan_exposes_global_sample_urls_without_disk_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    _headers, _revision = _prepare_style_plan(client, token=token)
    plan_path = data_dir / "runs" / RUN_ID / "develop.json"
    plan = read_json(plan_path)
    preview_key = "c" * 64
    private_path = str(
        data_dir / "runs" / RUN_ID / "style-previews" / f"{preview_key}.jpg"
    )
    sample = {
        "role": "representative",
        "index": 0,
        "filename": "source.ARW",
        "preview_key": preview_key,
        "preview_path": private_path,
        "source_path": str(_raw),
    }
    plan["creative_style"] = {
        "status": "pending",
        "scope": "global",
        "groups": {},
        "global_selection": {
            "status": "confirmed",
            "recommendation_status": "complete",
            "preset_id": "profile-1",
            "preset_hash": "profile-hash",
            "amount": 100,
            "selected_preview_samples": [sample],
            "top3": [
                {
                    "preset_id": "profile-1",
                    "preset_hash": "profile-hash",
                    "render_status": "ready",
                    "preview_key": preview_key,
                    "preview_path": private_path,
                    "preview_samples": [sample],
                }
            ],
        },
    }
    write_json(plan_path, plan)

    response = client.get(f"/api/runs/{RUN_ID}/develop")

    assert response.status_code == 200, response.text
    selection = response.json()["creative_style"]["global_selection"]
    expected_url = f"/api/runs/{RUN_ID}/style-preview/{preview_key}"
    assert selection["selected_preview_samples"][0]["preview_url"] == expected_url
    assert selection["top3"][0]["preview_samples"][0]["preview_url"] == expected_url
    serialized = json.dumps(response.json())
    assert private_path not in serialized
    assert str(_raw) not in serialized


def test_confirm_recommended_styles_rejects_pending_then_accepts_complete_neutral(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers, revision = _prepare_style_plan(client, token=token)
    selected = client.post(
        f"/api/runs/{RUN_ID}/develop/options",
        json={"base_revision": revision, "mode": "style"},
        headers=headers,
    )
    assert selected.status_code == 200
    revision = int(selected.json()["revision"])
    plan_path = data_dir / "runs" / RUN_ID / "develop.json"
    plan = read_json(plan_path)
    plan["creative_style"] = {
        "status": "pending",
        "groups": {
            "1": {
                "group_id": 1,
                "status": "pending",
                "recommendation_status": "pending",
                "recommended_kind": "pending",
                "manual_override": False,
                "top3": [],
            }
        },
    }
    write_json(plan_path, plan)

    pending = client.post(
        f"/api/runs/{RUN_ID}/develop/style/confirm-all",
        json={"base_revision": revision},
        headers=headers,
    )
    assert pending.status_code == 409
    assert "真实预览" in pending.json()["detail"]

    plan = read_json(plan_path)
    plan["creative_style"]["groups"]["1"].update(
        recommendation_status="complete",
        recommended_kind="neutral",
        recommended_preset_id=None,
        preset_id=None,
        neutral_preview_key="a" * 64,
    )
    write_json(plan_path, plan)
    confirmed = client.post(
        f"/api/runs/{RUN_ID}/develop/style/confirm-all",
        json={"base_revision": revision},
        headers=headers,
    )

    assert confirmed.status_code == 200
    payload = confirmed.json()
    assert payload["revision"] == revision + 1
    assert payload["creative_style"]["status"] == "confirmed"
    neutral = payload["creative_style"]["groups"]["1"]
    assert neutral["status"] == "skipped"
    assert neutral["preset_id"] is None
    assert neutral["amount"] == 0


def test_lightroom_status_and_configure_use_e_drive_bridge(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    plugin_dir = (
        Path(__file__).parents[1] / "integrations" / "photo-ai-lightroom.lrplugin"
    )
    captured: dict = {}
    status = {
        "root": str(data_dir / "lightroom-bridge"),
        "configured": True,
        "heartbeat": {"state": "offline"},
        "plugin": {"points_to_this_bridge": True},
        "lightroom": {"compatible": True, "executable": r"E:\Adobe\Lightroom.exe"},
    }

    def fake_status(received_data_dir: Path, received_project_root: Path) -> dict:
        captured.update(
            status_data_dir=received_data_dir, status_project_root=received_project_root
        )
        return status

    def fake_config(received_data_dir: Path, *, plugin_dir: Path) -> dict:
        captured.update(config_data_dir=received_data_dir, plugin_dir=plugin_dir)
        return {"root": str(received_data_dir / "lightroom-bridge")}

    monkeypatch.setattr("landscape_culler.web._lightroom_status", fake_status)
    monkeypatch.setattr(
        "landscape_culler.web.detect_lightroom_classic_15_3",
        lambda _roots=None: Path(status["lightroom"]["executable"]),
    )
    monkeypatch.setattr(
        "landscape_culler.web._require_e_drive_lightroom_storage",
        lambda *_args: plugin_dir,
    )
    monkeypatch.setattr(
        "landscape_culler.web.write_lightroom_plugin_config", fake_config
    )
    monkeypatch.setattr(
        "landscape_culler.web.install_lightroom_plugin",
        lambda template: {
            "installed": True,
            "plugin_dir": str(template),
        },
    )

    response = client.get("/api/lightroom/status")
    assert response.status_code == 200
    assert response.json() == status
    assert client.post("/api/lightroom/configure", json={}).status_code == 403
    configured = client.post(
        "/api/lightroom/configure",
        json={},
        headers={"X-Photo-AI-Token": token},
    )
    assert configured.status_code == 200
    assert configured.json()["configuration"]["restart_required"] is True
    assert configured.json()["configuration"]["plugin_installed"] is True
    assert captured["config_data_dir"] == data_dir
    assert captured["plugin_dir"] == plugin_dir


def test_lightroom_manual_path_is_validated_and_saved_on_data_drive(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    executable = tmp_path / "Adobe Lightroom Classic 15.3" / "Lightroom.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"test executable")
    plugin_dir = Path(__file__).parents[1] / "integrations" / "photo-ai-lightroom.lrplugin"
    monkeypatch.setattr(
        "landscape_culler.web._require_e_drive_lightroom_storage",
        lambda *_args: plugin_dir,
    )
    monkeypatch.setattr(
        "landscape_culler.web.write_lightroom_plugin_config",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "landscape_culler.web.install_lightroom_plugin",
        lambda template: {
            "installed": True,
            "plugin_dir": str(template),
        },
    )

    missing = client.post(
        "/api/lightroom/configure",
        json={"executable_path": str(tmp_path / "missing")},
        headers=headers,
    )
    assert missing.status_code == 409

    configured = client.post(
        "/api/lightroom/configure",
        json={"executable_path": str(executable)},
        headers=headers,
    )
    assert configured.status_code == 200
    assert configured.json()["lightroom"]["executable"] == str(executable.resolve())
    settings = read_json(data_dir / "lightroom" / "settings.json")
    assert settings["executable_path"] == str(executable.resolve())


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/audit", {"library": "unused", "pending": "unused"}),
        ("/api/train", {"library": "unused"}),
        ("/api/score", {"input_path": "unused"}),
        (
            f"/api/runs/{RUN_ID}/lightroom/apply",
            {"base_revision": 0, "develop_revision": 0},
        ),
        (
            f"/api/runs/{RUN_ID}/xmp/apply",
            {"base_revision": 0, "stage": "rating"},
        ),
        (
            f"/api/runs/{RUN_ID}/xmp/dry-run",
            {"min_rating": 3},
        ),
        (
            f"/api/runs/{RUN_ID}/xmp/commit",
            {"plan_id": "xmp-obsolete-plan"},
        ),
    ],
)
def test_removed_legacy_mutation_endpoints_return_404(
    tmp_path: Path,
    monkeypatch,
    path: str,
    payload: dict[str, Any],
) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    response = client.post(
        path,
        json=payload,
        headers={"X-Photo-AI-Token": token},
    )
    assert response.status_code == 404

def test_lightroom_progress_plan_and_job_result_use_batch_id(
    tmp_path: Path, monkeypatch
) -> None:
    plan = _job_progress_plan("lightroom_apply", {})
    assert [step["key"] for step in plan] == ["launch", "queue", "process", "finalize"]
    assert sum(step["weight"] for step in plan) == 100
    manager = JobManager(tmp_path / "data", Path(__file__).parents[1])
    monkeypatch.setattr(
        "landscape_culler.web.read_lightroom_batch_status",
        lambda data_dir, batch_id: {
            "batch_id": batch_id,
            "status": "complete",
            "root": str(data_dir),
        },
    )
    result = manager._find_result(
        {"kind": "lightroom_apply", "context": {"batch_id": "lr-safe-batch"}}
    )
    assert result["batch_id"] == "lr-safe-batch"
    assert result["status"] == "complete"


def test_style_progress_plan_uses_worker_process_phase() -> None:
    plan = _job_progress_plan("style_recommend", {"scope": "groups"})

    keys = [step["key"] for step in plan]
    assert keys == [
        "launch",
        "presets",
        "representative",
        "scene",
        "recall",
        "process",
        "collect",
        "rerank",
        "finalize",
    ]
    assert sum(step["weight"] for step in plan) == 100

    job = {
        "kind": "style_recommend",
        "context": {"scope": "groups"},
        "progress": None,
    }
    _apply_job_progress(
        job,
        {
            "phase": "process",
            "label": "Lightroom 自动处理",
            "current": 51,
            "total": 132,
            "unit": "张",
        },
    )
    assert job["progress"]["stage_label"] == "Lightroom 真实预览"
    assert job["progress"]["unit"] == "项预览"


def test_style_job_result_reports_partial_group_counts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    run_id = "20260902-120000-123456"
    run_dir = data_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    write_json(run_dir / "develop.json", {"revision": 8})
    write_json(
        run_dir / "style-recommendations.json",
        {
            "status": "partial",
            "worker": {
                "successful_group_ids": ["1", "2"],
                "failed_group_ids": ["3"],
            },
        },
    )
    manager = JobManager(data_dir, Path(__file__).parents[1])

    result = manager._find_result(
        {
            "kind": "style_recommend",
            "context": {"run_id": run_id, "batch_id": "style-partial"},
        }
    )

    assert result["style_status"] == "partial"
    assert result["succeeded_group_count"] == 2
    assert result["failed_group_count"] == 1


def test_native_ai_crash_gets_durable_cuda_guidance(
    tmp_path: Path, monkeypatch
) -> None:
    manager = JobManager(tmp_path / "data", Path(__file__).parents[1])
    manager.jobs["native-crash"] = {
        "id": "native-crash",
        "kind": "score",
        "title": "AI score",
        "status": "queued",
        "stage": "AI 评分",
        "message": "0 / 10张",
        "created_at": "2026-09-01T10:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "context": {"mode": "deep"},
        "progress": {
            "stage_label": "通用审美评分",
            "nodes": [
                {"key": "aesthetic", "label": "通用审美评分", "status": "active"},
                {"key": "local_vlm", "label": "构图组内评审", "status": "pending"},
            ],
        },
        "log_tail": [],
        "result": None,
        "command": ["unused"],
    }

    class CrashedProcess:
        pid = 123
        stdout = iter(())

        def wait(self, timeout=None):
            return 0xC0000005

    monkeypatch.setattr(
        "landscape_culler.web.subprocess.Popen",
        lambda *_args, **_kwargs: CrashedProcess(),
    )
    manager._run("native-crash")

    failed = manager.get("native-crash")
    assert failed["status"] == "failed"
    assert failed["exit_code"] == 0xC0000005
    assert "Windows 访问冲突（0xC0000005）" in failed["message"]
    assert "分组和缓存已保留" in failed["message"]
    assert failed["progress"]["nodes"][0]["status"] == "failed"


def test_job_manager_cancel_marks_published_lightroom_batch(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    batch_id = "lr-published"
    create_lightroom_batch(
        data_dir,
        [LightroomTask((tmp_path / "photo.ARW").resolve(), task_id="photo")],
        batch_id=batch_id,
    )
    manager = JobManager(data_dir, Path(__file__).parents[1])
    job = {
        "id": "web-lightroom-job",
        "kind": "lightroom_apply",
        "title": "Lightroom test",
        "status": "running",
        "stage": "Lightroom 自动处理",
        "message": "",
        "created_at": "2026-09-01T10:00:00+00:00",
        "started_at": "2026-09-01T10:00:01+00:00",
        "finished_at": None,
        "exit_code": None,
        "context": {"batch_id": batch_id},
        "progress": None,
        "log_tail": [],
        "result": None,
        "command": [],
    }
    manager.jobs[job["id"]] = job
    manager._save(job)

    cancelled = manager.cancel(job["id"])
    assert cancelled["status"] == "cancelling"
    assert cancelled["result"]["status"] == "cancelling"
    assert (bridge_paths(data_dir).cancelled / f"{batch_id}.cancel").is_file()
    assert read_lightroom_batch_status(data_dir, batch_id)["status"] == "cancelling"


def test_job_manager_restart_interrupts_job_and_cancels_lightroom_queue(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    batch_id = "lr-restart"
    create_lightroom_batch(
        data_dir,
        [LightroomTask((tmp_path / "photo.ARW").resolve(), task_id="photo")],
        batch_id=batch_id,
    )
    jobs_dir = data_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True)
    write_json(
        jobs_dir / "stale-job.json",
        {
            "id": "stale-job",
            "kind": "lightroom_apply",
            "title": "Lightroom stale",
            "status": "running",
            "created_at": "2026-09-01T10:00:00+00:00",
            "context": {"batch_id": batch_id},
        },
    )

    manager = JobManager(data_dir, Path(__file__).parents[1])
    recovered = manager.get("stale-job")
    assert recovered["status"] == "interrupted"
    assert recovered["result"]["status"] == "cancelling"
    assert (bridge_paths(data_dir).cancelled / f"{batch_id}.cancel").is_file()


def test_job_manager_shutdown_marks_queued_lightroom_batch_before_worker_start(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    batch_id = "lr-shutdown-queued"
    manager = JobManager(data_dir, Path(__file__).parents[1])
    manager.jobs["queued-job"] = {
        "id": "queued-job",
        "kind": "lightroom_apply",
        "title": "Lightroom queued",
        "status": "queued",
        "created_at": "2026-09-01T10:00:00+00:00",
        "context": {"batch_id": batch_id},
        "command": [],
    }

    manager.shutdown()

    stopped = manager.get("queued-job")
    assert stopped["status"] == "cancelling"
    assert stopped["result"]["status"] == "cancelled"
    assert (bridge_paths(data_dir).cancelled / f"{batch_id}.cancel").is_file()


def test_cancelled_lightroom_worker_exit_zero_is_not_marked_completed(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    batch_id = "lr-zero-after-cancel"
    create_lightroom_batch(
        data_dir,
        [LightroomTask((tmp_path / "photo.ARW").resolve(), task_id="photo")],
        batch_id=batch_id,
    )
    manager = JobManager(data_dir, Path(__file__).parents[1])
    job = {
        "id": "zero-job",
        "kind": "lightroom_apply",
        "title": "Lightroom zero",
        "status": "queued",
        "stage": "等待启动",
        "message": "",
        "created_at": "2026-09-01T10:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "context": {"batch_id": batch_id},
        "progress": None,
        "log_tail": [],
        "result": None,
        "command": ["unused"],
    }
    manager.jobs[job["id"]] = job

    class CancelOnIteration:
        def __iter__(self):
            manager.cancel(job["id"])
            return iter(())

    class FakeProcess:
        pid = 123
        stdout = CancelOnIteration()

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(
        "landscape_culler.web.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess()
    )
    manager._run(job["id"])

    final = manager.get(job["id"])
    assert final["exit_code"] == 0
    assert final["status"] == "cancelled"
    assert final["result"]["status"] == "cancelling"


def test_failed_lightroom_worker_cancels_remaining_plugin_queue(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    batch_id = "lr-worker-failed"
    create_lightroom_batch(
        data_dir,
        [LightroomTask((tmp_path / "photo.ARW").resolve(), task_id="photo")],
        batch_id=batch_id,
    )
    manager = JobManager(data_dir, Path(__file__).parents[1])
    job = {
        "id": "failed-worker",
        "kind": "lightroom_apply",
        "title": "Lightroom failed",
        "status": "queued",
        "stage": "等待启动",
        "message": "",
        "created_at": "2026-09-01T10:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "context": {"batch_id": batch_id},
        "progress": None,
        "log_tail": [],
        "result": None,
        "command": ["unused"],
    }
    manager.jobs[job["id"]] = job

    class FakeProcess:
        pid = 456
        stdout = iter(())

        def wait(self, timeout=None):
            return 9

    monkeypatch.setattr(
        "landscape_culler.web.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess()
    )
    manager._run(job["id"])

    final = manager.get(job["id"])
    assert final["exit_code"] == 9
    assert final["status"] == "failed"
    assert final["result"]["status"] == "cancelling"
    assert (bridge_paths(data_dir).cancelled / f"{batch_id}.cancel").is_file()


def test_failed_worker_preserves_already_terminal_lightroom_failure(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    batch_id = "lr-terminal-failure"
    photo = (tmp_path / "photo.ARW").resolve()
    create_lightroom_batch(
        data_dir,
        [LightroomTask(photo, task_id="photo")],
        batch_id=batch_id,
    )
    paths = bridge_paths(data_dir)
    (paths.pending / f"{batch_id}--photo.task").unlink()
    (paths.failed / f"{batch_id}--photo.result").write_text(
        bridge._line(
            bridge.RESULT_PROTOCOL,
            {
                "batch_id": batch_id,
                "task_id": "photo",
                "photo_path": str(photo),
                "status": "failed",
                "finished_at": "2026-09-01T10:00:00Z",
                "message": "catalog locked",
            },
        ),
        encoding="utf-8",
    )
    manager = JobManager(data_dir, Path(__file__).parents[1])
    manager.jobs["terminal-failure"] = {
        "id": "terminal-failure",
        "kind": "lightroom_apply",
        "title": "Lightroom terminal failure",
        "status": "queued",
        "stage": "等待启动",
        "message": "",
        "created_at": "2026-09-01T10:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "context": {"batch_id": batch_id},
        "progress": None,
        "log_tail": [],
        "result": None,
        "command": ["unused"],
    }

    class FakeProcess:
        pid = 789
        stdout = iter(())

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(
        "landscape_culler.web.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess()
    )
    manager._run("terminal-failure")

    final = manager.get("terminal-failure")
    assert final["status"] == "failed"
    assert final["result"]["status"] == "failed"
    assert not (paths.cancelled / f"{batch_id}.cancel").exists()


def test_bootstrap_ignores_dormant_personal_model_and_audit_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    audit_path = data_dir / "audit.json"
    model_path = data_dir / "models" / "personal-v1" / "metadata.json"
    model_path.parent.mkdir(parents=True)
    audit_path.write_bytes(b'{"legacy": "audit"}')
    model_path.write_bytes(b'{"legacy": "model"}')
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (audit_path, model_path)
    }

    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert "audit" not in payload
    assert "model" not in payload
    assert "library" not in payload["defaults"]
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (audit_path, model_path)
    } == before

def test_new_project_starts_grouping_without_scoring(
    tmp_path: Path, monkeypatch
) -> None:
    client, _data_dir, raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    captured: dict = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {"id": "test", "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    response = client.post(
        "/api/group",
        json={"input_path": str(raw.parent), "retain_ratio": 0.25, "mode": "deep"},
        headers={"X-Photo-AI-Token": token},
    )
    assert response.status_code == 200
    assert captured["kind"] == "group"
    assert captured["args"][0] == "group"
    assert "score" not in captured["args"]


def test_empty_project_is_created_before_grouping_and_can_be_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, raw = _client(tmp_path, monkeypatch)
    project_dir = raw.parent / "new-project"
    project_dir.mkdir()
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    assert (
        client.post("/api/projects", json={"input_path": str(project_dir)}).status_code
        == 403
    )
    created = client.post(
        "/api/projects",
        json={"input_path": str(project_dir)},
        headers=headers,
    )
    assert created.status_code == 200
    project = created.json()
    assert project["input_root"] == str(project_dir.resolve())
    assert project["latest_run_id"] is None
    assert project["version_count"] == 0
    assert project["workflow_state"] == "created"
    registration = data_dir / "projects" / f"{project['project_id']}.json"
    assert registration.is_file()

    repeated = client.post(
        "/api/projects",
        json={"input_path": str(project_dir)},
        headers=headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["project_id"] == project["project_id"]

    captured: dict[str, Any] = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {"id": "project-group", "kind": kind, "status": "queued"}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    grouped = client.post(
        f"/api/projects/{project['project_id']}/group",
        json={"retain_ratio": 0.3, "mode": "deep"},
        headers=headers,
    )
    assert grouped.status_code == 200
    assert captured["kind"] == "group"
    assert captured["args"][captured["args"].index("--input") + 1] == str(
        project_dir.resolve()
    )
    assert captured["context"]["project_id"] == project["project_id"]

    deleted = client.request(
        "DELETE",
        f"/api/projects/{project['project_id']}",
        json={"confirmation": "删除工程"},
        headers=headers,
    )
    assert deleted.status_code == 200
    assert deleted.json()["version_count"] == 0
    assert not registration.exists()
    recycle = next((data_dir / "trash" / "projects").iterdir())
    assert (recycle / "registration.json").is_file()


def test_manual_group_edit_persists_and_marks_scored_run_stale(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    moved = client.patch(
        f"/api/runs/{RUN_ID}/groups/0",
        json={"group_id": 7, "base_revision": 0},
        headers=headers,
    )
    assert moved.status_code == 200
    assert moved.json()["needs_rescore"] is True
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["results"][0]["group_id"] == 7
    assert detail["results"][0]["ai_group_id"] == 1
    assert detail["results"][0]["manual_group_override"] is True
    assert detail["xmp_ready"] is False
    assert (
        read_json(data_dir / "runs" / RUN_ID / "results.json")["results"][0]["group_id"]
        == 1
    )

    stale = client.patch(
        f"/api/runs/{RUN_ID}/groups/0",
        json={"group_id": 8, "base_revision": 0},
        headers=headers,
    )
    assert stale.status_code == 409

    blocked = client.post(
        f"/api/runs/{RUN_ID}/exports/prepare",
        json={"base_revision": 1, "xmp": True, "jpeg": False},
        headers=headers,
    )
    assert blocked.status_code == 409

    restored = client.patch(
        f"/api/runs/{RUN_ID}/groups/0",
        json={"group_id": None, "base_revision": 1},
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.json()["needs_rescore"] is False


def test_bulk_group_move_and_new_group_are_atomic(tmp_path: Path, monkeypatch) -> None:
    client, data_dir, first = _client(tmp_path, monkeypatch)
    second = first.parent / "DSC0002.ARW"
    third = first.parent / "DSC0003.ARW"
    _append_result(data_dir, second, 1)
    _append_result(data_dir, third, 2)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    moved = client.patch(
        f"/api/runs/{RUN_ID}/groups",
        json={"indexes": [0, 1, 1], "group_id": 2, "base_revision": 0},
        headers=headers,
    )
    assert moved.status_code == 200
    assert moved.json()["indexes"] == [0, 1]
    assert moved.json()["review_revision"] == 1
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert [detail["results"][index]["group_id"] for index in (0, 1, 2)] == [2, 2, 2]

    created = client.patch(
        f"/api/runs/{RUN_ID}/groups",
        json={"indexes": [0, 1], "new_group": True, "base_revision": 1},
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json()["group_id"] == 3
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert [detail["results"][index]["group_id"] for index in (0, 1, 2)] == [3, 3, 2]

    before = read_json(data_dir / "runs" / RUN_ID / "review.json")
    invalid = client.patch(
        f"/api/runs/{RUN_ID}/groups",
        json={"indexes": [0, 99], "group_id": 2, "base_revision": 2},
        headers=headers,
    )
    assert invalid.status_code == 404
    assert read_json(data_dir / "runs" / RUN_ID / "review.json") == before


def test_bulk_group_shift_moves_each_selection_to_adjacent_group_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, first = _client(tmp_path, monkeypatch)
    _append_result(data_dir, first.parent / "DSC0002.ARW", 2)
    _append_result(data_dir, first.parent / "DSC0003.ARW", 3)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    shifted = client.patch(
        f"/api/runs/{RUN_ID}/groups",
        json={"indexes": [0, 1, 2], "direction": "next", "base_revision": 0},
        headers=headers,
    )
    assert shifted.status_code == 200
    assert shifted.json()["moved_count"] == 2
    assert shifted.json()["review_revision"] == 1
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert [item["group_id"] for item in detail["results"]] == [2, 3, 3]

    shifted_back = client.patch(
        f"/api/runs/{RUN_ID}/groups",
        json={"indexes": [0, 1, 2], "direction": "previous", "base_revision": 1},
        headers=headers,
    )
    assert shifted_back.status_code == 200
    assert shifted_back.json()["moved_count"] == 2
    assert shifted_back.json()["review_revision"] == 2
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert [item["group_id"] for item in detail["results"]] == [2, 2, 2]

    boundary = client.patch(
        f"/api/runs/{RUN_ID}/groups",
        json={"indexes": [0, 1, 2], "direction": "previous", "base_revision": 2},
        headers=headers,
    )
    assert boundary.status_code == 200
    assert boundary.json()["moved_count"] == 0
    assert boundary.json()["review_revision"] == 2


def test_exclude_and_restore_are_reversible_without_touching_files(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, first = _client(tmp_path, monkeypatch)
    second = first.parent / "DSC0002.ARW"
    _append_result(data_dir, second, 2)
    xmp = first.with_suffix(".xmp")
    xmp.write_text("existing xmp", encoding="utf-8")
    raw_before = first.read_bytes()
    raw_mtime = first.stat().st_mtime_ns
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}

    removed = client.patch(
        f"/api/runs/{RUN_ID}/excluded",
        json={"indexes": [0], "excluded": True, "base_revision": 0},
        headers=headers,
    )
    assert removed.status_code == 200
    assert removed.json()["active_image_count"] == 1
    assert removed.json()["excluded_count"] == 1
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["results"][0]["excluded"] is True
    assert detail["needs_rescore"] is True
    assert detail["xmp_ready"] is False
    assert first.read_bytes() == raw_before
    assert first.stat().st_mtime_ns == raw_mtime
    assert xmp.read_text(encoding="utf-8") == "existing xmp"
    assert (
        client.patch(
            f"/api/runs/{RUN_ID}/items/0",
            json={"rating": 5, "base_revision": 1},
            headers=headers,
        ).status_code
        == 409
    )

    all_removed = client.patch(
        f"/api/runs/{RUN_ID}/excluded",
        json={"indexes": [1], "excluded": True, "base_revision": 1},
        headers=headers,
    )
    assert all_removed.status_code == 422
    assert read_json(data_dir / "runs" / RUN_ID / "review.json")["revision"] == 1

    restored = client.patch(
        f"/api/runs/{RUN_ID}/excluded",
        json={"indexes": [0], "excluded": False, "base_revision": 1},
        headers=headers,
    )
    assert restored.status_code == 200
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["excluded_count"] == 0
    assert detail["needs_rescore"] is False
    assert first.read_bytes() == raw_before
    assert xmp.read_text(encoding="utf-8") == "existing xmp"


def test_grouped_run_scores_from_materialized_manual_groups(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    results_path = data_dir / "runs" / RUN_ID / "results.json"
    payload = read_json(results_path)
    payload.update(
        workflow_state="grouped", scoring_mode="fast", candidate_count=0, strong_count=0
    )
    payload["results"][0]["rating"] = 0
    write_json(results_path, payload)
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    assert (
        client.patch(
            f"/api/runs/{RUN_ID}/items/0",
            json={"rating": 4, "base_revision": 0},
            headers=headers,
        ).status_code
        == 409
    )
    moved = client.patch(
        f"/api/runs/{RUN_ID}/groups/0",
        json={"group_id": 9, "base_revision": 0},
        headers=headers,
    )
    assert moved.status_code == 200

    captured: dict = {}

    def fake_start(kind: str, args: list[str], context: dict) -> dict:
        captured.update(kind=kind, args=args, context=context)
        return {"id": "test", "status": "queued", "title": context["title"]}

    monkeypatch.setattr(client.app.state.jobs, "start", fake_start)
    stale_score = client.post(
        f"/api/runs/{RUN_ID}/score",
        json={"base_revision": 0},
        headers=headers,
    )
    assert stale_score.status_code == 409
    response = client.post(
        f"/api/runs/{RUN_ID}/score",
        json={"base_revision": 1},
        headers=headers,
    )
    assert response.status_code == 200
    assert captured["kind"] == "score"
    groups_path = Path(captured["args"][captured["args"].index("--groups-from") + 1])
    assert groups_path.parent == results_path.parent
    snapshot = read_json(groups_path)
    assert snapshot["results"][0]["group_id"] == 9
    assert "--source-run-id" in captured["args"]


def test_projects_group_versions_by_input_directory(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    second_run_id = "20260831-130000-654321"
    payload = read_json(data_dir / "runs" / RUN_ID / "results.json")
    payload["run_id"] = second_run_id
    payload["scoring_mode"] = "deep"
    write_json(data_dir / "runs" / second_run_id / "results.json", payload)

    projects = client.get("/api/projects").json()
    assert len(projects) == 1
    assert projects[0]["version_count"] == 2
    assert projects[0]["latest_run_id"] == second_run_id

    detail = client.get(f"/api/projects/{projects[0]['project_id']}").json()
    assert [version["run_id"] for version in detail["versions"]] == [
        second_run_id,
        RUN_ID,
    ]


def test_same_folder_name_at_different_paths_creates_distinct_projects(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    first = tmp_path / "first" / "same-name"
    second = tmp_path / "second" / "same-name"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    base = read_json(data_dir / "runs" / RUN_ID / "results.json")
    for run_id, root in (
        ("20260831-130000-111111", first),
        ("20260831-140000-222222", second),
    ):
        payload = {**base, "run_id": run_id, "input_root": str(root)}
        write_json(data_dir / "runs" / run_id / "results.json", payload)

    same_name = [
        project
        for project in client.get("/api/projects").json()
        if project["name"] == "same-name"
    ]
    assert len(same_name) == 2
    assert same_name[0]["project_id"] != same_name[1]["project_id"]


def test_project_attaches_dry_run_and_commit_to_source_version(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, raw = _client(tmp_path, monkeypatch)
    run_dir = data_dir / "runs" / RUN_ID
    xmp = raw.with_suffix(".xmp")
    xmp.write_text("created by test", encoding="utf-8")
    write_json(
        run_dir / "xmp-dry-run.json",
        {
            "created_at": "2026-08-31T04:44:38+00:00",
            "source_results": str(run_dir / "results.json"),
            "commit": False,
            "min_rating": 3,
            "limit": None,
            "records": [
                {"status": "planned"},
                {"status": "skipped_existing_xmp"},
            ],
            "planned_count": 1,
            "skipped_count": 1,
        },
    )
    write_json(
        run_dir / "xmp-commit-manifest-test.json",
        {
            "created_at": "2026-08-31T04:44:40+00:00",
            "source_results": str(run_dir / "results.json"),
            "commit": True,
            "records": [{"status": "created", "xmp_path": str(xmp)}],
            "skipped_count": 0,
        },
    )

    project = client.get("/api/projects").json()[0]
    assert project["xmp_count"] == 1
    detail = client.get(f"/api/projects/{project['project_id']}").json()
    version = detail["versions"][0]
    assert version["dry_run"]["kind"] == "dry_run"
    assert version["dry_run"]["planned_count"] == 1
    assert version["dry_run"]["skipped_count"] == 1
    assert len(version["transactions"]) == 1
    assert version["transactions"][0]["kind"] == "commit"
    assert version["transactions"][0]["remaining_count"] == 1
    assert detail["orphan_transactions"] == []


def test_project_keeps_unmatched_commit_as_orphan(tmp_path: Path, monkeypatch) -> None:
    client, data_dir, raw = _client(tmp_path, monkeypatch)
    run_dir = data_dir / "runs" / RUN_ID
    write_json(
        run_dir / "xmp-commit-manifest-orphan.json",
        {
            "created_at": "2026-08-31T04:44:40+00:00",
            "source_results": str(
                data_dir / "runs" / "19990101-000000" / "results.json"
            ),
            "commit": True,
            "records": [
                {"status": "created", "xmp_path": str(raw.with_suffix(".xmp"))}
            ],
            "skipped_count": 0,
        },
    )
    project = client.get("/api/projects").json()[0]
    detail = client.get(f"/api/projects/{project['project_id']}").json()
    assert detail["versions"][0]["transactions"] == []
    assert len(detail["orphan_transactions"]) == 1


def test_unknown_project_returns_404(tmp_path: Path, monkeypatch) -> None:
    client, _data_dir, _raw = _client(tmp_path, monkeypatch)
    assert client.get("/api/projects/not-a-project").status_code == 404


def test_delete_project_moves_versions_to_recycle_without_touching_photos(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, raw = _client(tmp_path, monkeypatch)
    second_run_id = "20260831-130000-654321"
    payload = read_json(data_dir / "runs" / RUN_ID / "results.json")
    payload["run_id"] = second_run_id
    write_json(data_dir / "runs" / second_run_id / "results.json", payload)
    xmp = raw.with_suffix(".xmp")
    xmp.write_text("keep this xmp", encoding="utf-8")
    write_json(
        data_dir / "runs" / RUN_ID / "xmp-commit-manifest-test.json",
        {
            "created_at": "2026-08-31T04:44:40+00:00",
            "source_results": str(data_dir / "runs" / RUN_ID / "results.json"),
            "commit": True,
            "records": [{"status": "created", "xmp_path": str(xmp)}],
            "skipped_count": 0,
        },
    )
    project = client.get("/api/projects").json()[0]
    endpoint = f"/api/projects/{project['project_id']}"

    assert (
        client.request(
            "DELETE", endpoint, json={"confirmation": "删除工程"}
        ).status_code
        == 403
    )
    token = client.get("/api/bootstrap").json()["token"]
    headers = {"X-Photo-AI-Token": token}
    assert (
        client.request(
            "DELETE", endpoint, json={"confirmation": "yes"}, headers=headers
        ).status_code
        == 422
    )
    response = client.request(
        "DELETE", endpoint, json={"confirmation": "删除工程"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["version_count"] == 2
    assert response.json()["raw_files_deleted"] == 0
    assert response.json()["xmp_files_deleted"] == 0
    assert raw.is_file()
    assert xmp.read_text(encoding="utf-8") == "keep this xmp"
    assert client.get("/api/projects").json() == []
    assert not (data_dir / "runs" / RUN_ID).exists()
    assert not (data_dir / "runs" / second_run_id).exists()
    recycle_dirs = list((data_dir / "trash" / "projects").iterdir())
    assert len(recycle_dirs) == 1
    assert (recycle_dirs[0] / "project.json").is_file()
    assert (recycle_dirs[0] / RUN_ID / "results.json").is_file()
    assert (recycle_dirs[0] / second_run_id / "results.json").is_file()


def test_delete_project_is_blocked_while_a_job_is_active(
    tmp_path: Path, monkeypatch
) -> None:
    client, data_dir, _raw = _client(tmp_path, monkeypatch)
    project = client.get("/api/projects").json()[0]
    token = client.get("/api/bootstrap").json()["token"]
    monkeypatch.setattr(client.app.state.jobs, "active", lambda: {"id": "active-job"})
    response = client.request(
        "DELETE",
        f"/api/projects/{project['project_id']}",
        json={"confirmation": "删除工程"},
        headers={"X-Photo-AI-Token": token},
    )
    assert response.status_code == 409
    assert (data_dir / "runs" / RUN_ID / "results.json").is_file()
