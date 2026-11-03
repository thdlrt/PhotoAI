import hashlib
import json

import pytest

from landscape_culler import diagnostics
from landscape_culler.constants import AI_WORKER_CODE_VERSION
from landscape_culler.util import write_json
from landscape_culler.version import PRODUCT_VERSION
from test_web import _client


def _manifest(root, **changes):
    path = root / "release-manifest.json"
    write_json(path, dict(schema_version=1, product="PhotoAI", version=PRODUCT_VERSION,
        worker_code_version=AI_WORKER_CODE_VERSION, built_at="2026-10-10T03:00:00+00:00", **changes))
    path.with_suffix(".json.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest(), encoding="utf-8")
    return path


def test_same_version_rebuilds_have_distinct_read_only_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics.ai_runtime, "release_resources_root", lambda: tmp_path)
    path = _manifest(tmp_path)
    before = path.read_bytes()
    first = diagnostics.build_identity()
    assert first["status"] == "identified" and len(first["id"]) == 12
    assert path.read_bytes() == before
    payload = json.loads(before)
    payload["built_at"] = "2026-10-10T04:00:00+00:00"
    write_json(path, payload)
    path.with_suffix(".json.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest(), encoding="utf-8")
    assert diagnostics.build_identity()["id"] != first["id"]


@pytest.mark.parametrize("kind", ["missing", "damaged", "mismatch"])
def test_missing_or_invalid_build_metadata_is_not_claimed_verified(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(diagnostics.ai_runtime, "release_resources_root", lambda: tmp_path)
    if kind != "missing":
        path = _manifest(tmp_path)
        if kind == "damaged":
            path.write_bytes(b"bad metadata")
        else:
            payload = json.loads(path.read_bytes())
            payload["worker_code_version"] = "worker-v1-old"
            write_json(path, payload)
            path.with_suffix(".json.sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest(), encoding="utf-8")
    assert diagnostics.build_identity()["status"] == {"missing": "unavailable", "damaged": "invalid", "mismatch": "mismatch"}[kind]


def test_api_diagnostics_excludes_credentials_paths_and_job_messages(tmp_path, monkeypatch):
    from landscape_culler.vision_provider import settings_path
    client, data, raw = _client(tmp_path, monkeypatch)
    secret = "private-api-key-do-not-copy"
    write_json(settings_path(data), dict(mode="cloud", api_key=secret, base_url=f"https://{secret}.invalid",
        model=secret, error=secret))
    client.app.state.jobs.jobs["sensitive-job"] = dict(status="failed", kind="develop", title=secret, message=secret, path=str(raw))
    before = {p: p.read_bytes() for p in data.rglob("*.json")}
    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    report = response.json()
    assert report["vision_mode"] == "cloud" and report["jobs"]["failed"] == 1
    assert secret not in response.text and str(raw) not in report["copy_text"]
    assert "worker_expected" in report and report["copy_text"].startswith("照片选片诊断摘要")
    assert all(p.read_bytes() == value for p, value in before.items())
    assert "build" in client.get("/api/app-updates").json()


@pytest.mark.parametrize("installed", [AI_WORKER_CODE_VERSION, "worker-v1-old"])
def test_report_distinguishes_installed_worker_from_required_version(tmp_path, monkeypatch, installed):
    engine = tmp_path / "engine"
    write_json(engine / "engine.json", dict(worker_code_version=installed, profile_id="8gb"))
    monkeypatch.setattr(diagnostics.ai_runtime, "active_engine", lambda _: engine)
    report = diagnostics.diagnostic_report(tmp_path, object(), configured=True, job_counts={})
    assert report["worker_installed"] == installed
    assert report["worker_status"] == ("current" if installed == AI_WORKER_CODE_VERSION else "update_required")
    assert report["profile"] == "8gb"
