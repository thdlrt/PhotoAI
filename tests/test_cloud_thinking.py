import hashlib
import json

import pytest

from landscape_culler import vision_provider as vp
from landscape_culler.develop import _develop_checkpoint_context
from test_vision_provider import config, mock_transport
from test_web import RUN_ID, _client


@pytest.mark.parametrize("effort", ["off", "low", "high", "max", "auto"])
def test_deepseek_saved_strength_overrides_worker_flag(config, monkeypatch, effort):
    root, values = config
    public = vp.save_config(root, {**values, "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
                                    "thinking_effort": effort})
    assert public["thinking_effort"] == effort
    assert vp.public_config(root)["thinking_effort"] == effort
    calls = mock_transport(monkeypatch, [{"choices": [{"finish_reason": "stop", "message": {"content": "red"}}]}])
    vp.chat(vp.load_config(root), {"think": False, "messages": [{"content": "color", "images": [vp.TEST_IMAGE]}],
                                 "options": {"num_predict": 256}})
    body = calls[0]
    assert "think" not in body
    if effort == "auto":
        assert "thinking" not in body and "reasoning_effort" not in body
    else:
        assert body["thinking"] == {"type": "disabled" if effort == "off" else "enabled"}
        assert body.get("reasoning_effort") == (None if effort == "off" else effort)
    assert body["max_tokens"] == 256 + vp.THINKING_BUDGETS.get(effort, 0)
    assert ("temperature" in body) == (effort == "off")


@pytest.mark.parametrize("effort", ["off", "low", "medium", "high", "max", "auto"])
def test_bailian_strength_uses_thinking_budget(config, monkeypatch, effort):
    root, values = config
    vp.save_config(root, {**values, "model": "qwen3-vl-plus", "thinking_effort": effort})
    calls = mock_transport(monkeypatch, [{"choices": [{"message": {"content": "red"}}]}])
    vp.chat(vp.load_config(root), {"think": False, "messages": []})
    body = calls[0]
    assert "thinking" not in body and "reasoning_effort" not in body
    if effort == "auto":
        assert "enable_thinking" not in body and "thinking_budget" not in body
    else:
        assert body["enable_thinking"] == (effort != "off")
        if effort != "off":
            assert body["thinking_budget"] == vp.THINKING_BUDGETS[effort]
            assert body["max_tokens"] >= body["thinking_budget"] + 1400


@pytest.mark.parametrize("model,effort,expected", [
    ("custom-reasoning-vision", "medium", "medium"), ("gpt-5.1", "off", "none"),
    ("o3", "high", "high"), ("gpt-6-sol", "max", "max"),
])
def test_standard_compatible_reasoning_mapping(model, effort, expected):
    body = vp.thinking_request({"base_url": "https://api.openai.com/v1", "model": model,
                               "thinking_effort": effort}, 3000)
    assert body["reasoning_effort"] == expected
    assert "thinking" not in body and "enable_thinking" not in body
    if model.startswith(("gpt-", "o3")):
        assert body["max_completion_tokens"] >= 3000 and "max_tokens" not in body


@pytest.mark.parametrize("invalid", ["medium", "bogus", None, [], 3])
def test_unsupported_strength_does_not_change_configuration(config, invalid):
    root, values = config
    vp.save_config(root, {**values, "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "thinking_effort": "high"})
    before = vp.settings_path(root).read_bytes()
    with pytest.raises(ValueError):
        vp.save_config(root, {**values, "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "thinking_effort": invalid})
    assert vp.settings_path(root).read_bytes() == before


def test_thinking_only_model_has_no_off_switch(config):
    root, values = config
    capabilities = vp.thinking_capabilities({"base_url": vp.DEFAULT_URL, "model": "qwen3-vl-32b-thinking"})
    assert "off" not in {option["value"] for option in capabilities["options"]}
    with pytest.raises(ValueError):
        vp.save_config(root, {**values, "model": "qwen3-vl-32b-thinking", "thinking_effort": "off"})


def test_legacy_config_is_read_only_and_default_identity_is_preserved(config):
    root, values = config
    vp.save_config(root, {**values, "base_url": "https://api.deepseek.com", "model": "deepseek-flash"})
    saved = vp.load_config(root)
    saved.pop("thinking_effort")
    vp.write_json(vp.settings_path(root), saved)
    before = vp.settings_path(root).read_bytes()
    old_identity = hashlib.sha256(json.dumps([saved["base_url"], saved["model"]]).encode()).hexdigest()[:20]
    assert vp.public_config(root)["thinking_effort"] == "off"
    assert vp.identity(saved) == old_identity
    assert vp.identity({**saved, "thinking_effort": "high"}) != old_identity
    assert vp.identity({**saved, "key": "another-private-key"}) == old_identity
    assert vp.settings_path(root).read_bytes() == before


def test_configuration_and_connection_api_use_saved_strength_preserve_projects(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_AI_CONTENT_ROOT", raising=False)
    monkeypatch.setattr(vp, "_protect", lambda value, decrypt=False: "sealed")
    client, data, raw = _client(tmp_path, monkeypatch)
    source = data / "runs" / RUN_ID / "results.json"
    before = source.read_bytes()
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    body = {"mode": "cloud", "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "api_key": "private sentinel", "upload_consent": True, "thinking_effort": "high"}
    assert client.put("/api/vision-provider", json=body, headers=headers).status_code == 200
    response = client.get("/api/vision-provider").json()
    assert response["thinking_effort"] == "high" and "private sentinel" not in json.dumps(response)
    options = client.get("/api/vision-provider/thinking-options", params={key: body[key] for key in ("base_url", "model")}).json()
    assert {option["value"] for option in options["options"]} == {"off", "low", "high", "max", "auto"}
    checkpoint_context = _develop_checkpoint_context(source.parent)
    captured = mock_transport(monkeypatch, [{"choices": [{"message": {"content": "red"}}]}])
    assert client.post("/api/vision-provider/test", headers=headers).status_code == 200
    assert captured[0]["thinking"] == {"type": "enabled"} and captured[0]["reasoning_effort"] == "high"
    assert captured[0]["max_tokens"] == 8448
    body["thinking_effort"] = "low"
    assert client.put("/api/vision-provider", json=body, headers=headers).status_code == 200
    assert _develop_checkpoint_context(source.parent) != checkpoint_context
    assert source.read_bytes() == before and raw.read_bytes() == b"raw"


def test_save_without_strength_preserves_existing_selection(config):
    root, values = config
    values = {**values, "base_url": "https://api.deepseek.com", "model": "deepseek-flash"}
    vp.save_config(root, {**values, "thinking_effort": "high"})
    assert vp.save_config(root, values)["thinking_effort"] == "high"


def test_default_public_options_match_displayed_default_model(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_AI_CONTENT_ROOT", raising=False)
    public = vp.public_config(tmp_path)
    assert public["model"] == "qwen3-vl-plus" and public["thinking_effort"] == "off"
    assert public["thinking_capabilities"]["family"] == "bailian"


def test_unknown_model_default_does_not_send_unsupported_extra_parameters():
    body = vp.thinking_request({"base_url": "https://custom.example/v1", "model": "legacy-vision-model"}, 1400)
    assert body == {"max_tokens": 1400}
