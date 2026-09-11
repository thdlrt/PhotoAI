from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from landscape_culler.content_root import initialize_content_root
from landscape_culler.core_worker import PROTOCOL as WORKER_PROTOCOL
from landscape_culler.core_worker import (
    WorkerProtocolError,
    _emit,
    _execute,
    _validate_managed_job_path,
)
from landscape_culler.service import PROTOCOL as SERVICE_PROTOCOL
from landscape_culler.service import ServiceApp, _initialize_managed_content_root

FORBIDDEN_STARTUP_MODULES = (
    "torch",
    "numpy",
    "PIL",
    "transformers",
    "cv2",
    "pyiqa",
    "PyOpenColorIO",
    "OpenColorIO",
    "pyvips",
    "rawpy",
    "landscape_culler.smart_crop",
)


def test_core_worker_installer_self_test_imports_lazy_modules() -> None:
    result = _execute({"command": "ai-runtime-self-test"})

    assert result == {"ai_runtime": True, "model_resources": True}


def test_core_worker_self_test_rejects_missing_cleanup_module(monkeypatch) -> None:
    from landscape_culler import core_worker

    events = []
    original_import = core_worker.importlib.import_module

    def import_with_missing_cleanup(name, *args, **kwargs):
        if name == "landscape_culler.xmp_cleanup":
            raise ModuleNotFoundError(f"No module named '{name}'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(core_worker.importlib, "import_module", import_with_missing_cleanup)
    monkeypatch.setattr(core_worker, "_emit", lambda *args, **kwargs: events.append(kwargs))
    assert core_worker._self_test() == 1
    assert events[0]["status"] == "failed"
    assert "landscape_culler.xmp_cleanup" in events[0]["command_import_errors"]


def test_core_worker_protocol_is_ascii_safe(capsys: pytest.CaptureFixture[str]) -> None:
    _emit("failed", "encoding-probe", error="安装失败")

    raw = capsys.readouterr().out.strip()
    raw.encode("ascii")
    assert "安装失败" not in raw
    assert json.loads(raw)["error"] == "安装失败"


def test_managed_service_ignores_inherited_fixed_ollama_for_model_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from landscape_culler import web as web_module
    from landscape_culler.web import create_app

    original_environment = os.environ.copy()
    try:
        monkeypatch.setenv("PHOTO_AI_OLLAMA_ENDPOINT", "http://127.0.0.1:11435")
        monkeypatch.setenv(
            "PHOTO_AI_OLLAMA", str(tmp_path / "external" / "ollama.exe")
        )
        monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11435")
        layout = _initialize_managed_content_root(tmp_path / "content")

        assert "PHOTO_AI_OLLAMA_ENDPOINT" not in os.environ
        assert "PHOTO_AI_OLLAMA" not in os.environ
        assert "OLLAMA_HOST" not in os.environ

        def reject_external_request(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("managed Service contacted inherited Ollama endpoint")

        monkeypatch.setattr(
            web_module.urllib.request, "urlopen", reject_external_request
        )
        app = create_app(
            data_dir=layout.state,
            project_root=Path(__file__).resolve().parents[1],
            content_root=layout.root,
        )
        client = TestClient(app)
        token = client.get("/api/bootstrap").json()["token"]
        deleted = client.delete(
            "/api/model-resources/qwen3-vl-4b",
            headers={"X-Photo-AI-Token": token},
        )

        assert deleted.status_code == 200
    finally:
        os.environ.clear()
        os.environ.update(original_environment)


def test_managed_worker_renders_lut_without_service_image_dependencies(
    tmp_path: Path,
) -> None:
    from PIL import Image

    from landscape_culler.creative_lut import CreativeLutEngine
    from landscape_culler.lut_export_worker import LUT_EXPORT_PROTOCOL
    from landscape_culler.util import full_fingerprint, write_json

    layout = initialize_content_root(
        tmp_path / "content",
        install_dir=Path(sys.executable).resolve().parent,
        apply_environment=False,
        persist_registry=False,
    )
    lut_source = tmp_path / "invert.cube"
    lut_source.write_text(
        "LUT_1D_SIZE 2\n1.0 1.0 1.0\n0.0 0.0 0.0\n",
        encoding="utf-8",
    )
    descriptor = CreativeLutEngine.for_content_root(layout).import_lut(lut_source)
    output_root = tmp_path / "photos" / "成片"
    output_root.mkdir(parents=True)
    jpeg = output_root / "photo.jpg"
    Image.new("RGB", (32, 20), (24, 96, 208)).save(jpeg, "JPEG", quality=95)
    before = jpeg.read_bytes()

    task = layout.state / "exports" / "attempt" / "lut-export-task.json"
    write_json(
        task,
        {
            "protocol": LUT_EXPORT_PROTOCOL,
            "export_spec_id": "export-" + "a" * 32,
            "attempt_id": "attempt-1",
            "output_root": str(output_root),
            "jpeg_quality": 90,
            "items": [
                {
                    "item_id": "photo-1",
                    "jpeg_path": str(jpeg),
                    "lut_id": descriptor.lut_id,
                    "lut_hash": descriptor.lut_hash,
                    "strength": 100,
                    "input_fingerprint": full_fingerprint(jpeg),
                }
            ],
        },
    )
    jobs = layout.state / "web" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    spec = jobs / "lut.spec.json"
    result = jobs / "lut.worker-result.json"
    write_json(
        spec,
        {
            "protocol": WORKER_PROTOCOL,
            "job_id": "lut-export-1",
            "command": "cli",
            "argv": ["render-export-luts", "--task", str(task)],
            "result_path": str(result),
        },
    )
    env = os.environ.copy()
    env.update(
        PHOTO_AI_CONTENT_ROOT=str(layout.root),
        PHOTO_AI_DATA_DIR=str(layout.state),
    )
    completed = subprocess.run(
        [sys.executable, "-m", "landscape_culler.core_worker", "--job-spec", str(spec)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        timeout=60,
    )

    events = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [event["event"] for event in events] == ["started", "completed"]
    payload = json.loads(result.read_text(encoding="utf-8"))
    rendered = payload["result"]
    assert payload["status"] == "completed"
    assert rendered["protocol"] == LUT_EXPORT_PROTOCOL
    assert rendered["status"] == "completed"
    assert rendered["items"][0]["succeeded"] is True
    assert jpeg.read_bytes() != before
    assert not list(output_root.glob(".*.lut.tmp"))


def _probe_import(module_name: str) -> dict[str, Any]:
    script = """
import importlib
import json
import sys
module = importlib.import_module(sys.argv[1])
forbidden = tuple(json.loads(sys.argv[2]))
loaded = sorted(
    name for name in sys.modules
    if any(name == item or name.startswith(item + '.') for item in forbidden)
)
print(json.dumps({
    'loaded': loaded,
    'web_loaded': 'landscape_culler.web' in sys.modules,
    'cli_loaded': 'landscape_culler.cli' in sys.modules,
    'scoring_loaded': 'landscape_culler.scoring' in sys.modules,
    'style_worker_loaded': 'landscape_culler.style_worker' in sys.modules,
}))
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            module_name,
            json.dumps(FORBIDDEN_STARTUP_MODULES),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        timeout=30,
    )
    return json.loads(completed.stdout)


def test_service_core_worker_and_cli_imports_stay_lightweight() -> None:
    service = _probe_import("landscape_culler.service")
    worker = _probe_import("landscape_culler.core_worker")
    cli = _probe_import("landscape_culler.cli")
    web = _probe_import("landscape_culler.web")

    assert service["loaded"] == []
    assert service["web_loaded"] is False
    assert worker["loaded"] == []
    assert worker["cli_loaded"] is False
    assert cli["loaded"] == []
    assert cli["scoring_loaded"] is False
    assert cli["style_worker_loaded"] is False
    assert web["loaded"] == []


def test_service_and_core_worker_self_tests_are_lightweight() -> None:
    for module, protocol in (
        ("landscape_culler.service", SERVICE_PROTOCOL),
        ("landscape_culler.core_worker", WORKER_PROTOCOL),
    ):
        completed = subprocess.run(
            [sys.executable, "-m", module, "--self-test"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=Path(__file__).resolve().parents[1],
            env=os.environ.copy(),
            timeout=30,
        )
        payload = json.loads(completed.stdout.strip())
        assert payload["protocol"] == protocol
        assert payload["status"] == "passed"
        assert payload.get("unexpected_imports", []) == []


def test_service_exchanges_bootstrap_token_once_for_http_only_session() -> None:
    async def inner(scope, _receive, send):
        body = json.dumps({"path": scope["path"]}, separators=(",", ":")).encode(
            "utf-8"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    stopping: list[bool] = []
    gated = ServiceApp(inner, bootstrap_token="one-time-secret")
    gated.shutdown = lambda: stopping.append(True)
    client = TestClient(gated)

    assert client.get("/health").json()["protocol"] == SERVICE_PROTOCOL
    assert client.get("/").status_code == 401
    assert client.get("/static/app.js").status_code == 401
    assert client.get("/api/jobs").status_code == 401

    exchanged = client.get(
        "/?token=one-time-secret&install_profile=8gb", follow_redirects=False
    )
    assert exchanged.status_code == 303
    assert exchanged.headers["location"] == "/?install_profile=8gb"
    assert "HttpOnly" in exchanged.headers["set-cookie"]
    assert "SameSite=Strict" in exchanged.headers["set-cookie"]
    assert "token=" not in exchanged.headers["location"]

    assert client.get("/").json() == {"path": "/"}
    assert client.get("/static/app.js").json() == {"path": "/static/app.js"}
    assert client.get("/api/jobs").json() == {"path": "/api/jobs"}

    replay = TestClient(gated).get(
        "/api/jobs", headers={"Authorization": "Bearer one-time-secret"}
    )
    assert replay.status_code == 401
    stopped = client.post("/api/service/shutdown")
    assert stopped.json() == {"protocol": SERVICE_PROTOCOL, "status": "stopping"}
    assert stopping == [True]

    bearer_gate = ServiceApp(inner, bootstrap_token="bearer-once")
    bearer_client = TestClient(bearer_gate)
    bearer_exchange = bearer_client.get(
        "/api/jobs", headers={"Authorization": "Bearer bearer-once"}
    )
    assert bearer_exchange.status_code == 200
    assert "HttpOnly" in bearer_exchange.headers["set-cookie"]
    assert bearer_client.get("/api/jobs").status_code == 200

    control_gate = ServiceApp(
        inner,
        bootstrap_token="browser-bootstrap",
        control_token="shutdown-control",
    )
    control_stopping: list[bool] = []
    control_gate.shutdown = lambda: control_stopping.append(True)
    control_client = TestClient(control_gate)
    assert (
        control_client.post(
            "/api/service/shutdown",
            headers={"Authorization": "Bearer browser-bootstrap"},
        ).status_code
        == 401
    )
    assert (
        control_client.get(
            "/api/jobs",
            headers={"Authorization": "Bearer shutdown-control"},
        ).status_code
        == 401
    )
    assert (
        control_client.post(
            "/api/service/shutdown",
            headers={"Authorization": "Bearer shutdown-control"},
        ).status_code
        == 200
    )
    assert control_stopping == [True]


def test_service_bootstrap_storage_rejects_mutations_before_route_dispatch(
    tmp_path: Path,
) -> None:
    from landscape_culler.web import create_app

    project_root = tmp_path / "application"
    project_root.mkdir()
    inner = create_app(
        data_dir=tmp_path / "bootstrap" / "state",
        project_root=project_root,
        bootstrap_storage=True,
    )
    gated = ServiceApp(inner, bootstrap_token="bootstrap-storage-once")
    with TestClient(gated) as client:
        exchanged = client.get(
            "/?token=bootstrap-storage-once", follow_redirects=False
        )
        assert exchanged.status_code == 303
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        payload = bootstrap.json()
        assert payload["system"]["content_root_configured"] is False

        blocked = client.post(
            "/api/model-resources/configure",
            headers={"X-Photo-AI-Token": payload["token"]},
            json={"profile_id": "16gb"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "content_root_required"
        assert client.get("/api/jobs").json() == []


def test_core_worker_ping_uses_ndjson_and_atomic_result(tmp_path: Path) -> None:
    spec = tmp_path / "job.json"
    result = tmp_path / "result.json"
    spec.write_text(
        json.dumps(
            {
                "protocol": WORKER_PROTOCOL,
                "job_id": "ping-1",
                "command": "ping",
                "payload": {"value": 7},
                "result_path": str(result),
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "-m", "landscape_culler.core_worker", "--job-spec", str(spec)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        timeout=30,
    )

    events = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [event["event"] for event in events] == ["started", "completed"]
    assert all(event["protocol"] == WORKER_PROTOCOL for event in events)
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload == {
        "protocol": WORKER_PROTOCOL,
        "job_id": "ping-1",
        "status": "completed",
        "result": {"pong": True, "payload": {"value": 7}},
    }
    assert not list(tmp_path.glob(".*.tmp"))


def test_core_worker_managed_paths_cannot_leave_content_jobs_root(
    tmp_path: Path, monkeypatch
) -> None:
    content = tmp_path / "content"
    jobs = content / "state" / "web" / "jobs"
    jobs.mkdir(parents=True)
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(content))

    accepted = (jobs / "task.spec.json").resolve()
    assert _validate_managed_job_path(accepted, "spec") == accepted
    with pytest.raises(WorkerProtocolError, match="state/web/jobs"):
        _validate_managed_job_path((content / "projects" / "escape.json").resolve(), "spec")


def test_core_worker_validates_only_an_owned_content_root(tmp_path: Path) -> None:
    root = tmp_path / "owned-content"
    initialize_content_root(
        root,
        install_dir=Path(sys.executable).resolve().parent,
        apply_environment=False,
        persist_registry=False,
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "landscape_culler.core_worker",
            "--validate-owned-content-root",
            str(root),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        timeout=30,
    )
    event = json.loads(completed.stdout)
    assert event["event"] == "content-root-validation"
    assert event["status"] == "passed"
    assert Path(event["root"]) == root.resolve()

    (root / "marker.json").write_text("{}", encoding="utf-8")
    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "landscape_culler.core_worker",
            "--validate-owned-content-root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        timeout=30,
    )
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["status"] == "failed"


def test_core_worker_persists_failed_job_without_partial_result(tmp_path: Path) -> None:
    spec = tmp_path / "bad-job.json"
    result = tmp_path / "bad-result.json"
    spec.write_text(
        json.dumps(
            {
                "protocol": WORKER_PROTOCOL,
                "job_id": "bad-1",
                "command": "not-supported",
                "result_path": str(result),
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "-m", "landscape_culler.core_worker", "--job-spec", str(spec)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        timeout=30,
    )

    assert completed.returncode == 1
    events = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [event["event"] for event in events] == ["started", "failed"]
    failed = json.loads(result.read_text(encoding="utf-8"))
    assert failed["protocol"] == WORKER_PROTOCOL
    assert failed["job_id"] == "bad-1"
    assert failed["status"] == "failed"
    assert "不支持命令" in failed["error"]
    assert not list(tmp_path.glob(".*.tmp"))


def test_core_worker_converts_cli_parse_exit_to_atomic_failure(tmp_path: Path) -> None:
    spec = tmp_path / "invalid-cli-job.json"
    result = tmp_path / "invalid-cli-result.json"
    spec.write_text(
        json.dumps(
            {
                "protocol": WORKER_PROTOCOL,
                "job_id": "invalid-cli-1",
                "command": "cli",
                "argv": ["not-a-command"],
                "result_path": str(result),
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "-m", "landscape_culler.core_worker", "--job-spec", str(spec)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        timeout=30,
    )

    assert completed.returncode == 1
    assert [json.loads(line)["event"] for line in completed.stdout.splitlines()] == [
        "started",
        "failed",
    ]
    failed = json.loads(result.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["job_id"] == "invalid-cli-1"
    assert not list(tmp_path.glob(".*.tmp"))


def test_core_worker_preserves_installation_error_behind_exit_code(tmp_path, monkeypatch):
    from landscape_culler import core_worker

    spec = tmp_path / "failure.spec.json"
    result = tmp_path / "failure.result.json"
    spec.write_text(json.dumps({
        "protocol": WORKER_PROTOCOL, "job_id": "install-failed",
        "command": "cli", "argv": ["model-configure", "--profile", "16gb"],
        "result_path": str(result),
    }), encoding="utf-8")
    monkeypatch.delenv("PHOTO_AI_CONTENT_ROOT", raising=False)
    # pytest owns stdout; avoid changing its encoding from inside main().
    monkeypatch.setattr(core_worker, "_emit", lambda *args, **kwargs: None)

    def fail(_spec):
        try:
            raise RuntimeError("Qwen3-VL 8B：模型清单缺失，请重试安装")
        except RuntimeError as exc:
            raise SystemExit(1) from exc

    monkeypatch.setattr(core_worker, "_execute", fail)
    assert core_worker.main(["--job-spec", str(spec)]) == 1
    failure = json.loads(result.read_text(encoding="utf-8"))
    assert failure["error"] == "Qwen3-VL 8B：模型清单缺失，请重试安装"


def test_core_worker_executes_managed_develop_plan_job_spec(tmp_path: Path) -> None:
    content = tmp_path / "content"
    layout = initialize_content_root(
        content,
        install_dir=Path(sys.executable).resolve().parent,
        apply_environment=False,
        persist_registry=False,
    )
    run_dir = layout.projects / "run-1"
    jobs_dir = layout.state / "web" / "jobs"
    preview = layout.cache / "previews" / "preview.jpg"
    run_dir.mkdir(parents=True)
    jobs_dir.mkdir(parents=True)
    preview.parent.mkdir(parents=True)
    Image.new("RGB", (320, 200), "#708a9c").save(preview, "JPEG")
    input_path = jobs_dir / "develop-input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "review_revision": 2,
                "payload": {
                    "run_id": "run-1",
                    "results": [
                        {
                            "path": str(tmp_path / "DSC0001.ARW"),
                            "preview": str(preview),
                            "rating": 3,
                            "group_id": 1,
                            "technical": {},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    result = jobs_dir / "develop-result.json"
    spec = jobs_dir / "develop.spec.json"
    spec.write_text(
        json.dumps(
            {
                "protocol": WORKER_PROTOCOL,
                "job_id": "develop-1",
                "command": "cli",
                "argv": [
                    "develop-plan",
                    "--input",
                    str(input_path),
                    "--run-dir",
                    str(run_dir),
                    "--review-revision",
                    "2",
                ],
                "result_path": str(result),
            }
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PHOTO_AI_CONTENT_ROOT": str(layout.root),
        "PHOTO_AI_PROJECTS_DIR": str(layout.projects),
        "PHOTO_AI_SMART_CROP_MODE": "heuristic",
        "PHOTO_AI_PROGRESS": "json",
    }

    completed = subprocess.run(
        [sys.executable, "-m", "landscape_culler.core_worker", "--job-spec", str(spec)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        timeout=60,
    )

    assert [json.loads(line)["event"] for line in completed.stdout.splitlines()] == [
        "started",
        "completed",
    ]
    worker_result = json.loads(result.read_text(encoding="utf-8"))
    assert worker_result["status"] == "completed"
    assert worker_result["result"]["plan_id"].startswith("develop-")
    assert (run_dir / "develop.json").is_file()
    assert json.loads((run_dir / "develop-progress.json").read_text(encoding="utf-8"))[
        "status"
    ] == "completed"


def test_service_process_emits_one_handshake_and_protects_web_surface(
    tmp_path: Path,
) -> None:
    content_root = tmp_path / "service-content"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "landscape_culler.service",
            "--port",
            "0",
            "--content-root",
            str(content_root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert process.stdout is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            handshake_line = (
                pool.submit(process.stdout.readline).result(timeout=60).strip()
            )
        prefix = f"{SERVICE_PROTOCOL} "
        assert handshake_line.startswith(prefix)
        handshake = json.loads(handshake_line[len(prefix) :])
        assert int(handshake["port"]) > 0
        assert handshake["token"]
        assert handshake["control_token"]
        assert handshake["control_token"] != handshake["token"]
        assert handshake["origin"] == f"http://127.0.0.1:{handshake['port']}"

        origin = handshake["origin"]
        deadline = time.monotonic() + 30
        while True:
            try:
                health = httpx.get(f"{origin}/health", timeout=2)
                if health.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("PhotoAI.Service health endpoint did not start")
            time.sleep(0.05)
        assert health.json()["protocol"] == SERVICE_PROTOCOL

        with httpx.Client(
            base_url=origin, follow_redirects=False, timeout=30
        ) as client:
            assert client.get("/api/jobs").status_code == 401
            assert client.get("/static/app.js").status_code == 401
            exchange = client.get("/", params={"token": handshake["token"]})
            assert exchange.status_code == 303
            assert exchange.headers["location"] == "/"
            assert client.get("/").status_code == 200
            assert client.get("/static/app.js").status_code == 200
            assert client.get("/api/jobs").status_code == 200
            assert (
                httpx.post(
                    f"{origin}/api/service/shutdown",
                    headers={
                        "Authorization": f"Bearer {handshake['control_token']}"
                    },
                    timeout=30,
                ).status_code
                == 200
            )

        process.wait(timeout=30)
        assert process.returncode == 0
        assert process.stdout.read() == ""
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
