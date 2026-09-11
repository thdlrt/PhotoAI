from __future__ import annotations

from types import SimpleNamespace

from landscape_culler import ai_install_worker
from landscape_culler.ai_install_worker import (
    _SmokeTelemetry,
    install_models_and_smoke,
)
from landscape_culler.progress import parse_progress_line


def test_smoke_telemetry_reports_current_model_and_checkpoint(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    telemetry = _SmokeTelemetry(9)
    telemetry.working("DINOv2 Base", "加载视觉特征模型到 GPU")
    telemetry.advance("DINOv2 Base", "视觉特征实测通过")
    telemetry.finish(True)

    events = [
        event
        for line in capsys.readouterr().out.splitlines()
        if (event := parse_progress_line(line)) is not None
    ]
    working = next(
        event for event in events if event.get("detail") == "加载视觉特征模型到 GPU"
    )
    checkpoint = next(
        event for event in events if event.get("detail") == "视觉特征实测通过"
    )
    assert working["current_resource"] == "DINOv2 Base"
    assert working["current"] == 0
    assert checkpoint["current"] == 1
    assert events[-1]["event"] == "phase_end"


def test_cached_profile_starts_owned_ollama_before_smoke(
    tmp_path, monkeypatch
) -> None:
    # Register these keys with monkeypatch before the worker mutates them so
    # the process environment is restored for the remainder of the suite.
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", "test-sentinel")
    monkeypatch.setenv("HF_HUB_OFFLINE", "test-sentinel")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "test-sentinel")
    monkeypatch.setenv("UV_OFFLINE", "test-sentinel")
    layout = SimpleNamespace(
        root=tmp_path / "content",
        models=tmp_path / "content" / "models",
        state=tmp_path / "content" / "state",
    )
    order: list[str] = []

    monkeypatch.setattr(
        ai_install_worker,
        "resolve_content_root",
        lambda *_args, **_kwargs: layout,
    )
    monkeypatch.setattr(ai_install_worker, "apply_runtime_environment", lambda _layout: None)
    monkeypatch.setattr(
        ai_install_worker,
        "configure_model_profile",
        lambda *_args, **_kwargs: {"profiles": [{"id": "16gb", "ready": True}]},
    )
    monkeypatch.setattr(
        ai_install_worker,
        "ensure_owned_ollama",
        lambda *_args: order.append("ollama"),
    )

    def smoke(_layout, _profile):
        assert order == ["ollama"]
        order.append("smoke")
        return {"passed": True}

    monkeypatch.setattr(ai_install_worker, "_run_smoke", smoke)
    monkeypatch.setattr(ai_install_worker, "shutdown_owned_ollama", lambda: order.append("shutdown"))

    result = tmp_path / "smoke.json"
    install_models_and_smoke(layout.root, "16gb", result)

    assert order == ["ollama", "smoke", "shutdown"]


def test_offline_import_never_enables_model_downloads(tmp_path, monkeypatch) -> None:
    for name in (
        "PHOTO_AI_CONTENT_ROOT",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "UV_OFFLINE",
    ):
        monkeypatch.setenv(name, "test-sentinel")
    layout = SimpleNamespace(
        root=tmp_path / "content",
        models=tmp_path / "content" / "models",
        state=tmp_path / "content" / "state",
    )
    configured: dict[str, object] = {}
    monkeypatch.setattr(
        ai_install_worker,
        "resolve_content_root",
        lambda *_args, **_kwargs: layout,
    )
    monkeypatch.setattr(ai_install_worker, "apply_runtime_environment", lambda _layout: None)

    def configure(*_args, **kwargs):
        configured.update(kwargs)
        assert ai_install_worker.os.environ["HF_HUB_OFFLINE"] == "1"
        assert ai_install_worker.os.environ["TRANSFORMERS_OFFLINE"] == "1"
        assert ai_install_worker.os.environ["UV_OFFLINE"] == "1"
        return {"profiles": [{"id": "16gb", "ready": True}]}

    monkeypatch.setattr(ai_install_worker, "configure_model_profile", configure)
    monkeypatch.setattr(ai_install_worker, "ensure_owned_ollama", lambda *_args: None)
    monkeypatch.setattr(ai_install_worker, "_run_smoke", lambda *_args: {"passed": True})
    monkeypatch.setattr(ai_install_worker, "shutdown_owned_ollama", lambda: None)

    result = tmp_path / "offline-smoke.json"
    install_models_and_smoke(layout.root, "16gb", result, offline=True)

    assert configured["allow_download"] is False
