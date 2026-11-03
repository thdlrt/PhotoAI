from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from landscape_culler.framepick import FramePickLauncher
from landscape_culler.web import create_app


def test_installed_framepick_does_not_adopt_source_or_path(tmp_path, monkeypatch):
    resources = tmp_path / "installed"
    monkeypatch.setenv("PHOTO_AI_RESOURCE_ROOT", str(resources))
    launcher = FramePickLauncher(tmp_path / "data", tmp_path / "checkout")
    assert launcher.executable() == resources / "tools/framepick/FramePick.exe"
    assert launcher.status()["available"] is False
    with pytest.raises(FileNotFoundError): launcher.launch_spec()
    assert not launcher.storage.exists()


def test_launch_spec_owns_data_reuses_media_and_drops_service_secrets(tmp_path, monkeypatch):
    resources = tmp_path / "installed 程序"
    executable = resources / "tools/framepick/FramePick.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    (resources / "tools/ffmpeg").mkdir()
    monkeypatch.setenv("PHOTO_AI_RESOURCE_ROOT", str(resources))
    monkeypatch.setenv("PHOTO_AI_PRIVATE_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak-either")
    launcher = FramePickLauncher(tmp_path / "data", tmp_path / "checkout")
    spec = launcher.launch_spec()
    environment = dict(pair.split("=", 1) for pair in spec["environment"])
    assert environment["FRAMEPICK_PROJECT_DIRECTORY"] == str(launcher.storage / "projects")
    assert environment["FRAMEPICK_FFMPEG_DIRECTORY"] == str((resources / "tools/ffmpeg").resolve())
    assert environment["FRAMEPICK_DATA_ROOT"] == str(launcher.storage / "state")
    assert environment["TEMP"] == environment["TMP"] == str(launcher.storage / "temp")
    assert "must-not-leak" not in json.dumps(spec)
    assert spec["command_line"].startswith('"')


def test_launch_uses_hidden_wmi_broker_and_requires_valid_pid(tmp_path, monkeypatch):
    launcher = FramePickLauncher(tmp_path / "data", tmp_path)
    spec = {"command_line": '"E:\\fixture\\FramePick.exe"', "working_directory": "E:\\fixture", "environment": []}
    monkeypatch.setattr(launcher, "launch_spec", lambda: spec)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"pid":12345}')
    monkeypatch.setattr("landscape_culler.framepick.subprocess.run", run)
    assert launcher.launch()["pid"] == 12345
    command, kwargs = calls[0]
    script = base64.b64decode(command[-1]).decode("utf-16le")
    assert "Win32_Process" in script and "Invoke-CimMethod" in script
    assert kwargs["timeout"] == 25 and kwargs["creationflags"] != 0
    monkeypatch.setattr("landscape_culler.framepick.subprocess.run", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="not a receipt"))
    with pytest.raises(OSError, match="回执"): launcher.launch()


def test_framepick_launch_uses_shared_csrf_and_does_not_touch_old_projects(tmp_path, monkeypatch):
    data = tmp_path / "data"
    old = data / "runs/legacy/review.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"ratings":{"0":5},"unknown":"keep"}', encoding="utf-8")
    before = old.read_bytes(), old.stat().st_mtime_ns
    calls = []
    monkeypatch.setattr(FramePickLauncher, "launch", lambda _self: calls.append(1) or {"launched": True, "pid": 12345})
    with TestClient(create_app(data_dir=data, project_root=tmp_path)) as client:
        assert client.get("/api/toolbox/framepick").status_code == 200
        assert client.post("/api/toolbox/framepick/launch").status_code == 403
        assert client.post("/api/toolbox/framepick/launch", headers={"X-Photo-AI-Token":"invalid"}).status_code == 403
        assert not calls
        token = client.get("/api/bootstrap").json()["token"]
        response = client.post("/api/toolbox/framepick/launch", headers={"X-Photo-AI-Token":token})
        assert response.status_code == 200 and response.json()["pid"] == 12345
        assert calls == [1]
    assert before == (old.read_bytes(), old.stat().st_mtime_ns)
