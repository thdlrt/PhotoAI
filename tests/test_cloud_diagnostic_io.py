import json
import sys

import pytest

from landscape_culler import vision_provider as vp
from test_vision_provider import config, mock_transport
from test_web import _client


class BrokenDesktopConsole:
    def write(self, value):
        raise OSError(22, "Invalid argument")

    def flush(self):
        raise OSError(22, "Invalid argument")


def test_good_response_survives_invalid_desktop_output_handle(config, monkeypatch):
    root, _ = config
    monkeypatch.delenv("PHOTO_AI_LOGS_DIR", raising=False)
    mock_transport(monkeypatch, [{"choices": [{"finish_reason": "stop", "message": {
        "content": "red", "reasoning_content": "private reasoning sentinel"}}]}])
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", BrokenDesktopConsole())
        result = vp.chat(vp.load_config(root), {"messages": []}, data_dir=root)
    assert result["message"]["content"] == "red"
    file = next((root / "web/logs/vision-responses").glob("*.json"))
    diagnostic = json.loads(file.read_text(encoding="utf-8"))
    assert diagnostic["finish_reason"] == "stop"
    assert "secret-test" not in file.read_text(encoding="utf-8")
    assert "private reasoning sentinel" not in file.read_text(encoding="utf-8")


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_unwritable_console_and_disk_do_not_replace_response_result(config, monkeypatch, finish_reason):
    root, _ = config
    mock_transport(monkeypatch, [{"choices": [{"finish_reason": finish_reason, "message": {"content": "red"}}]}])

    def unwritable(*_args, **_kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(vp, "print", unwritable, raising=False)
    monkeypatch.setattr(vp, "write_json", unwritable)
    if finish_reason == "length":
        with pytest.raises(vp.CloudResponseError, match="输出额度截断"):
            vp.chat(vp.load_config(root), {"messages": []}, data_dir=root)
    else:
        assert vp.chat(vp.load_config(root), {"messages": []}, data_dir=root)["message"]["content"] == "red"


def test_connection_api_returns_success_with_broken_console_and_log_disk(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOTO_AI_CONTENT_ROOT", raising=False)
    monkeypatch.setattr(vp, "_protect", lambda value, decrypt=False: "sealed")
    client, data, _ = _client(tmp_path, monkeypatch)
    vp.save_config(data, {"mode": "cloud", "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
                         "api_key": "sentinel", "upload_consent": True, "thinking_effort": "high"})
    calls = mock_transport(monkeypatch, [{"choices": [{"finish_reason": "stop", "message": {"content": "red"}}]}])

    def unwritable(*_args, **_kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(vp, "print", unwritable, raising=False)
    monkeypatch.setattr(vp, "write_json", unwritable)
    headers = {"X-Photo-AI-Token": client.get("/api/bootstrap").json()["token"]}
    response = client.post("/api/vision-provider/test", headers=headers)
    assert response.status_code == 200 and response.json()["ok"], response.text
    assert calls[0]["reasoning_effort"] == "high"
