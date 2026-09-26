import hashlib
import io
import json
from pathlib import Path
import pytest
from landscape_culler import app_updates as updates
from test_web import _client, RUN_ID


def release(tag="v0.9.0-beta.3", data=b"MZ installer", **changes):
    result = {"tag_name": tag, "prerelease": "-" in tag, "draft": False, "body": "notes",
              "assets": [{"name": updates.ASSET, "state": "uploaded", "size": len(data),
                          "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                          "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/{updates.ASSET}"}]}
    return result | changes


def test_release_selection():
    assert updates.select_release([release(), release("v0.9.0-beta.1")])["version"] == "0.9.0-beta.3"
    assert updates.select_release([release("v0.9.0-beta.1")]) is None
    assert updates.select_release([release("v0.9.0-beta.3")], "0.9.0") is None
    assert updates.select_release([release("v0.9.1")], "0.9.0")["version"] == "0.9.1"
    assert updates.select_release([release(draft=True)]) is None
    invalid = release()
    invalid["assets"][0]["browser_download_url"] = "https://example.org/setup.exe"
    assert updates.select_release([invalid]) is None
    invalid = release()
    invalid["assets"][0]["digest"] = None
    assert updates.select_release([invalid]) is None


class Response(io.BytesIO):
    status = 200
    headers = {}


def test_download_resume_and_integrity(tmp_path, monkeypatch):
    updater = updates.AppUpdater(tmp_path)
    payload = b"MZinstaller-content"
    updater.state["release"] = updates.select_release([release(data=payload)])
    folder = updater.root / "0.9.0-beta.3"
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
    updater.state["release"] = updates.select_release([release()])
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
