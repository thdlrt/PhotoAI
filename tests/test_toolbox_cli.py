from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from PIL import Image

from landscape_culler import toolbox_cli as cli
from landscape_culler.agent_connection import publish_connection
from landscape_culler.service import ServiceApp
from landscape_culler.util import read_json, write_json
from test_web import RUN_ID, _client


def invoke(monkeypatch, args):
    output = io.StringIO()
    errors = io.StringIO()
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setattr(cli.sys, "stderr", errors)
    code = cli.main(args)
    text = output.getvalue()
    return code, json.loads(text), text, errors.getvalue()


@pytest.fixture
def live(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    wrapped = ServiceApp(client.app)
    server = uvicorn.Server(uvicorn.Config(wrapped, host="127.0.0.1", port=0, log_config=None, access_log=False))
    sock = server.config.bind_socket()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline: time.sleep(0.01)
    assert server.started
    connection = publish_connection(data / "cli-session.json", origin=origin, pid=os.getpid(),
        instance_id=wrapped.instance_id, token=wrapped.cli_token, content_root=tmp_path, data_dir=data)
    source = tmp_path / "有空格的 素材"
    source.mkdir()
    Image.new("RGB", (64, 40), "blue").save(source / "第一张.jpg")
    try:
        yield SimpleNamespace(data=data, root=tmp_path, raw=raw, source=source, connection=connection,
                              wrapped=wrapped, origin=origin, inner=client.app,
                              prefix=["--connection-file", str(connection)])
    finally:
        server.should_exit = True
        worker.join(5)
        sock.close()
        assert not worker.is_alive()


def test_capabilities_reads_protected_connection_without_printing_secrets(live, monkeypatch):
    code, answer, text, errors = invoke(monkeypatch, [*live.prefix, "capabilities"])
    assert code == 0 and answer["ok"] and answer["data"]["transport"] == "http"
    assert live.wrapped.cli_token not in text + errors
    assert live.wrapped.bootstrap_token not in text + errors
    assert live.wrapped.control_token not in text + errors
    assert "X-Photo-AI-Token" not in text
    # Auto-discovery from data-dir resolves to the same running service.
    code, answer, *_ = invoke(monkeypatch, ["--data-dir", str(live.data), "review", "list"])
    assert code == 0 and answer["data"] == {"workspaces": []}


def create_review(live, monkeypatch):
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "review", "create", "--input-root", str(live.source), "--name", "中文 审片"])
    assert code == 0, answer
    return answer["data"]


def test_review_roundtrip_unicode_paths_revisions_and_no_implicit_acceptance(live, monkeypatch):
    workspace = create_review(live, monkeypatch)
    wid, asset = workspace["workspace_id"], workspace["items"][0]["id"]
    code, current, *_ = invoke(monkeypatch, [*live.prefix, "review", "get", wid])
    assert code == 0 and current["data"]["user_revision"] == 0
    assert current["data"]["items"][0]["user"]["status"] == "unreviewed"
    code, suggestion, *_ = invoke(monkeypatch, [*live.prefix, "review", "suggest", wid, "--revision", "0"])
    assert code == 0 and suggestion["data"]["items"][0]["user"]["status"] == "unreviewed"
    code, decision, *_ = invoke(monkeypatch, [*live.prefix, "review", "decide", wid, asset,
        "--revision", "0", "--status", "keep", "--notes", "保留这段回忆", "--memory", "3"])
    assert code == 0 and decision["data"]["items"][0]["user"]["notes"] == "保留这段回忆"
    code, conflict, *_ = invoke(monkeypatch, [*live.prefix, "review", "decide", wid, asset,
                                           "--revision", "0", "--status", "reject"])
    assert code == 3 and conflict["error"]["code"] == "conflict"
    code, latest, *_ = invoke(monkeypatch, [*live.prefix, "review", "get", wid])
    assert code == 0 and latest["data"]["user_revision"] == 1
    assert latest["data"]["items"][0]["assistant"] is not None


def test_watch_user_revision_ignores_ai_only_changes_and_changes_is_incremental(live, monkeypatch):
    workspace = create_review(live, monkeypatch)
    wid, asset = workspace["workspace_id"], workspace["items"][0]["id"]
    baseline = live.root / "baseline.json"
    write_json(baseline, workspace)
    invoke(monkeypatch, [*live.prefix, "review", "suggest", wid, "--revision", "0"])
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "review", "watch", "--workspace", wid,
        "--after-user-revision", "0", "--timeout", "0.03", "--interval", "0.01"])
    assert code == 4 and answer["error"]["details"]["user_revision"] == 0
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "review", "watch", "--workspace", wid,
        "--after-revision", "0", "--timeout", "0.03"])
    assert code == 0 and answer["data"]["revision"] == 1
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "review", "changes", wid, "--baseline", str(baseline)])
    assert code == 0 and answer["data"]["changed"] is False
    invoke(monkeypatch, [*live.prefix, "review", "decide", wid, asset, "--revision", "0", "--status", "keep"])
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "review", "changes", wid, "--baseline", str(baseline)])
    assert code == 0 and answer["data"]["changed"] and len(answer["data"]["items"]) == 1
    assert answer["data"]["user_revision"] == 1


def test_export_output_is_json_envelope_and_never_overwrites(live, monkeypatch):
    workspace = create_review(live, monkeypatch)
    output = live.root / "选择 清单.csv"
    command = [*live.prefix, "review", "export", workspace["workspace_id"], "--format", "csv", "--all", "--output", str(output)]
    code, answer, *_ = invoke(monkeypatch, command)
    assert code == 0 and answer["data"]["path"] == str(output)
    original = output.read_bytes()
    code, answer, *_ = invoke(monkeypatch, command)
    assert code == 3 and answer["error"]["code"] == "output_exists"
    assert output.read_bytes() == original


def test_selection_export_and_copy_use_existing_service_and_keep_legacy_data(live, monkeypatch):
    folder = live.data / "runs" / RUN_ID
    write_json(folder / "review.json", {"revision": 2, "ratings": {"0": 5}})
    write_json(folder / "develop.json", {"schema_version": 1, "items": [], "custom": "retain"})
    before = {path: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "selection", "export", RUN_ID])
    assert code == 0 and answer["data"]["items"][0]["rating"] == 5
    assert "develop" not in answer["data"]["items"][0]
    target = live.root / "复制输出 新目录"
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "selection", "copy", RUN_ID, "--revision", "2",
        "--output-dir", str(target), "--wait", "--wait-timeout", "5", "--interval", "0.01"])
    assert code == 0 and answer["data"]["status"] == "completed"
    assert (target / live.raw.name).read_bytes() == live.raw.read_bytes()
    assert all(path.read_bytes() == value for path, value in before.items())


def test_storyboard_rules_import_context_and_approval_versions(live, monkeypatch):
    workspace = create_review(live, monkeypatch)
    wid, asset = workspace["workspace_id"], workspace["items"][0]["id"]
    invoke(monkeypatch, [*live.prefix, "review", "decide", wid, asset, "--revision", "0", "--status", "keep"])
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "storyboard", "context", wid])
    assert code == 0 and "context" in answer["data"]
    code, answer, *_ = invoke(monkeypatch, [*live.prefix, "storyboard", "create", wid, "--workspace-revision", "1", "--title", "测试草案"])
    assert code == 0, answer
    proposal = answer["data"]["proposal"]
    pid = proposal["proposal_id"]
    assert proposal["status"] == "draft"
    body = live.root / "approval.json"
    write_json(body, {"status": "approved"})
    code, approved, *_ = invoke(monkeypatch, [*live.prefix, "storyboard", "update", wid, pid,
        "--revision", str(proposal["revision"]), "--workspace-revision", "1", "--input-json", str(body)])
    assert code == 0 and approved["data"]["proposal"]["status"] == "approved", approved
    code, exported, *_ = invoke(monkeypatch, [*live.prefix, "storyboard", "export", wid, pid])
    assert code == 0, exported
    exported_file = live.root / "exported-proposal.json"
    write_json(exported_file, exported)
    code, imported, *_ = invoke(monkeypatch, [*live.prefix, "storyboard", "import", wid,
        "--workspace-revision", "1", "--input-json", str(exported_file)])
    assert code == 0 and imported["data"]["proposal"]["status"] == "draft", imported
    code, history_export, *_ = invoke(monkeypatch, [*live.prefix, "storyboard", "export", wid, pid,
                                                   "--revision", str(proposal["revision"])])
    assert code == 0 and history_export["data"]["revision"] == proposal["revision"], history_export
    code, offline, *_ = invoke(monkeypatch, ["--offline", "--data-dir", str(live.data), "storyboard", "export", wid, pid, "--format", "csv"])
    assert code == 0 and offline["data"]["format"] == "csv", offline


def test_offline_queries_do_not_mark_running_jobs_interrupted_or_create_managers(tmp_path, monkeypatch):
    root = tmp_path / "state"
    job = root / "toolbox" / "timelapse" / "jobs" / ("a" * 32 + ".json")
    write_json(job, {"id": "a" * 32, "status": "running", "progress": 0.5})
    before = job.read_bytes(), job.stat().st_mtime_ns
    import landscape_culler.timelapse as timelapse
    monkeypatch.setattr(timelapse, "TimelapseManager", lambda *_: pytest.fail("must not create manager for offline queries"))
    code, answer, *_ = invoke(monkeypatch, ["--offline", "--data-dir", str(root), "timelapse", "jobs"])
    assert code == 0 and answer["data"][0]["status"] == "running"
    assert (job.read_bytes(), job.stat().st_mtime_ns) == before
    code, answer, *_ = invoke(monkeypatch, ["--offline", "--data-dir", str(root), "timelapse", "scan", "--input-root", str(tmp_path)])
    assert code == 5 and answer["error"]["code"] == "service_required"


def test_job_wait_timeout_cancel_and_terminal_failure_are_machine_readable(monkeypatch):
    class Fake:
        calls = []
        def request(self, method, path, body=None):
            self.calls.append((method, path))
            return {"id": "job", "status": "running"}
    client = Fake()
    with pytest.raises(cli.CliError) as error:
        cli._wait_job(client, "copy", "job", None, 0.01, 0.001, True)
    assert error.value.exit_code == 4 and error.value.code == "wait_timeout"
    assert client.calls[-1] == ("POST", "/api/selection-copies/job/cancel")
    client.request = lambda *_: {"id": "job", "status": "failed", "message": "disk full"}
    with pytest.raises(cli.CliError, match="disk full") as error:
        cli._wait_job(client, "copy", "job", None, 1, 0.01)
    assert error.value.code == "job_failed"


@pytest.mark.parametrize("arguments", [
    ["review", "decide", "workspace", "asset", "--status", "keep"],
    ["timelapse", "process", "session"],
    ["selection", "xmp", "run"],
    ["storyboard", "update", "workspace", "proposal", "--input-json", "missing.json"],
])
def test_existing_state_mutations_require_explicit_revision(arguments, monkeypatch):
    code, answer, *_ = invoke(monkeypatch, arguments)
    assert code == 2 and answer["error"]["code"] == "usage_error"


@pytest.mark.parametrize("origin", ["https://127.0.0.1:123", "http://example.com:80", "http://user:password@127.0.0.1:123", "http://127.0.0.1:123?token=secret", "http://127.0.0.1:0"])
def test_cli_rejects_remote_or_credential_bearing_urls(origin):
    with pytest.raises(cli.CliError) as error: cli._url(origin)
    assert error.value.code == "invalid_url" and "password" not in str(error.value) and "secret" not in str(error.value)


def test_schema_documents_versions_job_wait_and_no_implicit_acceptance(monkeypatch):
    code, answer, *_ = invoke(monkeypatch, ["schema"])
    assert code == 0
    for operation in ("review.watch", "review.decide", "storyboard.import", "timelapse.render", "selection.xmp", "job.cancel", "photo.score", "tools.xmp-cleanup-execute"):
        assert operation in answer["data"]["commands"]
    assert any("No implicit acceptance" in value for value in answer["data"]["rules"])
    assert "request_schema" in answer["data"]["commands"]["review.decide"]


def test_legacy_schema_snapshot_matches_existing_api_models():
    from landscape_culler import web
    from landscape_culler.toolbox_legacy_schemas import LEGACY_REQUEST_SCHEMAS
    for metadata in LEGACY_REQUEST_SCHEMAS.values():
        assert metadata["request_schema"] == getattr(web, metadata["request_model"]).model_json_schema()


def test_photo_and_files_commands_forward_existing_api_contracts_without_automatic_execution(live, monkeypatch):
    calls = []
    def fake_start(kind, args, context):
        calls.append({"kind": kind, "args": args, "context": context})
        return {"id": f"test-job-{len(calls)}", "kind": kind, "status": "queued", "context": context}
    monkeypatch.setattr(live.inner.state.jobs, "start", fake_start)
    code, projects, *_ = invoke(monkeypatch, [*live.prefix, "photo", "projects"])
    assert code == 0 and len(projects["data"]) == 1
    code, project, *_ = invoke(monkeypatch, [*live.prefix, "photo", "create", "--input-root", str(live.raw.parent)])
    assert code == 0, project
    project_id = project["data"]["project_id"]
    code, grouping, *_ = invoke(monkeypatch, [*live.prefix, "photo", "group", project_id])
    assert code == 0 and grouping["data"]["kind"] == "group"
    assert calls[-1]["context"]["project_id"] == project_id
    code, scoring, *_ = invoke(monkeypatch, [*live.prefix, "photo", "score", RUN_ID, "--revision", "0"])
    assert code == 0 and scoring["data"]["kind"] == "score", scoring
    ratings = live.root / "人工 星级.json"
    write_json(ratings, {"indexes": [0], "rating": 5})
    code, rated, *_ = invoke(monkeypatch, [*live.prefix, "photo", "rate", RUN_ID, "--revision", "0", "--input-json", str(ratings)])
    assert code == 0 and rated["data"]["review_revision"] == 1, rated
    code, detail, *_ = invoke(monkeypatch, [*live.prefix, "photo", "open-run", RUN_ID])
    assert code == 0 and detail["data"]["results"][0]["rating"] == 5

    preview_file = live.root / "raw-preview.json"
    write_json(preview_file, {"layout": "mixed", "direction": "jpeg", "mixed_path": str(live.raw.parent)})
    before = len(calls)
    code, preview, *_ = invoke(monkeypatch, [*live.prefix, "tools", "raw-jpeg-preview", "--input-json", str(preview_file)])
    assert code == 0 and len(calls) == before, preview
    plan_id = preview["data"]["plan_id"]
    code, executing, *_ = invoke(monkeypatch, [*live.prefix, "tools", "raw-jpeg-execute", "--plan-id", plan_id])
    assert code == 0 and executing["data"]["kind"] == "raw_jpeg_execute", executing
    assert calls[-1]["context"]["plan_id"] == plan_id and live.raw.read_bytes() == b"raw"

    sidecar = live.raw.with_suffix(".xmp")
    sidecar.write_text("original sidecar", encoding="utf-8")
    write_json(preview_file, {"root_path": str(live.raw.parent)})
    before = len(calls)
    code, preview, *_ = invoke(monkeypatch, [*live.prefix, "tools", "xmp-cleanup-preview", "--input-json", str(preview_file)])
    assert code == 0 and len(calls) == before and sidecar.exists(), preview
    code, executing, *_ = invoke(monkeypatch, [*live.prefix, "tools", "xmp-cleanup-execute", "--plan-id", preview["data"]["plan_id"]])
    assert code == 0 and executing["data"]["kind"] == "xmp_cleanup_execute", executing
    assert sidecar.read_text(encoding="utf-8") == "original sidecar"
    for kind in ("xmp", "raw-jpeg", "xmp-cleanup"):
        code, history, *_ = invoke(monkeypatch, [*live.prefix, "tools", "transactions", "--kind", kind])
        assert code == 0 and isinstance(history["data"], list)


@pytest.mark.parametrize("arguments", [
    ["tools", "raw-jpeg-execute"], ["tools", "xmp-cleanup-execute"],
    ["tools", "rollback", "--transaction-id", "test"],
    ["photo", "delete-project", "test"],
    ["photo", "delete-photos", "run", "--revision", "0", "--input-json", "plan.json"],
])
def test_file_mutations_require_explicit_plan_or_confirmation(arguments, monkeypatch):
    code, result, *_ = invoke(monkeypatch, arguments)
    assert code == 2 and result["error"]["code"] == "usage_error"


def test_cli_timelapse_scan_process_roundtrip_and_stale_revision(live, monkeypatch):
    for index in range(2):
        Image.new("RGB", (64, 40), "blue").save(live.source / f"后续帧{index}.jpg")
    code, scanned, *_ = invoke(monkeypatch, [*live.prefix, "timelapse", "scan", "--input-root", str(live.source),
        "--wait", "--wait-timeout", "10", "--interval", "0.02"])
    assert code == 0 and scanned["data"]["status"] == "completed", scanned
    sid = scanned["data"]["result"]["session_id"]
    code, processed, *_ = invoke(monkeypatch, [*live.prefix, "timelapse", "process", sid, "--revision", "0",
        "--no-holy-grail", "--no-deflicker", "--wait", "--wait-timeout", "10", "--interval", "0.02"])
    assert code == 0 and processed["data"]["result"]["revision"] == 1, processed
    code, conflict, *_ = invoke(monkeypatch, [*live.prefix, "timelapse", "process", sid, "--revision", "0"])
    assert code != 0 and "修订号" in conflict["error"]["message"], conflict


def test_serve_redacts_internal_handshake(monkeypatch, tmp_path):
    from landscape_culler import service
    def fake_main(argv):
        print(service.PROTOCOL + " " + json.dumps({"origin": "http://127.0.0.1:1234", "pid": 123,
                                                   "token": "SECRET-BOOTSTRAP", "control_token": "SECRET-CONTROL"}))
        return 0
    monkeypatch.setattr(service, "main", fake_main)
    code, answer, text, errors = invoke(monkeypatch, ["serve", "--content-root", str(tmp_path / "中文 根目录")])
    assert code == 0 and answer["data"]["mode"] == "foreground"
    assert "SECRET" not in text + errors


def test_real_service_process_cli_discovery_and_user_watch(tmp_path):
    """Exercise the installed-style auth boundary across actual OS processes."""
    project = Path(__file__).parents[1]
    content_root = tmp_path / "摄影 服务 空间"
    source = tmp_path / "人工 审片"
    source.mkdir()
    Image.new("RGB", (80, 50), "green").save(source / "旅行 照片.jpg")
    environment = dict(os.environ)
    for key in ("PHOTO_AI_PARENT_PID", "PHOTO_AI_DESKTOP_PID", "PHOTO_AI_BOOTSTRAP_CONTENT_ROOT",
                "PHOTO_AI_PROJECTS_DIR", "PHOTO_AI_DATA_DIR", "PHOTO_AI_CONTENT_ROOT"):
        environment.pop(key, None)
    environment["PYTHONUTF8"] = "1"
    command = [sys.executable, "-m", "landscape_culler.toolbox_cli"]
    logs = tmp_path / "serve.stderr.log"
    watcher = None
    with logs.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen([*command, "serve", "--content-root", str(content_root)], cwd=project,
                                   env=environment, stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8")
        messages = queue.Queue()
        reader = threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            handshake_line = messages.get(timeout=15)
            handshake = json.loads(handshake_line)
            assert handshake["ok"] is True and handshake["command"] == "serve", handshake
            assert not {"token", "control_token", "credential", "headers"} & set(handshake["data"])
            descriptor = Path(handshake["data"]["connection_file"])
            assert descriptor.is_file() and descriptor == content_root / "state" / "cli-session.json"
            record = read_json(descriptor)
            assert record["credential"]["value"] not in handshake_line

            def run(*arguments):
                result = subprocess.run([*command, "--data-dir", str(content_root), *arguments], cwd=project,
                                        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=15)
                answer = json.loads(result.stdout)
                assert result.returncode == 0, answer
                assert record["credential"]["value"] not in result.stdout + result.stderr
                return answer["data"]

            capabilities = run("capabilities")
            assert capabilities["transport"] == "http"
            workspace = run("review", "create", "--input-root", str(source), "--name", "CLI 与人协作")
            wid, asset = workspace["workspace_id"], workspace["items"][0]["id"]
            initial = run("review", "get", wid)
            assert initial["user_revision"] == 0
            watcher = subprocess.Popen([*command, "--data-dir", str(content_root), "review", "watch",
                "--workspace", wid, "--after-user-revision", "0", "--timeout", "8", "--interval", "0.1"],
                cwd=project, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            time.sleep(0.15)
            run("review", "suggest", wid, "--revision", "0", "--mode", "organize")
            assert watcher.poll() is None, "AI suggestion must not satisfy the user watch"
            run("review", "decide", wid, asset, "--revision", "0", "--status", "keep", "--notes", "人工确认保留")
            watch_output, watch_errors = watcher.communicate(timeout=12)
            assert watcher.returncode == 0, watch_errors
            observed = json.loads(watch_output)["data"]
            assert observed["user_revision"] == 1
            assert observed["items"][0]["user"]["notes"] == "人工确认保留"
            assert record["credential"]["value"] not in watch_output + watch_errors

            # Force offline reads while the real service is alive: persisted
            # job state remains byte-for-byte unchanged and no manager starts.
            fake_active = content_root / "state" / "toolbox" / "timelapse" / "jobs" / ("b" * 32 + ".json")
            write_json(fake_active, {"id": "b" * 32, "status": "running"})
            before = fake_active.read_bytes(), fake_active.stat().st_mtime_ns
            offline = subprocess.run([*command, "--offline", "--data-dir", str(content_root), "timelapse", "jobs"],
                cwd=project, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=10)
            assert offline.returncode == 0
            assert json.loads(offline.stdout)["data"][0]["status"] == "running"
            assert (fake_active.read_bytes(), fake_active.stat().st_mtime_ns) == before
        finally:
            if watcher and watcher.poll() is None:
                watcher.terminate(); watcher.wait(timeout=5)
            if process.poll() is None:
                process.terminate(); process.wait(timeout=5)
            process.stdout.close()
