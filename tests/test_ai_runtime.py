from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import landscape_culler.ai_runtime as runtime
from landscape_culler.ai_runtime import (
    AiRuntimeError,
    NvidiaStatus,
    active_engine,
    delete_ai_runtime,
    install_ai_profile,
)
from landscape_culler.content_root import initialize_content_root
from landscape_culler.progress import parse_progress_line


def _release_resources(tmp_path: Path) -> Path:
    root = tmp_path / "release-resources"
    (root / "wheels").mkdir(parents=True)
    (root / "ai-worker").mkdir()
    (root / "uv.exe").write_bytes(b"MZuv")
    (root / "ai-requirements.lock").write_text(
        "example==1 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
    )
    wheel = root / "ai-worker" / "landscape_ai_culler-0.9.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    wheel.with_name(wheel.name + ".sha256").write_text(
        f"{digest}  {wheel.name}\n", encoding="utf-8"
    )
    return root


def _layout(tmp_path: Path):
    return initialize_content_root(
        tmp_path / "content",
        install_dir=tmp_path / "program",
        apply_environment=False,
        persist_registry=False,
    )


def _gpu() -> NvidiaStatus:
    return NvidiaStatus(True, "RTX Test", 16_384, "580.10", True)


def _successful_runner(
    commands: list[list[str]], environments: list[dict[str, str]] | None = None
):
    def run(command, environment, _cwd):
        commands.append(command)
        if environments is not None:
            environments.append(dict(environment))
        if len(command) > 2 and command[1] == "venv":
            engine = Path(command[2]).parent
            python = runtime._venv_python(engine)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"MZpython")
        if "landscape_culler.ai_install_worker" in command:
            result = Path(command[command.index("--smoke-result") + 1])
            runtime.write_json(result, {"schema_version": 1, "status": "passed"})

    return run


def test_install_uses_managed_python_hash_sync_and_atomic_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    resources = _release_resources(tmp_path)
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []
    monkeypatch.setattr(runtime, "detect_nvidia", _gpu)

    status = install_ai_profile(
        layout,
        "16gb",
        resources_root=resources,
        runner=_successful_runner(commands, environments),
    )

    assert status["ready"] is True
    engine = active_engine(layout)
    assert engine is not None
    assert engine.parent == layout.runtimes / "engines"
    assert engine.name.startswith(runtime.AI_ENGINE_VERSION + "-")
    assert not (layout.runtimes / ".staging").exists()
    sync = next(command for command in commands if command[1:3] == ["pip", "sync"])
    assert "--require-hashes" in sync
    assert sync[sync.index("--only-binary") + 1] == ":all:"
    assert "--managed-python" in sync
    install_python = next(
        command for command in commands if command[1:3] == ["python", "install"]
    )
    assert "--no-registry" in install_python
    assert install_python[install_python.index("--mirror") + 1] == (
        runtime.DOMESTIC_PYTHON_MIRROR
    )
    assert sync[sync.index("--default-index") + 1] == runtime.DOMESTIC_PYPI_INDEX
    assert runtime.DOMESTIC_PYTORCH_WHEELS in [
        sync[index + 1]
        for index, value in enumerate(sync[:-1])
        if value == "--find-links"
    ]
    assert "--torch-backend" not in sync
    assert any(
        "国内 npmmirror" in environment.get("PHOTO_AI_COMMAND_DETAIL", "")
        for environment in environments
    )
    assert Path(install_python[install_python.index("--install-dir") + 1]) == (
        layout.runtimes / "python"
    )
    assert (
        json.loads((layout.runtimes / "current.json").read_text(encoding="utf-8"))[
            "profile_id"
        ]
        == "16gb"
    )


def test_managed_python_falls_back_without_discarding_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    resources = _release_resources(tmp_path)
    monkeypatch.setattr(runtime, "detect_nvidia", _gpu)
    commands: list[list[str]] = []
    successful = _successful_runner(commands)
    failed_domestic = False

    def runner(command, environment, cwd):
        nonlocal failed_domestic
        if command[1:3] == ["python", "install"] and "--mirror" in command:
            failed_domestic = True
            commands.append(command)
            raise AiRuntimeError("mirror unavailable")
        successful(command, environment, cwd)

    status = install_ai_profile(
        layout,
        "8gb",
        resources_root=resources,
        runner=runner,
    )

    python_attempts = [
        command for command in commands if command[1:3] == ["python", "install"]
    ]
    assert failed_domestic is True
    assert len(python_attempts) == 2
    assert "--mirror" in python_attempts[0]
    assert "--mirror" not in python_attempts[1]
    assert status["ready"] is True


def test_failed_replacement_does_not_publish_or_remove_current_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    resources = _release_resources(tmp_path)
    monkeypatch.setattr(runtime, "detect_nvidia", _gpu)
    install_ai_profile(
        layout,
        "8gb",
        resources_root=resources,
        runner=_successful_runner([]),
    )
    before = active_engine(layout)

    def fail_on_smoke(command, environment, cwd):
        _successful_runner([])(command, environment, cwd)
        if "landscape_culler.ai_install_worker" in command:
            raise AiRuntimeError("smoke failed")

    with pytest.raises(AiRuntimeError, match="smoke failed"):
        install_ai_profile(
            layout,
            "8gb",
            resources_root=resources,
            runner=fail_on_smoke,
        )

    assert active_engine(layout) == before
    assert before is not None and before.is_dir()
    assert list((layout.runtimes / "engines").iterdir()) == [before]


def test_activation_failure_restores_pointer_and_profile_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    resources = _release_resources(tmp_path)
    monkeypatch.setattr(runtime, "detect_nvidia", _gpu)
    install_ai_profile(
        layout,
        "8gb",
        resources_root=resources,
        runner=_successful_runner([]),
    )
    before = active_engine(layout)
    settings_path = layout.state / "model-resources" / "settings.json"
    settings_before = settings_path.read_bytes()

    def fail_after_pointer_write(layout_arg, engine, profile_id):
        runtime.write_json(
            runtime._engine_pointer(layout_arg),
            {"engine_path": "engines/incomplete", "profile_id": profile_id},
        )
        raise OSError("pointer publish failed")

    monkeypatch.setattr(runtime, "_atomic_activate", fail_after_pointer_write)
    with pytest.raises(OSError, match="pointer publish failed"):
        install_ai_profile(
            layout,
            "16gb",
            resources_root=resources,
            runner=_successful_runner([]),
        )

    assert active_engine(layout) == before
    assert settings_path.read_bytes() == settings_before
    assert before is not None
    assert list((layout.runtimes / "engines").iterdir()) == [before]


def test_delete_runtime_preserves_models_projects_and_styles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    resources = _release_resources(tmp_path)
    monkeypatch.setattr(runtime, "detect_nvidia", _gpu)
    (layout.models / "keep.bin").write_bytes(b"model")
    (layout.projects / "keep.json").write_text("{}", encoding="utf-8")
    (layout.styles / "keep.xmp").write_text("style", encoding="utf-8")
    install_ai_profile(
        layout,
        "8gb",
        resources_root=resources,
        runner=_successful_runner([]),
    )

    status = delete_ai_runtime(layout)

    assert status["ready"] is False
    assert (layout.models / "keep.bin").read_bytes() == b"model"
    assert (layout.projects / "keep.json").is_file()
    assert (layout.styles / "keep.xmp").is_file()


def test_profile_readiness_requires_its_own_smoke_test(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(runtime, "ai_runtime_status", lambda _layout: {
        "ready": True, "smoke_test": {"status": "passed", "profile_id": "8gb"}
    })
    from landscape_culler import model_resources
    monkeypatch.setattr(model_resources, "model_resources_status", lambda *_args, **_kwargs: {
        "profiles": [{"id": "8gb", "ready": True}, {"id": "16gb", "ready": True}],
        "components": [{"id": "ollama", "verified": True}],
    })
    status = runtime.ai_resources_status(layout)
    profiles = {p["id"]: p for p in status["profiles"]}
    assert profiles["8gb"]["layers"]["smoke_test"] is True
    assert profiles["16gb"]["layers"]["smoke_test"] is False
    assert profiles["16gb"]["ready"] is False


def test_old_driver_is_rejected_before_any_release_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    monkeypatch.setattr(
        runtime,
        "detect_nvidia",
        lambda: NvidiaStatus(True, "RTX Test", 16_384, "577.1", False),
    )

    with pytest.raises(AiRuntimeError, match="R580"):
        install_ai_profile(layout, "16gb", resources_root=tmp_path / "missing")
    assert not (layout.runtimes / "current.json").exists()


def test_quiet_install_command_emits_one_second_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    environment = os.environ.copy()
    environment.update(
        PHOTO_AI_COMMAND_PHASE="dependencies",
        PHOTO_AI_COMMAND_LABEL="安装固定 AI 依赖",
        PHOTO_AI_COMMAND_RESOURCE="Torch",
        PHOTO_AI_COMMAND_DETAIL="安装固定依赖",
        PHOTO_AI_COMMAND_WATCH_ROOTS="[]",
    )

    runtime._run(
        [sys.executable, "-c", "import time; time.sleep(1.25)"],
        environment,
        tmp_path,
    )

    events = [
        event
        for line in capsys.readouterr().out.splitlines()
        if (event := parse_progress_line(line)) is not None
    ]
    assert events
    heartbeat = events[-1]
    assert heartbeat["phase"] == "dependencies"
    assert heartbeat["total"] == 0
    assert heartbeat["current_resource"] == "Torch"
    assert heartbeat["elapsed_seconds"] >= 1.0
    assert heartbeat["heartbeat_at"]


def test_verbose_install_output_is_not_throttled_by_cache_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watched = tmp_path / "cache"
    watched.mkdir()
    environment = os.environ.copy()
    environment.update(
        PHOTO_AI_COMMAND_PHASE="dependencies",
        PHOTO_AI_COMMAND_LABEL="安装固定 AI 依赖",
        PHOTO_AI_COMMAND_RESOURCE="Torch",
        PHOTO_AI_COMMAND_DETAIL="安装固定依赖",
        PHOTO_AI_COMMAND_WATCH_ROOTS=json.dumps([str(watched)]),
    )
    real_rglob = Path.rglob
    scans = 0

    def counted_rglob(path: Path, pattern: str):
        nonlocal scans
        if path == watched:
            scans += 1
        return real_rglob(path, pattern)

    monkeypatch.setattr(Path, "rglob", counted_rglob)
    runtime._run(
        [
            sys.executable,
            "-c",
            "import sys; [print(i, flush=True) for i in range(200)]",
        ],
        environment,
        tmp_path,
    )

    assert scans == 1
