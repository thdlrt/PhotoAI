from __future__ import annotations

import hashlib
import io
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from landscape_culler.collaborative_review import ReviewConflict, ReviewCorrupt, ReviewStore, export_csv
from landscape_culler.collaborative_review_api import create_review_router
from landscape_culler.collaborative_review_vision import ReviewVisionJobs


def source_files(tmp_path):
    source = tmp_path / "originals"
    (source / "city").mkdir(parents=True)
    Image.new("RGB", (80, 60), "red").save(source / "city" / "photo.jpg")
    (source / "city" / "clip.mp4").write_bytes(b"video-bytes-for-range-test")
    (source / "secret.txt").write_text("not-media", encoding="utf-8")
    return source


def setup_store(tmp_path):
    source = source_files(tmp_path)
    store = ReviewStore(tmp_path / "data", video_probe=lambda _: {"duration": 10.0, "width": 1920, "height": 1080})
    workspace = store.create(str(source))
    return store, workspace, source


def asset(workspace, kind):
    return next(item for item in workspace["items"] if item["kind"] == kind)


def test_index_has_stable_ids_and_never_changes_originals(tmp_path):
    source = source_files(tmp_path)
    originals = {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                 for path in source.rglob("*") if path.is_file()}
    store = ReviewStore(tmp_path / "data")
    first = store.create(str(source))
    store.update_item(first["workspace_id"], asset(first, "photo")["id"], 0, {"status": "keep", "notes": "纪念意义"})
    path = store.path(first["workspace_id"])
    before = path.read_bytes()
    reopened = store.create(str(source), "Ignored rename")
    assert path.read_bytes() == before
    assert reopened["revision"] == 1
    assert {item["id"] for item in first["items"]} == {item["id"] for item in reopened["items"]}
    assert asset(reopened, "photo")["user"]["notes"] == "纪念意义"
    assert len(reopened["items"]) == 2
    assert originals == {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                         for path in source.rglob("*") if path.is_file()}


def test_item_revision_conflict_is_independent_of_other_items(tmp_path):
    store, workspace, _ = setup_store(tmp_path)
    wid = workspace["workspace_id"]
    photo, video = asset(workspace, "photo"), asset(workspace, "video")
    store.update_item(wid, photo["id"], 0, {"status": "keep"})
    # Another asset can still be edited with its own unchanged revision.
    store.update_item(wid, video["id"], 0, {"status": "maybe"})
    with pytest.raises(ReviewConflict):
        store.update_item(wid, photo["id"], 0, {"status": "reject"})
    assert asset(store.get(wid), "photo")["user"]["status"] == "keep"
    previous = json.loads(store.path(wid).with_name("workspace.previous.json").read_text(encoding="utf-8"))
    assert previous["revision"] == 1


def test_organize_and_imported_suggestions_cannot_overwrite_manual_decisions(tmp_path):
    store, workspace, _ = setup_store(tmp_path)
    wid, pid = workspace["workspace_id"], asset(workspace, "photo")["id"]
    manual = store.update_item(wid, pid, 0, {"status": "reject", "quality": 2, "chapter": "用户章节", "order": 8})
    organized = store.organize(wid, manual["revision"])
    assert asset(organized, "photo")["assistant"]["source"] == "folder-order"
    imported = store.suggestions(wid, organized["revision"], {pid: {"status": "keep", "chapter": "AI章节", "reason": "光线", "order": 1}})
    photo = asset(imported, "photo")
    assert photo["user"] == asset(manual, "photo")["user"]
    assert photo["assistant"]["status"] == "keep"
    assert store.export(wid)["clips"] == []
    with pytest.raises(ReviewConflict):
        store.suggestions(wid, organized["revision"], {})


@pytest.mark.parametrize("segments", [
    [{"in": 1, "out": 11}], [{"in": -1, "out": 5}], [{"in": 5, "out": 5}],
    [{"in": 5, "out": 2}], [{"in": float("nan"), "out": 2}],
    [{"in": True, "out": 2}], [{"in": 1, "out": float("inf")}],
])
def test_video_segments_are_finite_original_source_bounds(tmp_path, segments):
    store, workspace, _ = setup_store(tmp_path)
    wid, vid = workspace["workspace_id"], asset(workspace, "video")["id"]
    before = store.path(wid).read_bytes()
    with pytest.raises(ValueError):
        store.update_item(wid, vid, 0, {"segments": segments})
    assert store.path(wid).read_bytes() == before


def test_photos_reject_video_segments_and_valid_segments_export_separately(tmp_path):
    store, workspace, _ = setup_store(tmp_path)
    wid = workspace["workspace_id"]
    with pytest.raises(ValueError):
        store.update_item(wid, asset(workspace, "photo")["id"], 0, {"segments": [{"in": 0, "out": 1}]})
    store.update_item(wid, asset(workspace, "video")["id"], 0, {
        "status": "must", "segments": [{"in": 1, "out": 3, "note": "转身"}, {"in": 7, "out": 9}], "notes": "=HYPERLINK(x)"})
    exported = store.export(wid)
    assert exported["time_basis"] == "original-source-seconds"
    assert [(row["source_in"], row["source_out"]) for row in exported["clips"]] == [(1, 3), (7, 9)]
    assert exported["clips"][0]["status"] == "keep"
    assert "'=HYPERLINK(x)" in export_csv(exported)


def test_rescan_preserves_missing_items_manual_decisions_and_flags_changes(tmp_path):
    store, workspace, source = setup_store(tmp_path)
    wid, photo = workspace["workspace_id"], asset(workspace, "photo")
    saved = store.update_item(wid, photo["id"], 0, {"status": "keep", "notes": "保留"})
    Path(photo["path"]).unlink()
    scanned = store.rescan(wid, saved["revision"])
    missing = asset(scanned, "photo")
    assert missing["available"] is False and missing["user"]["notes"] == "保留"
    video = asset(scanned, "video")
    Path(video["path"]).write_bytes(b"changed-video")
    with pytest.raises(ReviewConflict):
        store.media_path(wid, video["id"])
    rescanned = store.rescan(wid, scanned["revision"])
    assert asset(rescanned, "video")["source_changed"] is True


def test_corrupt_state_is_not_reset_or_overwritten(tmp_path):
    store, workspace, _ = setup_store(tmp_path)
    path = store.path(workspace["workspace_id"])
    path.write_text('{"schema":', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ReviewCorrupt):
        store.get(workspace["workspace_id"])
    with pytest.raises(ReviewCorrupt):
        store.create(workspace["input_root"])
    assert path.read_bytes() == before
    assert store.list()[0]["error"]


def test_unknown_ids_and_registered_path_escape_never_serve_files(tmp_path):
    store, workspace, _ = setup_store(tmp_path)
    wid, photo = workspace["workspace_id"], asset(workspace, "photo")
    with pytest.raises(ValueError):
        store.get("../workspace")
    with pytest.raises(FileNotFoundError):
        store.media_path(wid, "0" * 24)
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (10, 10)).save(outside)
    state = store._load(wid)
    state["assets"][photo["id"]]["path"] = str(outside)
    with pytest.raises(ValueError):
        store.media_path(wid, photo["id"], state=state)


def test_existing_ai_results_are_identified_and_keep_human_data(tmp_path):
    store, workspace, _ = setup_store(tmp_path)
    photo = asset(workspace, "photo")
    folder = store.data_dir / "runs" / "20261025-010000"
    folder.mkdir(parents=True)
    (folder / "results.json").write_text(json.dumps({"run_id": folder.name, "results": [
        {"path": photo["path"], "score": 0.85, "rating": 4}]}), encoding="utf-8")
    organized = store.organize(workspace["workspace_id"], 0, existing_ai=True)
    assert asset(organized, "photo")["assistant"]["source"] == "existing-photo-ai"
    assert asset(organized, "photo")["assistant"]["score"] == 0.85
    assert asset(organized, "photo")["user"]["status"] == "unreviewed"
    assert asset(organized, "video")["assistant"]["source"] == "folder-order"


def client_for(store):
    def token(x_photo_ai_token: str | None = Header(None)):
        if x_photo_ai_token != "test-token":
            raise HTTPException(403)
    router = create_review_router(store.data_dir, token, store=store)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), router


def test_api_token_conflict_media_ranges_and_download(tmp_path):
    store, workspace, _ = setup_store(tmp_path)
    client, router = client_for(store)
    wid, video = workspace["workspace_id"], asset(workspace, "video")
    base = f"/api/review-workspaces/{wid}"
    try:
        assert client.get(base).status_code == 200
        assert client.patch(base + "/items/" + video["id"], json={"revision": 0, "status": "keep"}).status_code == 403
        headers = {"X-Photo-AI-Token": "test-token"}
        assert client.patch(base + "/items/" + video["id"], headers=headers,
                            json={"revision": 0, "status": "keep"}).status_code == 200
        assert client.patch(base + "/items/" + video["id"], headers=headers,
                            json={"revision": 0, "status": "reject"}).status_code == 409
        media = client.get(base + "/media/" + video["id"], headers={"Range": "bytes=2-5"})
        assert media.status_code == 206 and media.content == Path(video["path"]).read_bytes()[2:6]
        assert media.headers["accept-ranges"] == "bytes"
        exported = client.get(base + "/export?format=json")
        assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
        assert exported.json()["clips"][0]["status"] == "keep"
    finally:
        router.review_jobs.shutdown()


def wait_job(jobs, wid, jid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = jobs.get(wid, jid)
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.01)
    pytest.fail("mock review job did not finish")


def test_vision_saves_evidence_and_preserves_concurrent_manual_edit(tmp_path, monkeypatch):
    store, workspace, _ = setup_store(tmp_path)
    wid, video = workspace["workspace_id"], asset(workspace, "video")
    entered, proceed = threading.Event(), threading.Event()
    seen = []
    class Provider:
        model = "test-vision-model"
        def __call__(self, **kwargs):
            seen.append(kwargs)
            entered.set()
            assert proceed.wait(3)
            return {"status": "keep", "reason": "主体清晰", "chapter": "街景", "score": 80}
    monkeypatch.setattr("landscape_culler.collaborative_review_vision.preview_image", lambda *a, **k: Path("mock.jpg"))
    monkeypatch.setattr("landscape_culler.collaborative_review_vision._image_base64", lambda _: "test-image")
    jobs = ReviewVisionJobs(store, provider_factory=lambda _: Provider())
    try:
        job = jobs.start(wid, 0, [video["id"]])
        assert entered.wait(3)
        store.update_item(wid, video["id"], 0, {"status": "reject", "notes": "不用这段"})
        proceed.set()
        done = wait_job(jobs, wid, job["job_id"])
        assert done["status"] == "done" and done["saved"] == 1
        result = asset(store.get(wid), "video")
        assert result["user"]["status"] == "reject" and result["user"]["notes"] == "不用这段"
        assert result["assistant"]["source"] == "vision-model"
        assert result["assistant"]["sample_times"] == [1.0, 5.0, 9.0]
        assert "未听声音" in result["assistant"]["evidence"]
        assert seen[0]["images"] == ["test-image"] * 3
    finally:
        proceed.set()
        jobs.shutdown()


def test_vision_cancel_discards_in_flight_response(tmp_path, monkeypatch):
    store, workspace, _ = setup_store(tmp_path)
    wid, photo = workspace["workspace_id"], asset(workspace, "photo")
    entered, proceed = threading.Event(), threading.Event()
    class Provider:
        model = "test"
        def __call__(self, **_):
            entered.set()
            assert proceed.wait(3)
            return {"status": "keep", "reason": "ok", "chapter": "", "score": 80}
    monkeypatch.setattr("landscape_culler.collaborative_review_vision.preview_image", lambda *a, **k: Path("mock.jpg"))
    monkeypatch.setattr("landscape_culler.collaborative_review_vision._image_base64", lambda _: "test-image")
    jobs = ReviewVisionJobs(store, provider_factory=lambda _: Provider())
    try:
        job = jobs.start(wid, 0, [photo["id"]])
        assert entered.wait(3)
        assert jobs.cancel(wid, job["job_id"])["status"] == "cancelling"
        proceed.set()
        assert wait_job(jobs, wid, job["job_id"])["status"] == "cancelled"
        assert asset(store.get(wid), "photo")["assistant"] is None
    finally:
        proceed.set()
        jobs.shutdown()


def test_vision_provider_unavailable_is_explicit_and_no_fake_suggestions(tmp_path):
    store, workspace, _ = setup_store(tmp_path)
    def unavailable(_):
        raise ValueError("未配置视觉模型")
    jobs = ReviewVisionJobs(store, provider_factory=unavailable)
    try:
        job = jobs.start(workspace["workspace_id"], 0)
        done = wait_job(jobs, workspace["workspace_id"], job["job_id"])
        assert done["status"] == "failed" and done["error"] == "未配置视觉模型"
        assert all(item["assistant"] is None for item in store.get(workspace["workspace_id"])["items"])
    finally:
        jobs.shutdown()


def test_real_ffmpeg_source_probe_and_frame_sampling(tmp_path):
    from landscape_culler.timelapse import find_media_tool
    from landscape_culler.collaborative_review import probe_video
    from landscape_culler.collaborative_review_vision import preview_image
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg or not find_media_tool("ffprobe"):
        pytest.skip("FFmpeg tools unavailable")
    source = tmp_path / "originals"
    source.mkdir()
    video = source / "synthetic.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
                    "testsrc2=s=160x90:r=5:d=2", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(video)],
                   check=True, capture_output=True, timeout=20)
    before = hashlib.sha256(video.read_bytes()).hexdigest(), video.stat().st_mtime_ns
    metadata = probe_video(video)
    assert metadata["duration"] == pytest.approx(2, abs=0.1)
    store = ReviewStore(tmp_path / "data")
    workspace = store.create(str(source))
    frames = [preview_image(store, workspace["workspace_id"], workspace["items"][0]["id"], source_time=t)
              for t in (0.2, 1.0, 1.8)]
    assert len({path.read_bytes() for path in frames}) == 3
    for path in frames:
        with Image.open(path) as frame:
            assert frame.format == "JPEG" and max(frame.size) <= 768
    assert before == (hashlib.sha256(video.read_bytes()).hexdigest(), video.stat().st_mtime_ns)


def test_packaged_raw_preview_uses_embedded_jpeg_and_camera_orientation(tmp_path, monkeypatch):
    from landscape_culler.collaborative_review_vision import preview_image
    source = tmp_path / "originals"
    source.mkdir()
    raw = source / "portrait.ARW"
    raw.write_bytes(b"synthetic-raw-with-mocked-embedded-preview")
    with io.BytesIO() as buffer:
        Image.new("RGB", (120, 80), "red").save(buffer, "JPEG")
        jpeg = buffer.getvalue()
    calls = []
    monkeypatch.setattr("landscape_culler.timelapse.find_media_tool", lambda _: "test-exiftool")
    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"6\n" if "-Orientation" in args else jpeg, b"")
    monkeypatch.setattr("landscape_culler.collaborative_review_vision.subprocess.run", fake_run)
    monkeypatch.setattr("landscape_culler.preview._load_raw_thumbnail", lambda _: pytest.fail("rawpy must not load"))
    store = ReviewStore(tmp_path / "data")
    workspace = store.create(str(source))
    result = preview_image(store, workspace["workspace_id"], workspace["items"][0]["id"])
    with Image.open(result) as image:
        assert image.size == (80, 120)
    assert len(calls) == 2
    assert raw.read_bytes() == b"synthetic-raw-with-mocked-embedded-preview"


def test_packaged_local_provider_uses_owned_runtime_assigned_port(tmp_path, monkeypatch):
    from landscape_culler.collaborative_review_vision import VisionClient
    from landscape_culler import model_resources
    managed = tmp_path / "PhotoAIData"
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(managed))
    monkeypatch.setenv("PHOTO_AI_MODELS_DIR", str(managed / "models"))
    monkeypatch.delenv("PHOTO_AI_OLLAMA_ENDPOINT", raising=False)
    monkeypatch.setattr("landscape_culler.vision_provider.load_config", lambda _: {"mode": "local"})
    monkeypatch.setattr(model_resources, "active_vlm_model", lambda _: "test-local-model")
    seen = []
    def ensure(runtime_root, data_dir):
        seen.append((runtime_root, data_dir))
        monkeypatch.setenv("PHOTO_AI_OLLAMA_ENDPOINT", "http://127.0.0.1:51789")
    monkeypatch.setattr(model_resources, "ensure_owned_ollama", ensure)
    def fetch(url, **kwargs):
        assert url == "http://127.0.0.1:51789/api/tags"
        return io.BytesIO(b'{"models":[{"name":"test-local-model"}]}')
    monkeypatch.setattr("urllib.request.urlopen", fetch)
    provider = VisionClient(tmp_path / "data")
    assert provider.endpoint == "http://127.0.0.1:51789"
    assert seen == [(managed / "models", tmp_path / "data")]


def test_real_compatible_proxy_has_source_mapping_audio_range_and_reusable_cache(tmp_path):
    from landscape_culler.timelapse import find_media_tool
    ffmpeg, ffprobe = find_media_tool("ffmpeg"), find_media_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg tools unavailable")
    source = tmp_path / "media"
    source.mkdir()
    original = source / "camera-test.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
                    "testsrc2=s=160x90:r=25:d=2", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:v", "mpeg4", "-c:a", "aac", "-pix_fmt", "yuv420p", str(original)],
                   check=True, capture_output=True, timeout=20)
    fingerprint = original.read_bytes(), original.stat().st_mtime_ns
    store = ReviewStore(tmp_path / "data")
    workspace = store.create(str(source))
    wid, vid = workspace["workspace_id"], workspace["items"][0]["id"]
    before = store.path(wid).read_bytes()
    client, router = client_for(store)
    token = {"X-Photo-AI-Token": "test-token"}
    url = f"/api/review-workspaces/{wid}/proxy/{vid}"
    try:
        assert client.post(url, headers=token, json={"start": 0, "duration": 61}).status_code == 422
        assert client.post(url, headers=token, json={"start": 10, "duration": 1}).status_code == 422
        response = client.post(url, headers=token, json={"start": 0.5, "duration": 1})
        assert response.status_code == 200
        job = response.json()["job"]
        done = wait_job(router.review_jobs.proxy_jobs, wid, job["job_id"])
        assert done["status"] == "done", done
        assert done["source_start"] == 0.5 and done["duration"] == pytest.approx(1, abs=0.1)
        assert done["progress"] == 100 and done["preview_only"] is True
        proxy = router.review_jobs.proxy_jobs.media_path(wid, job["job_id"])
        metadata = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_name,codec_type",
                                   "-of", "json", str(proxy)], capture_output=True, check=True, timeout=10)
        codecs = {stream["codec_type"]: stream["codec_name"] for stream in json.loads(metadata.stdout)["streams"]}
        assert codecs == {"video": "h264", "audio": "aac"}
        part = client.get(done["preview_url"], headers={"Range": "bytes=0-31"})
        assert part.status_code == 206 and part.content == proxy.read_bytes()[:32]
        cached = client.post(url, headers=token, json={"start": 0.5, "duration": 1}).json()["job"]
        assert cached["status"] == "done" and cached["cached"] is True
        assert store.path(wid).read_bytes() == before
        assert fingerprint == (original.read_bytes(), original.stat().st_mtime_ns)
        # A rescan must not make an old preview valid for replacement source bytes.
        original.write_bytes(original.read_bytes() + b"replacement")
        store.rescan(wid, workspace["revision"])
        assert client.get(done["preview_url"]).status_code == 409
    finally:
        router.review_jobs.shutdown()


def test_proxy_cancellation_terminates_only_its_process_and_publishes_no_media(tmp_path, monkeypatch):
    from landscape_culler.collaborative_review_proxy import ReviewProxyJobs
    store, workspace, _ = setup_store(tmp_path)
    wid, vid = workspace["workspace_id"], asset(workspace, "video")["id"]
    entered = threading.Event()
    processes = []
    class FakeProcess:
        returncode = None
        def __init__(self, *_args, **_kwargs):
            processes.append(self)
            entered.set()
        def poll(self):
            return self.returncode
        def terminate(self):
            self.returncode = -15
        def kill(self):
            self.returncode = -9
        def wait(self, timeout=None):
            return self.returncode
    monkeypatch.setattr("landscape_culler.timelapse.find_media_tool", lambda _: "test-ffmpeg")
    monkeypatch.setattr("landscape_culler.collaborative_review_proxy.subprocess.Popen", FakeProcess)
    jobs = ReviewProxyJobs(store)
    try:
        job = jobs.start(wid, vid, 1, 2)
        assert entered.wait(3)
        jobs.cancel(wid, job["job_id"])
        final = wait_job(jobs, wid, job["job_id"])
        assert final["status"] == "cancelled"
        assert processes[0].returncode == -15
        with pytest.raises(ReviewConflict):
            jobs.media_path(wid, job["job_id"])
        assert not list((store.root / wid / "proxies").glob("*.mp4"))
    finally:
        jobs.shutdown()
