from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from landscape_culler import selection_export as exports
from landscape_culler import xmp
from landscape_culler.util import full_fingerprint, read_json, write_json


@pytest.fixture
def selection(tmp_path, monkeypatch):
    source = tmp_path / "photos"
    paths = [source / "day-one" / "DSC0001.ARW", source / "day-two" / "DSC0001.ARW", source / "unused.ARW"]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"raw-{index}".encode())
    paths[1].with_suffix(".xmp").write_text('<custom exposure="2.1">preserve me</custom>', encoding="utf-8")
    folder = tmp_path / "data" / "runs" / "legacy-run"
    folder.mkdir(parents=True)
    payload = {"run_id": "legacy-run", "input_root": str(source), "workflow_state": "scored",
               "review_revision": 4,
               "results": [{"index": i, "path": str(path), "rating": 4 if i < 2 else 5,
                            "ai_rating": 3, "manual_override": True, "score": 0.7, "group_id": i + 1,
                            "excluded": i == 2, "keywords": ["人工|终选"],
                            "develop": {"confirmed": True, "base": {"Exposure2012": 3}}}
                           for i, path in enumerate(paths)]}
    write_json(folder / "results.json", payload)
    write_json(folder / "review.json", {"revision": 4, "ratings": {"0": 4}, "custom_metadata": {"legacy": True}})
    write_json(folder / "develop.json", {"schema_version": 1, "items": [{"manual_crop": [0.1, 0.2, 0.8, 0.9]}]})
    jobs = SimpleNamespace(lock=threading.RLock(), blocks_photos=lambda _payload: False)
    calls = []

    def load(run_id):
        if run_id != "legacy-run":
            raise HTTPException(404, "unknown")
        return copy.deepcopy(payload)

    def auth(x_photo_ai_token: str | None = Header(None)):
        if x_photo_ai_token != "test-token":
            raise HTTPException(403, "token")

    def start(kind, args, context):
        calls.append({"kind": kind, "args": args, "context": context})
        return {"id": "xmp-job", "kind": kind, "status": "queued"}

    router = exports.make_selection_export_router(data_dir=tmp_path / "data", mutate_token=auth,
        load_run=load, run_file=lambda _run_id: folder / "results.json", jobs=jobs,
        review_lock=threading.RLock(), start_job=start)
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(exports, "assert_lightroom_not_running", lambda: None)
    monkeypatch.setattr(exports, "assert_exiftool_available", lambda: Path("exiftool"))
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, payload=payload, folder=folder, paths=paths,
                              manager=router.selection_copies, calls=calls, jobs=jobs,
                              headers={"X-Photo-AI-Token": "test-token"}, root=tmp_path)


BASE = "/api/runs/legacy-run/selection-export"


def _finish(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/selection-copies/{job_id}").json()
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.01)
    raise AssertionError("copy did not finish")


def test_manifest_and_download_preserve_legacy_files_and_ignore_recipes(selection):
    fixture = selection
    before = {p: p.read_bytes() for p in fixture.folder.iterdir()}
    answer = fixture.client.get(BASE).json()
    assert answer["count"] == 2 and answer["review_revision"] == 4
    assert answer["xmp_create_count"] == 1 and answer["xmp_existing_count"] == 1
    assert answer["items"][0]["rating"] == 4 and answer["items"][0]["ai_rating"] == 3
    assert "develop" not in answer["items"][0]
    assert answer["copy_job"] is None
    for format in ("json", "csv"):
        download = fixture.client.get(BASE + "/download", params={"format": format})
        assert download.status_code == 200
        assert "attachment;" in download.headers["content-disposition"]
        assert "Exposure2012" not in download.text
    assert all(p.read_bytes() == content for p, content in before.items())
    assert fixture.client.get(BASE + "/download?format=exe").status_code == 422


def test_csv_protects_literal_formula_filenames(selection):
    path = selection.root / "photos" / "=SUM(1,2).jpg"
    path.write_bytes(b"jpg")
    selection.payload["results"] = [{"path": str(path), "rating": 5, "keywords": ["@test"]}]
    download = selection.client.get(BASE + "/download?format=csv")
    assert "'=SUM(1,2).jpg" in download.text and "'@test" in download.text


def test_copy_preserves_relative_names_sidecar_and_source_bytes(selection):
    before = {p: full_fingerprint(p) for p in selection.paths}
    sidecar = selection.paths[1].with_suffix(".xmp")
    sidecar_before = sidecar.read_bytes()
    target = selection.root / "selected"
    response = selection.client.post(BASE + "/copy", headers=selection.headers,
        json={"base_revision": 4, "output_dir": str(target)})
    assert response.status_code == 200, response.text
    job = _finish(selection.client, response.json()["id"])
    assert job["status"] == "completed", job
    assert job["percent"] == 100 and job["completed_files"] == job["file_count"] == 3
    for source in selection.paths[:2]:
        output = target / source.relative_to(selection.root / "photos")
        assert output.read_bytes() == source.read_bytes()
    assert (target / "day-two" / "DSC0001.xmp").read_bytes() == sidecar_before
    assert not (target / "unused.ARW").exists()
    assert all(full_fingerprint(p) == value for p, value in before.items())
    assert selection.client.get(BASE).json()["copy_job"]["id"] == job["id"]
    recovered = exports.SelectionCopyManager(selection.root / "data").get(job["id"])
    assert recovered["status"] == "completed"


def test_copy_rejects_existing_directory_stale_revision_and_missing_token(selection):
    target = selection.root / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("untouched")
    body = {"base_revision": 4, "output_dir": str(target)}
    assert selection.client.post(BASE + "/copy", json=body).status_code == 403
    assert selection.client.post(BASE + "/copy", json=body, headers=selection.headers).status_code == 409
    body.update(base_revision=3, output_dir=str(selection.root / "new"))
    assert selection.client.post(BASE + "/copy", json=body, headers=selection.headers).status_code == 409
    assert not (selection.root / "new").exists()
    assert (target / "keep.txt").read_text() == "untouched"


def test_copy_cancels_without_publishing_partial_file(selection, monkeypatch):
    begun = threading.Event()
    real = selection.manager._copy_one

    def waiting(job, item, stop):
        begun.set()
        assert stop.wait(3)
        return real(job, item, stop)

    monkeypatch.setattr(selection.manager, "_copy_one", waiting)
    target = selection.root / "cancelled"
    answer = selection.client.post(BASE + "/copy", headers=selection.headers,
        json={"base_revision": 4, "output_dir": str(target)}).json()
    assert begun.wait(2)
    cancelled = selection.client.post(f"/api/selection-copies/{answer['id']}/cancel", headers=selection.headers)
    assert cancelled.status_code == 200
    job = _finish(selection.client, answer["id"])
    assert job["status"] == "cancelled" and job["completed_files"] == 0
    assert not list(target.rglob("*.ARW")) and not list(target.rglob("*.partial"))


def test_copy_detects_source_change_after_prepare(selection, monkeypatch):
    real = selection.manager._copy_one

    def changed(job, item, stop):
        Path(item["source"]).write_bytes(b"changed-original")
        return real(job, item, stop)

    monkeypatch.setattr(selection.manager, "_copy_one", changed)
    target = selection.root / "changed"
    answer = selection.client.post(BASE + "/copy", headers=selection.headers,
        json={"base_revision": 4, "output_dir": str(target)}).json()
    job = _finish(selection.client, answer["id"])
    assert job["status"] == "failed" and "发生变化" in job["message"]
    assert not list(target.rglob("*.ARW"))


def test_selection_refuses_path_outside_source_root(selection):
    outside = selection.root / "private.ARW"
    outside.write_bytes(b"outside")
    selection.payload["results"][0]["path"] = str(outside)
    assert selection.client.get(BASE).status_code == 409


def test_xmp_uses_existing_transaction_without_develop_and_preserves_existing_sidecar(selection, monkeypatch):
    originals = {p: p.read_bytes() for p in selection.folder.iterdir()}
    raw_before = {p: full_fingerprint(p) for p in selection.paths}
    sidecar = selection.paths[1].with_suffix(".xmp")
    sidecar_before = sidecar.read_bytes()
    response = selection.client.post(BASE + "/xmp", headers=selection.headers, json={"base_revision": 4})
    assert response.status_code == 200, response.text
    assert response.json()["create_count"] == 1 and response.json()["skipped_existing_count"] == 1
    call = selection.calls[0]
    assert call["kind"] == "xmp_commit" and call["args"][0] == "write-xmp"
    frozen_path = Path(call["args"][2])
    frozen = read_json(frozen_path)
    assert len(frozen["results"]) == 1 and "develop" not in frozen["results"][0]
    assert frozen["results"][0]["source_fingerprint"] == raw_before[selection.paths[0]]
    monkeypatch.setattr(xmp, "assert_lightroom_not_running", lambda: None)
    monkeypatch.setattr(xmp, "assert_exiftool_available", lambda: Path("test-exiftool"))
    monkeypatch.setattr(xmp, "validate_xmp_with_exiftool", lambda *_: None)
    result = xmp.write_results_xmp(frozen_path, commit=True)
    written = selection.paths[0].with_suffix(".xmp")
    text = written.read_text(encoding="utf-8")
    assert 'xmp:Rating="4"' in text and "crs:Exposure" not in text and "crs:Crop" not in text
    assert sidecar.read_bytes() == sidecar_before
    assert all(p.read_bytes() == content for p, content in originals.items())
    assert all(full_fingerprint(p) == value for p, value in raw_before.items())
    rollback = xmp.rollback_manifest(Path(result["manifest_path"]))
    assert str(written) in rollback["removed"] and not written.exists()
    assert sidecar.read_bytes() == sidecar_before


def test_xmp_preflight_finds_bundled_exiftool_without_legacy_environment(selection, monkeypatch):
    bundled = selection.root / "resources" / "tools" / "exiftool-13.59" / "exiftool-13.59_64" / "exiftool.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"test executable")
    monkeypatch.delenv("PHOTO_AI_EXIFTOOL", raising=False)
    monkeypatch.delenv("PHOTO_TOOLBOX_EXIFTOOL", raising=False)
    monkeypatch.delenv("LRT_EXIFTOOL", raising=False)
    monkeypatch.setenv("PHOTO_AI_RESOURCE_ROOT", str(selection.root / "resources"))
    monkeypatch.setattr(exports, "assert_exiftool_available", xmp.assert_exiftool_available)
    assert xmp.assert_exiftool_available() == bundled.resolve()
    response = selection.client.post(BASE + "/xmp", headers=selection.headers, json={"base_revision": 4})
    assert response.status_code == 200, response.text
    assert response.json()["create_count"] == 1


def test_xmp_preflight_still_rejects_missing_exiftool(selection, monkeypatch):
    monkeypatch.setattr("landscape_culler.xmp.find_media_tool", lambda _name: None)
    monkeypatch.setattr(exports, "assert_exiftool_available", xmp.assert_exiftool_available)
    response = selection.client.post(BASE + "/xmp", headers=selection.headers, json={"base_revision": 4})
    assert response.status_code == 409
    assert "ExifTool 不可用" in response.text
    assert not selection.calls


def test_xmp_refuses_changed_source_after_snapshot(selection, monkeypatch):
    response = selection.client.post(BASE + "/xmp", headers=selection.headers, json={"base_revision": 4})
    assert response.status_code == 200
    frozen_path = Path(selection.calls[0]["args"][2])
    selection.paths[0].write_bytes(b"new raw contents")
    with pytest.raises(RuntimeError, match="准备导出后发生变化"):
        xmp.write_results_xmp(frozen_path, commit=True)
    assert not selection.paths[0].with_suffix(".xmp").exists()


def test_xmp_refuses_stale_revision_and_unready_review(selection):
    assert selection.client.post(BASE + "/xmp", headers=selection.headers,
                                 json={"base_revision": 3}).status_code == 409
    selection.payload["needs_rescore"] = True
    assert selection.client.post(BASE + "/xmp", headers=selection.headers,
                                 json={"base_revision": 4}).status_code == 409
    assert not selection.calls


def test_interrupted_copy_can_be_inspected_after_restart(selection):
    manager = selection.manager
    job_id = "a" * 32
    write_json(manager.root / f"{job_id}.json", {"id": job_id, "run_id": "legacy-run", "status": "running"})
    assert manager.get(job_id)["status"] == "interrupted"
    assert read_json(manager.root / f"{job_id}.json")["status"] == "interrupted"
