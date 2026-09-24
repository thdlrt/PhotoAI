import json
from pathlib import Path
import pytest
from landscape_culler import vision_provider as vp
from test_web import _client, RUN_ID


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_AI_CONTENT_ROOT", raising=False)
    monkeypatch.setattr(vp, "_protect", lambda value, decrypt=False: value[7:] if decrypt else "sealed:" + value)
    values = dict(mode="cloud", base_url=vp.DEFAULT_URL, model="vision-test", api_key="secret-test",
                  upload_consent=True, timeout=30)
    vp.save_config(tmp_path, values)
    return tmp_path, values


def test_private_settings_and_validation(config):
    root, values = config
    assert vp.public_config(root)["has_key"]
    assert "secret-test" not in json.dumps(vp.public_config(root))
    vp.save_config(root, {**values, "api_key": "", "mode": "local"})
    assert vp.load_config(root)["key"] == "sealed:secret-test"
    for url in ["http://example.com/v1", "https://key:secret@example.com/v1", "https://host/v1?key=secret"]:
        with pytest.raises(ValueError):
            vp.save_config(root, {**values, "base_url": url})
    with pytest.raises(ValueError):
        vp.save_config(root, {**values, "upload_consent": False})


def test_transport_multimodal_and_provider_cache(config, monkeypatch):
    root, _ = config
    captured = {}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}]}'
    class Opener:
        def open(self, request, timeout):
            captured.update(url=request.full_url, body=json.loads(request.data), headers=request.headers)
            return Response()
    monkeypatch.setattr(vp.urllib.request, "build_opener", lambda *args: Opener())
    from landscape_culler.group_critic import OllamaGroupCritic
    critic = OllamaGroupCritic(root)
    critic.ensure_ready()
    result = critic._request("/api/chat", {"messages": [{"role": "user", "content": "rate", "images": ["abc"]}], "format": {"type": "object"}})
    assert json.loads(result["message"]["content"])["ok"]
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["model"] == "vision-test"
    assert captured["body"]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    critic.unload()  # Never sends local Ollama management calls to the cloud.
    assert critic.provider_identity != "local"


def test_settings_api_does_not_expose_key_or_change_legacy_project(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_AI_CONTENT_ROOT", raising=False)
    monkeypatch.setattr(vp, "_protect", lambda value, decrypt=False: "sealed")
    client, data, raw = _client(tmp_path, monkeypatch)
    source = data / "runs" / RUN_ID / "results.json"
    before = source.read_bytes()
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    body = dict(mode="cloud", base_url=vp.DEFAULT_URL, model="vision", api_key="private", upload_consent=True)
    assert client.put("/api/vision-provider", json=body).status_code == 403
    response = client.put("/api/vision-provider", json=body, headers=headers)
    assert response.status_code == 200 and "private" not in response.text
    assert client.get(f"/api/runs/{RUN_ID}").status_code == 200
    assert source.read_bytes() == before and raw.read_bytes() == b"raw"


def test_dpapi_round_trip(tmp_path, monkeypatch):
    import os
    if os.name != "nt": pytest.skip("Windows only")
    encrypted = vp._protect("test-secret-中文")
    assert "test-secret" not in encrypted
    assert vp._protect(encrypted, decrypt=True) == "test-secret-中文"


def test_cloud_install_skips_ollama_and_keeps_six_local_models(config, monkeypatch):
    root, values = config
    from landscape_culler import model_resources as resources
    def status(runtime, key, **kwargs):
        return {"id": key, "label": key, "installed": True, "verified": True,
                "installed_bytes": 0, "issues": []}
    monkeypatch.setattr(resources, "_resource_status", status)
    monkeypatch.setattr(resources, "_gpu_status", lambda: {})
    monkeypatch.setattr(resources, "_ollama_component_status", lambda *_: {"verified": False})
    monkeypatch.setattr(resources, "_download_ollama_component", lambda *_: pytest.fail("must not install Ollama"))
    result = resources.configure_model_profile("16gb", root / "models", root, publish_settings=False)
    profile = next(p for p in result["profiles"] if p["id"] == "16gb")
    assert profile["ready"] and profile["model_count"] == 6
    assert all(resources.MODEL_SPECS[k]["provider"] != "ollama" for k in profile["model_ids"])
    vp.save_config(root, {**values, "mode": "local"})
    profile = resources.model_resources_status(root / "models", root)["profiles"][0]
    assert not profile["ready"] and profile["model_count"] == 7
