from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import landscape_culler.offline_bundle as offline
from landscape_culler.ai_runtime import NvidiaStatus, ReleaseResources, active_engine
from landscape_culler.content_root import initialize_content_root
from landscape_culler.model_resources import MODEL_SPECS, PROFILE_SPECS


def _layout(tmp_path: Path):
    return initialize_content_root(
        tmp_path / "content",
        install_dir=tmp_path / "program",
        apply_environment=False,
        persist_registry=False,
    )


def _bundle(tmp_path: Path, *, extra: dict[str, bytes] | None = None) -> Path:
    members = {
        "payload/engine/venv/pyvenv.cfg": b"home = C:\\old\\python\n",
        "payload/engine/venv/Scripts/python.exe": b"MZpython",
        (
            "payload/python/"
            f"cpython-{offline.PYTHON_RUNTIME_VERSION}-windows-x86_64-none/python.exe"
        ): b"MZmanaged",
        "payload/models/ollama/blobs/sha256-test": b"model",
        "payload/models/ollama/manifests/registry.ollama.ai/library/qwen3-vl/8b-instruct-q4_K_M": b"{}",
        "payload/tools/ollama-v0.33.2/ollama.exe": b"MZollama",
    }
    for resource_id in PROFILE_SPECS["16gb"]["model_ids"]:
        spec = MODEL_SPECS[resource_id]
        if spec["provider"] != "huggingface":
            continue
        repository = "models--" + str(spec["repo_id"]).replace("/", "--")
        members[
            "payload/models/huggingface/hub/"
            f"{repository}/snapshots/{spec['revision']}/fixture.bin"
        ] = b"fixture"
    members.update(extra or {})
    manifest = {
        "schema_version": offline.OFFLINE_BUNDLE_SCHEMA,
        "application": "PhotoAI",
        "profile_id": "16gb",
        "product_version": offline.PRODUCT_VERSION,
        "engine_version": offline.AI_ENGINE_VERSION,
        "python_version": offline.PYTHON_RUNTIME_VERSION,
        "architecture": "windows-x86_64",
        "created_at": "2026-09-04T00:00:00+00:00",
        "file_count": len(members),
        "payload_uncompressed_bytes": sum(len(value) for value in members.values()),
        "resources": list(PROFILE_SPECS["16gb"]["model_ids"]),
    }
    target = tmp_path / "PhotoAI-16GB.photoai-offline"
    with zipfile.ZipFile(target, "w", allowZip64=True) as archive:
        archive.writestr(offline.OFFLINE_MANIFEST, json.dumps(manifest))
        for name, value in members.items():
            archive.writestr(name, value)
    return target


def _resources(tmp_path: Path) -> ReleaseResources:
    root = tmp_path / "release"
    wheelhouse = root / "wheels"
    worker_root = root / "ai-worker"
    wheelhouse.mkdir(parents=True)
    worker_root.mkdir()
    uv = root / "uv.exe"
    uv.write_bytes(b"MZuv")
    requirements = root / "ai-requirements.lock"
    requirements.write_text("", encoding="utf-8")
    worker = worker_root / "worker.whl"
    worker.write_bytes(b"wheel")
    digest = hashlib.sha256(worker.read_bytes()).hexdigest()
    worker.with_name(worker.name + ".sha256").write_text(
        f"{digest} *{worker.name}\n",
        encoding="ascii",
    )
    return ReleaseResources(root, uv, requirements, wheelhouse, worker, digest)


def test_inspect_accepts_current_16gb_bundle(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)

    result = offline.inspect_offline_bundle(bundle)

    assert result["profile_id"] == "16gb"
    assert result["file_count"] > 10
    assert result["package_bytes"] == bundle.stat().st_size


def test_inspect_rejects_path_escape(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, extra={"../outside.bin": b"bad"})

    with pytest.raises(offline.AiRuntimeError, match="无效路径"):
        offline.inspect_offline_bundle(bundle)


def test_offline_import_relocates_environment_and_never_enables_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    bundle = _bundle(tmp_path)
    resources = _resources(tmp_path)
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    monkeypatch.setattr(
        offline,
        "_ensure_install_preconditions",
        lambda *_args: NvidiaStatus(True, "RTX Test", 16_384, "580.10", True),
    )
    monkeypatch.setattr(
        offline,
        "ai_runtime_status",
        lambda _layout: {"ready": True, "profile_id": "16gb"},
    )

    def runner(command, environment, _cwd):
        commands.append(command)
        environments.append(dict(environment))
        if "landscape_culler.ai_install_worker" in command:
            smoke = Path(command[command.index("--smoke-result") + 1])
            offline.write_json(smoke, {"status": "passed"})

    result = offline.install_offline_bundle(
        layout,
        bundle,
        resources_root=resources.root,
        runner=runner,
    )

    engine = active_engine(layout)
    assert result["ready"] is True
    assert engine is not None
    assert "-offline-" in engine.name
    assert (engine / "venv" / "Scripts" / "python.exe").is_file()
    config = (engine / "venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    assert str(layout.runtimes / "python") in config
    assert any("--offline" in command for command in commands)
    assert any(command[-1] == "--offline" for command in commands)
    assert all(environment["UV_OFFLINE"] == "1" for environment in environments)
    assert all(environment["HF_HUB_OFFLINE"] == "1" for environment in environments)
    assert not list(layout.temp.glob("offline-import-*"))


def test_failed_offline_smoke_keeps_previous_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    previous = layout.runtimes / "engines" / "previous"
    (previous / "venv" / "Scripts").mkdir(parents=True)
    (previous / "venv" / "Scripts" / "python.exe").write_bytes(b"MZpython")
    offline.write_json(
        previous / offline.ENGINE_MANIFEST,
        {"engine_version": offline.AI_ENGINE_VERSION},
    )
    offline.write_json(previous / offline.SMOKE_RESULT, {"status": "passed"})
    offline._atomic_activate(layout, previous, "16gb")
    bundle = _bundle(tmp_path)
    resources = _resources(tmp_path)
    monkeypatch.setattr(
        offline,
        "_ensure_install_preconditions",
        lambda *_args: NvidiaStatus(True, "RTX Test", 16_384, "580.10", True),
    )

    def runner(command, _environment, _cwd):
        if "landscape_culler.ai_install_worker" in command:
            raise offline.AiRuntimeError("smoke failed")

    with pytest.raises(offline.AiRuntimeError, match="smoke failed"):
        offline.install_offline_bundle(
            layout,
            bundle,
            resources_root=resources.root,
            runner=runner,
        )

    assert active_engine(layout) == previous
    assert previous.is_dir()
