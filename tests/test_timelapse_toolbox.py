from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from landscape_culler.timelapse import TimelapseManager, find_media_tool
from landscape_culler.timelapse_api import create_timelapse_router
from landscape_culler._vendor.lrtlite.xmp import XmpSidecar


def sequence(root: Path, count=12, mixed=False):
    root.mkdir()
    for i in range(count):
        width = 128 if mixed and i == 1 else 160
        image = Image.new("RGB", (width, 90), (75 + (i % 3) * 30,) * 3)
        image.save(root / f"IMG_{i + 1}.jpg")
    return root


def wait(manager, job):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        item = manager.job(job["id"])
        if item["status"] not in {"running", "queued"}:
            return item
        time.sleep(0.02)
    pytest.fail(f"Job did not finish: {manager.job(job['id'])}")


def scan(manager, root):
    job = wait(manager, manager.submit("scan", {"input_root": str(root)}))
    assert job["status"] == "completed", job
    return manager.session(job["result"]["session_id"])


@pytest.fixture
def manager(tmp_path):
    item = TimelapseManager(tmp_path / "data")
    yield item
    item.close()


def fingerprint(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()}


def test_scan_natural_order_and_output_preserves_sources(tmp_path, manager):
    root = sequence(tmp_path / "source")
    xmp = XmpSidecar.new()
    xmp.rating = 5
    xmp.set("dc:description", "用户原有说明")
    xmp.save(root / "IMG_1.xmp")
    before = fingerprint(root)
    session = scan(manager, root)
    assert [f["name"] for f in session["frames"]][:3] == ["IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg"]
    assert session["frame_count"] == 12
    assert session["renderable"] is True
    params = {"keyframe_mode": "even", "keyframe_count": 3, "holy_grail": False, "deflicker": True}
    first = wait(manager, manager.submit("process", params, session["id"]))
    assert first["status"] == "completed", first
    first_exposures = [f["exposure"] for f in manager.session(session["id"])["frames"]]
    assert any(abs(v) > 0.01 for v in first_exposures)
    second = wait(manager, manager.submit("process", params, session["id"]))
    assert second["status"] == "completed", second
    assert first_exposures == [f["exposure"] for f in manager.session(session["id"])["frames"]]
    assert first["result"]["output_dir"] != second["result"]["output_dir"]
    assert fingerprint(root) == before
    copied = XmpSidecar.load(Path(first["result"]["sidecar_dir"]) / "IMG_1.xmp")
    assert copied.get("dc:description") == "用户原有说明"
    assert manager.artifact(first["id"], "xmp").is_file()


def test_process_requires_matching_optional_revision_and_publishes_next(tmp_path, manager):
    session = scan(manager, sequence(tmp_path / "revision-source", count=3))
    assert session["revision"] == 0
    params = {"base_revision": 0, "holy_grail": False, "deflicker": False}
    job = wait(manager, manager.submit("process", params, session["id"]))
    assert job["status"] == "completed", job
    assert job["result"]["revision"] == 1
    assert manager.session(session["id"])["revision"] == 1
    with pytest.raises(ValueError, match="修订号"):
        manager.submit("process", params, session["id"])


def test_changed_sidecars_block_processing(tmp_path, manager):
    root = sequence(tmp_path / "source")
    session = scan(manager, root)
    xmp = XmpSidecar.new()
    xmp.set_exposure(1.25)
    xmp.save(root / "IMG_1.xmp")
    before = fingerprint(root)
    job = wait(manager, manager.submit("process", {}, session["id"]))
    assert job["status"] == "failed"
    assert "XMP 已被修改" in job["error"]
    assert fingerprint(root) == before


def test_invalid_sidecar_not_silently_replaced(tmp_path, manager):
    root = sequence(tmp_path / "source")
    (root / "IMG_1.xmp").write_text("invalid xml", encoding="utf-8")
    job = wait(manager, manager.submit("scan", {"input_root": str(root)}))
    assert job["status"] == "failed"
    assert (root / "IMG_1.xmp").read_text() == "invalid xml"


def test_duplicate_raw_jpeg_requires_format_choice(tmp_path, manager):
    root = sequence(tmp_path / "source")
    (root / "IMG_1.arw").write_bytes(b"test")
    job = wait(manager, manager.submit("scan", {"input_root": str(root)}))
    assert job["status"] == "failed"
    assert "同名不同格式" in job["error"]
    job = wait(manager, manager.submit("scan", {"input_root": str(root), "extensions": [".jpg"]}))
    assert job["status"] == "completed"


def test_mixed_dimensions_require_explicit_acceptance(tmp_path, manager):
    root = sequence(tmp_path / "source", mixed=True)
    session = scan(manager, root)
    assert session["mixed_dimensions"] is True
    job = wait(manager, manager.submit("render", {}, session["id"]))
    assert job["status"] == "failed"
    assert "不同尺寸" in job["error"]


def test_restart_retains_session_and_marks_running_interrupted(tmp_path):
    data = tmp_path / "data"
    manager = TimelapseManager(data)
    session = scan(manager, sequence(tmp_path / "source"))
    manager.close()
    running_id = "f" * 32
    path = data / "toolbox" / "timelapse" / "jobs" / f"{running_id}.json"
    path.write_text(json.dumps({"id": running_id, "status": "running", "created_at": "2026-10-25"}), encoding="utf-8")
    reopened = TimelapseManager(data)
    try:
        assert reopened.session(session["id"])["frame_count"] == 12
        assert reopened.job(running_id)["status"] == "interrupted"
        assert json.loads(path.read_text(encoding="utf-8"))["status"] == "interrupted"
    finally:
        reopened.close()


def test_cancel_queued_job_persists_cancelled(tmp_path, monkeypatch):
    manager = TimelapseManager(tmp_path / "data")
    started, release = threading.Event(), threading.Event()
    def blocked(*args):
        started.set()
        release.wait(10)
        return {}
    monkeypatch.setattr(manager, "_scan", blocked)
    first = manager.submit("scan", {})
    assert started.wait(3)
    second = manager.submit("scan", {})
    manager.cancel(second["id"])
    release.set()
    try:
        wait(manager, first)
        assert wait(manager, second)["status"] == "cancelled"
    finally:
        manager.close()


def test_router_mutation_token_and_validation(tmp_path):
    def token(x_photo_token: str | None = Header(None)):
        if x_photo_token != "expected":
            raise HTTPException(403, "Forbidden")
    router = create_timelapse_router(tmp_path / "data", token)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    prefix = "/api/toolbox/timelapse"
    try:
        assert client.post(prefix + "/scan", json={"input_root": str(tmp_path)}).status_code == 403
        assert client.post(prefix + "/jobs/" + "f" * 32 + "/cancel").status_code == 403
        assert client.get(prefix + "/sessions").json() == []
        response = client.post(prefix + "/sessions/" + "f" * 32 + "/render",
                               json={"width": 101}, headers={"x-photo-token": "expected"})
        assert response.status_code == 422
    finally:
        router.timelapse_manager.close()


@pytest.mark.parametrize("tool_name", ["ffmpeg", "ffprobe"])
def test_media_tool_uses_packaged_resource_root(tmp_path, monkeypatch, tool_name):
    tools = tmp_path / "tools" / "ffmpeg"
    tools.mkdir(parents=True)
    filename = tool_name + (".exe" if __import__("os").name == "nt" else "")
    executable = tools / filename
    executable.write_bytes(b"tool-discovery-fixture")
    monkeypatch.delenv(f"PHOTO_TOOLBOX_{tool_name.upper()}", raising=False)
    monkeypatch.delenv(f"PHOTO_AI_{tool_name.upper()}", raising=False)
    monkeypatch.setenv("PHOTO_AI_RESOURCE_ROOT", str(tmp_path))
    assert find_media_tool(tool_name) == str(executable.resolve())


@pytest.mark.skipif(not (find_media_tool("ffmpeg") and find_media_tool("ffprobe")), reason="FFmpeg not installed")
def test_render_real_video_decode_and_source_preservation(tmp_path, manager):
    root = sequence(tmp_path / "source")
    before = fingerprint(root)
    session = scan(manager, root)
    job = wait(manager, manager.submit("render", {"width": 320, "height": 180, "fps": 12, "crf": 20}, session["id"]))
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["verified"] is True
    assert result["frames"] == 12
    assert result["duration"] == pytest.approx(1, abs=.01)
    assert Path(result["video_path"]).is_file()
    decode = subprocess.run([find_media_tool("ffmpeg"), "-v", "error", "-i", result["video_path"], "-f", "null", "-"], capture_output=True, timeout=10)
    assert decode.returncode == 0, decode.stderr
    assert fingerprint(root) == before
