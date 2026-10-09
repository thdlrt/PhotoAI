import hashlib
import base64
import io
import json
from pathlib import Path
import pytest
from landscape_culler import app_updates as updates
from test_web import _client, RUN_ID


def release(tag="v0.9.0-beta.4", data=b"MZ installer", **changes):
    result = {"tag_name": tag, "prerelease": "-" in tag, "draft": False, "body": "notes",
              "assets": [{"name": updates.ASSET, "state": "uploaded", "size": len(data),
                          "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                          "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/{updates.ASSET}"}]}
    return result | changes


def test_release_selection():
    assert updates.select_release([release(), release("v0.9.0-beta.1")], "0.9.0-beta.2")["version"] == "0.9.0-beta.4"
    assert updates.select_release([release("v" + updates.PRODUCT_VERSION)]) is None
    assert updates.select_release([release("v0.9.0-beta.1")]) is None
    assert updates.select_release([release("v0.9.0-beta.3")], "0.9.0") is None
    assert updates.select_release([release("v0.9.1")], "0.9.0")["version"] == "0.9.1"
    assert updates.select_release([release(draft=True)], "0.9.0-beta.2") is None
    invalid = release()
    invalid["assets"][0]["browser_download_url"] = "https://example.org/setup.exe"
    assert updates.select_release([invalid], "0.9.0-beta.2") is None
    invalid = release()
    invalid["assets"][0]["digest"] = None
    assert updates.select_release([invalid], "0.9.0-beta.2") is None


class Response(io.BytesIO):
    status = 200
    headers = {}


def test_download_resume_and_integrity(tmp_path, monkeypatch):
    updater = updates.AppUpdater(tmp_path)
    payload = b"MZinstaller-content"
    updater.state["release"] = updates.select_release([release(data=payload)], "0.9.0-beta.2")
    folder = updater.root / "0.9.0-beta.4"
    folder.mkdir(parents=True)
    (folder / (updates.ASSET + ".part")).write_bytes(payload[:4])
    def request(url, headers=None):
        assert headers == {"Range": "bytes=4-"}
        response = Response(payload[4:])
        response.status = 206
        response.headers = {"Content-Range": f"bytes 4-{len(payload)-1}/{len(payload)}"}
        return response
    monkeypatch.setattr(updates, "open_url", request)
    updater._work("download")
    assert updater.status()["phase"] == "ready"
    assert (folder / updates.ASSET).read_bytes() == payload
    updater._work("download")  # Completed verified downloads are reusable.
    assert updater.status()["phase"] == "ready"


def test_bad_payload_and_cancel_never_enable_install(tmp_path, monkeypatch):
    updater = updates.AppUpdater(tmp_path)
    updater.state["release"] = updates.select_release([release()], "0.9.0-beta.2")
    monkeypatch.setattr(updates, "open_url", lambda *args: Response(b"bad"))
    updater._work("download")
    assert updater.status()["phase"] == "failed"
    with pytest.raises(ValueError): updater.install()
    updater.cancelled.set()
    updater._work("download")
    assert updater.status()["phase"] == "cancelled"


def test_api_token_tasks_and_old_project(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    endpoint = "/api/app-updates"
    source = data / "runs" / RUN_ID / "results.json"
    before = source.read_bytes()
    assert client.get(endpoint).json()["current_version"] == updates.PRODUCT_VERSION
    assert client.post(endpoint + "/check").status_code == 403
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    monkeypatch.setattr(client.app.state.jobs, "active", lambda: {"id": "busy"})
    assert client.post(endpoint + "/install", headers=headers).status_code == 409
    client.app.state.updater.installing = True
    assert client.post(endpoint + "/check", headers=headers).status_code == 409
    assert source.read_bytes() == before and raw.read_bytes() == b"raw"


def test_install_script_quotes_paths_and_waits_before_install():
    script = updates.installer_script(Path("E:/test's/update.exe"), Path("E:/Photo AI"), 123, "a"*64, Path("E:/log.txt"))
    assert "test''s" in script
    assert script.index("while (Get-Process") < script.index("Start-Process")
    assert "'/D=E:/Photo AI'" in script.replace("\\", "/")
    assert "Get-FileHash" in script and "ExitCode" in script
    assert "-WorkingDirectory" in script and "-WindowStyle Hidden" in script
    assert script.index("GetFolderPath('LocalApplicationData')") < script.index("if (Test-Path")
    assert "-WorkingDirectory 'E:/Photo AI'" in script.replace("\\", "/")


def test_nested_data_update_stages_every_running_path_outside_install(tmp_path, monkeypatch):
    install = tmp_path / "Photo AI"
    install.mkdir()
    (install / "PhotoAI.exe").write_bytes(b"old executable")
    (install / ".photoai-install-marker").write_text("PHOTO_AI_INSTALL/1")
    updater = updates.AppUpdater(install / "data")
    payload = b"MZ installer"
    updater.state.update(phase="ready", release=updates.select_release([release(data=payload)], "0.9.0-beta.2"))
    source = updater.root / updater.state["release"]["version"] / updates.ASSET
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    monkeypatch.setenv("PHOTO_AI_DESKTOP_PID", "123")
    monkeypatch.setenv("PHOTO_AI_INSTALL_DIR", str(install))
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setenv("TEMP", str(install / "data/temp"))
    monkeypatch.setenv("TMP", str(install / "data/temp"))
    calls = []
    class Process:
        def wait(self):
            return 0
    class Thread:
        def __init__(self, **_kwargs):
            pass
        def start(self):
            pass
    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        return Process()
    monkeypatch.setattr(updates.subprocess, "Popen", spawn)
    monkeypatch.setattr(updates.threading, "Thread", Thread)
    updater.install()
    command, options = calls[0]
    staging = Path(options["cwd"])
    assert not staging.resolve().is_relative_to(install.resolve())
    assert options["env"]["TEMP"] == str(staging) and options["env"]["TMP"] == str(staging)
    assert (staging / updates.ASSET).read_bytes() == payload and source.read_bytes() == payload
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    assert updates.ps_quote(str(staging / updates.ASSET)) in script
    assert "-WorkingDirectory " + updates.ps_quote(str(staging)) in script
    assert (install / "PhotoAI.exe").read_bytes() == b"old executable"


def test_staging_rejects_bad_digest_without_leaving_copy(tmp_path):
    installer = tmp_path / "download.exe"
    installer.write_bytes(b"invalid installer")
    with pytest.raises(ValueError, match="校验失败"):
        updates.stage_installer(installer, tmp_path / "installed", "a" * 64)
    assert not list(tmp_path.glob(".photoai-update-*"))
    assert installer.read_bytes() == b"invalid installer"
