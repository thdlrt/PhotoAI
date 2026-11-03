from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from landscape_culler.agent_connection import (
    CLI_HEADER, acquire_service_lock, load_connection, publish_connection,
    release_service_lock, remove_connection, validate_origin,
)
from landscape_culler.service import ServiceApp


def test_private_connection_roundtrip_and_replacement_cleanup(tmp_path):
    path = tmp_path / "中文 state" / "cli-session.json"
    secret = "private-cli-key-should-never-be-printed"
    publish_connection(path, origin="http://127.0.0.1:23456", pid=123, instance_id="first",
                       token=secret, content_root=tmp_path, data_dir=path.parent)
    record = load_connection(path, verify_health=False)
    assert record["headers"] == {CLI_HEADER: secret}
    assert secret not in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    remove_connection(path, "different-instance")
    assert path.exists()
    remove_connection(path, "first")
    assert not path.exists()


@pytest.mark.parametrize("origin", ["https://127.0.0.1:1234", "http://example.com:1234",
    "http://127.0.0.1:1234/path", "http://user:secret@localhost:1234", "http://localhost:1234?key=secret",
    "http://localhost", "http://127.0.0.1:0"])
def test_discovery_rejects_non_loopback_or_credential_urls(origin):
    with pytest.raises(ValueError):
        validate_origin(origin)


def test_live_discovery_probes_without_credentials_and_rejects_reused_port(tmp_path):
    observed = []
    health = {"protocol": "PHOTO_AI_SERVICE/1", "pid": 123, "instance_id": "current"}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(dict(self.headers))
            body = json.dumps(health).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        path = tmp_path / "cli-session.json"
        publish_connection(path, origin=f"http://127.0.0.1:{server.server_port}", pid=123,
            instance_id="current", token="test-private-key-12345", content_root=tmp_path, data_dir=tmp_path)
        assert load_connection(path)["pid"] == 123
        assert CLI_HEADER not in observed[0] and "Authorization" not in observed[0]
        health["instance_id"] = "other-service"
        with pytest.raises(ConnectionError, match="身份"):
            load_connection(path)
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_only_one_service_can_own_data_directory(tmp_path):
    first = acquire_service_lock(tmp_path / "service.lock")
    try:
        with pytest.raises(RuntimeError, match="已有服务"):
            acquire_service_lock(tmp_path / "service.lock")
    finally:
        release_service_lock(first)
    second = acquire_service_lock(tmp_path / "service.lock")
    release_service_lock(second)


def test_cli_auth_is_independent_of_one_time_browser_auth_and_shutdown():
    inner = FastAPI()
    @inner.get("/api/sample")
    def sample(): return {"ok": True}
    wrapped = ServiceApp(inner, bootstrap_token="browser-bootstrap", cli_token="cli-key")
    client = TestClient(wrapped)
    assert client.get("/api/sample").status_code == 401
    assert client.get("/api/sample", headers={CLI_HEADER:"wrong"}).status_code == 401
    for _ in range(2):
        response = client.get("/api/sample", headers={CLI_HEADER:"cli-key"})
        assert response.status_code == 200 and "set-cookie" not in response.headers
    assert client.post("/api/service/shutdown", headers={CLI_HEADER:"cli-key"}).status_code == 401
    assert client.get("/api/sample", headers={"Authorization":"Bearer browser-bootstrap"}).status_code == 200
    assert client.get("/api/sample").status_code == 200
