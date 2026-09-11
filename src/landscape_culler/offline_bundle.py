"""Portable, one-file offline AI environment bundles.

The bundle is deliberately separate from the small desktop installer.  It
contains only PhotoAI-owned runtime/model payload and is imported into a
staged Content Root before the normal CUDA/model smoke gate atomically
publishes the environment.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .ai_runtime import (
    AI_RUNTIME_SCHEMA,
    ENGINE_MANIFEST,
    SMOKE_RESULT,
    AiRuntimeError,
    CommandRunner,
    _atomic_activate,
    _ensure_install_preconditions,
    _file_snapshot,
    _restore_file_snapshot,
    _run,
    _venv_python,
    active_engine,
    ai_runtime_status,
    locate_release_resources,
)
from .content_root import ContentRootLayout, runtime_environment
from .model_resources import MODEL_SPECS, PROFILE_SPECS
from .progress import emit_progress, heartbeat_timestamp, phase_end, phase_start
from .util import read_json, write_json
from .version import AI_ENGINE_VERSION, PRODUCT_VERSION, PYTHON_RUNTIME_VERSION

OFFLINE_BUNDLE_SCHEMA = 1
OFFLINE_BUNDLE_SUFFIX = ".photoai-offline"
OFFLINE_MANIFEST = "offline-manifest.json"
OFFLINE_PROFILE = "16gb"
MAX_MEMBER_COUNT = 100_000
MAX_UNCOMPRESSED_BYTES = 30 * 1024**3
COPY_BLOCK_BYTES = 4 * 1024**2
STORE_THRESHOLD_BYTES = 256 * 1024**2

_PAYLOAD_PREFIXES = (
    "payload/engine/venv/",
    "payload/python/",
    "payload/models/huggingface/hub/",
    "payload/models/ollama/",
    "payload/tools/ollama-v0.33.2/",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_member_name(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise AiRuntimeError(f"离线包包含无效路径：{value}")
    return path.as_posix()


def _is_allowed_payload(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _PAYLOAD_PREFIXES)


def _archive_member(
    archive: zipfile.ZipFile,
    source: Path,
    destination: str,
) -> None:
    target = _safe_member_name(destination)
    info = zipfile.ZipInfo(target)
    modified = time.localtime(source.stat().st_mtime)[:6]
    info.date_time = tuple(max(value, 1980 if index == 0 else 1) for index, value in enumerate(modified))  # type: ignore[assignment]
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = (
        zipfile.ZIP_STORED
        if source.stat().st_size >= STORE_THRESHOLD_BYTES
        else zipfile.ZIP_DEFLATED
    )
    with source.open("rb") as incoming, archive.open(info, "w", force_zip64=True) as outgoing:
        shutil.copyfileobj(incoming, outgoing, length=COPY_BLOCK_BYTES)


def _tree_members(source: Path, prefix: str) -> Iterable[tuple[Path, str]]:
    if not source.is_dir():
        raise AiRuntimeError(f"离线包源目录不存在：{source}")
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        yield path, f"{prefix.rstrip('/')}/{relative}"


def _hf_snapshot_members(
    runtime_root: Path, resource_id: str
) -> Iterable[tuple[Path, str]]:
    from .model_resources import local_hf_model_path

    spec = MODEL_SPECS[resource_id]
    try:
        snapshot = local_hf_model_path(runtime_root, resource_id)
    except FileNotFoundError:
        legacy = str(spec.get("legacy_path") or "").strip()
        snapshot = runtime_root / legacy if legacy else Path()
        if not legacy or not snapshot.is_dir():
            raise AiRuntimeError(f"离线模型源不完整：{resource_id}") from None
    repository = "models--" + str(spec["repo_id"]).replace("/", "--")
    prefix = (
        f"payload/models/huggingface/hub/{repository}/snapshots/"
        f"{spec['revision']}"
    )
    declared = {Path(str(value)).as_posix() for value in spec.get("files", [])}
    for relative in sorted(declared):
        source = snapshot / relative
        if not source.is_file():
            raise AiRuntimeError(f"离线模型源不完整：{resource_id}/{relative}")
        yield source, f"{prefix}/{relative}"


def _ollama_members(runtime_root: Path) -> Iterable[tuple[Path, str]]:
    from .model_resources import (
        _is_sha256_digest,
        _ollama_blob_path,
        _ollama_manifest_candidates,
        _ollama_models_root,
    )

    spec = MODEL_SPECS["qwen3-vl-8b"]
    manifest = next(
        (
            candidate
            for candidate in _ollama_manifest_candidates(
                runtime_root, str(spec["ollama_name"])
            )
            if candidate.is_file()
        ),
        None,
    )
    if manifest is None:
        raise AiRuntimeError("离线包源缺少 Qwen3-VL 8B 清单。")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    model_root = _ollama_models_root(runtime_root)
    relative_manifest = manifest.relative_to(model_root).as_posix()
    yield manifest, f"payload/models/ollama/{relative_manifest}"
    descriptors = [payload.get("config"), *(payload.get("layers") or [])]
    seen: set[str] = set()
    for descriptor in descriptors:
        digest = str((descriptor or {}).get("digest") or "")
        if not _is_sha256_digest(digest) or digest in seen:
            continue
        seen.add(digest)
        blob = _ollama_blob_path(runtime_root, digest)
        if not blob.is_file():
            raise AiRuntimeError(f"离线包源缺少 Qwen3-VL 数据层：{digest}")
        yield blob, f"payload/models/ollama/blobs/{blob.name}"


def _source_engine(content_root: Path) -> Path:
    pointer = read_json(content_root / "runtimes" / "current.json")
    relative = str(pointer.get("engine_path") or "")
    engine = (content_root / "runtimes" / relative).resolve()
    engines = (content_root / "runtimes" / "engines").resolve()
    if not engine.is_dir() or not engine.is_relative_to(engines):
        raise AiRuntimeError("没有可用于离线包的已激活 16GB 计算环境。")
    manifest = read_json(engine / ENGINE_MANIFEST)
    if (
        manifest.get("engine_version") != AI_ENGINE_VERSION
        or manifest.get("python_version") != PYTHON_RUNTIME_VERSION
        or manifest.get("profile_id") != OFFLINE_PROFILE
    ):
        raise AiRuntimeError("当前计算环境版本或档位与 16GB 离线包不一致。")
    return engine


def create_offline_bundle(
    output: Path,
    *,
    source_content_root: Path,
    source_model_runtime: Path,
) -> dict[str, Any]:
    """Create the first-party 16GB offline bundle from verified local assets."""

    from .model_resources import (
        _ollama_component_status,
        _resource_status,
        model_resources_status,
    )

    output = Path(output).expanduser().resolve()
    if output.suffix.casefold() != OFFLINE_BUNDLE_SUFFIX:
        raise AiRuntimeError(f"离线包文件必须以 {OFFLINE_BUNDLE_SUFFIX} 结尾。")
    output.parent.mkdir(parents=True, exist_ok=True)
    source_content_root = Path(source_content_root).resolve()
    source_model_runtime = Path(source_model_runtime).resolve()
    engine = _source_engine(source_content_root)
    model_state = model_resources_status(
        source_model_runtime, source_model_runtime / "data"
    )
    profile = next(
        item for item in model_state["profiles"] if item["id"] == OFFLINE_PROFILE
    )
    if not profile["ready"]:
        raise AiRuntimeError("本机 16GB 模型源没有完整校验通过，不能制作离线包。")
    for resource_id in PROFILE_SPECS[OFFLINE_PROFILE]["model_ids"]:
        status_payload = _resource_status(
            source_model_runtime, resource_id, deep_verification=False
        )
        if not status_payload["verified"]:
            raise AiRuntimeError(f"离线模型源校验失败：{status_payload['label']}")
    component = _ollama_component_status(source_model_runtime)
    if not component["verified"]:
        raise AiRuntimeError("离线包源的便携 Ollama 未通过校验。")

    python_root = (
        source_content_root
        / "runtimes"
        / "python"
        / f"cpython-{PYTHON_RUNTIME_VERSION}-windows-x86_64-none"
    )
    members: list[tuple[Path, str]] = []
    members.extend(_tree_members(engine / "venv", "payload/engine/venv"))
    members.extend(_tree_members(python_root, f"payload/python/{python_root.name}"))
    for resource_id in PROFILE_SPECS[OFFLINE_PROFILE]["model_ids"]:
        if MODEL_SPECS[resource_id]["provider"] == "huggingface":
            members.extend(_hf_snapshot_members(source_model_runtime, resource_id))
    members.extend(_ollama_members(source_model_runtime))
    ollama_root = Path(str(component["path"])).parent
    members.extend(
        _tree_members(ollama_root, f"payload/tools/{ollama_root.name}")
    )
    names = [name for _path, name in members]
    if len(names) != len(set(names)):
        raise AiRuntimeError("离线包源生成了重复文件名。")
    total_bytes = sum(path.stat().st_size for path, _name in members)
    if total_bytes <= 0 or total_bytes > MAX_UNCOMPRESSED_BYTES:
        raise AiRuntimeError("离线包源数据量不在允许范围内。")
    manifest = {
        "schema_version": OFFLINE_BUNDLE_SCHEMA,
        "application": "PhotoAI",
        "profile_id": OFFLINE_PROFILE,
        "product_version": PRODUCT_VERSION,
        "engine_version": AI_ENGINE_VERSION,
        "python_version": PYTHON_RUNTIME_VERSION,
        "architecture": "windows-x86_64",
        "created_at": _now(),
        "file_count": len(members),
        "payload_uncompressed_bytes": total_bytes,
        "resources": list(PROFILE_SPECS[OFFLINE_PROFILE]["model_ids"]),
    }
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.part")
    copied = 0
    last_report = time.monotonic()
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as archive:
            archive.writestr(
                OFFLINE_MANIFEST,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            for index, (source, destination) in enumerate(members, start=1):
                _archive_member(archive, source, destination)
                copied += source.stat().st_size
                if time.monotonic() - last_report >= 1.0 or index == len(members):
                    print(
                        f"offline-pack {index}/{len(members)} "
                        f"{copied / 1024**3:.2f}/{total_bytes / 1024**3:.2f} GiB",
                        flush=True,
                    )
                    last_report = time.monotonic()
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        **manifest,
        "path": str(output),
        "package_bytes": output.stat().st_size,
    }


def _read_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        info = archive.getinfo(OFFLINE_MANIFEST)
        if info.file_size > 1024 * 1024:
            raise AiRuntimeError("离线包清单过大。")
        payload = json.loads(archive.read(info).decode("utf-8"))
    except (KeyError, OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
        raise AiRuntimeError("无法读取 PhotoAI 离线包清单。") from exc
    expected = {
        "schema_version": OFFLINE_BUNDLE_SCHEMA,
        "application": "PhotoAI",
        "profile_id": OFFLINE_PROFILE,
        "engine_version": AI_ENGINE_VERSION,
        "python_version": PYTHON_RUNTIME_VERSION,
        "architecture": "windows-x86_64",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise AiRuntimeError(f"离线包不兼容：{key} 与当前版本不一致。")
    return payload


def inspect_offline_bundle(package: Path) -> dict[str, Any]:
    package = Path(package).expanduser().resolve()
    if package.suffix.casefold() != OFFLINE_BUNDLE_SUFFIX or not package.is_file():
        raise AiRuntimeError("请选择有效的 .photoai-offline 文件。")
    try:
        with zipfile.ZipFile(package, "r", allowZip64=True) as archive:
            manifest = _read_manifest(archive)
            members = [item for item in archive.infolist() if item.filename != OFFLINE_MANIFEST]
            if not members or len(members) > MAX_MEMBER_COUNT:
                raise AiRuntimeError("离线包文件数量不在允许范围内。")
            names: set[str] = set()
            total = 0
            for member in members:
                name = _safe_member_name(member.filename)
                if name in names or not _is_allowed_payload(name) or member.is_dir():
                    raise AiRuntimeError(f"离线包包含不允许的文件：{name}")
                names.add(name)
                mode = member.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise AiRuntimeError("离线包不能包含符号链接。")
                total += int(member.file_size)
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise AiRuntimeError("离线包解压数据量超过限制。")
            if total != int(manifest.get("payload_uncompressed_bytes") or -1):
                raise AiRuntimeError("离线包数据量与清单不一致。")
            if len(members) != int(manifest.get("file_count") or -1):
                raise AiRuntimeError("离线包文件数量与清单不一致。")
            return {
                **manifest,
                "path": str(package),
                "package_bytes": package.stat().st_size,
            }
    except zipfile.BadZipFile as exc:
        raise AiRuntimeError("离线包格式损坏。") from exc


def _extract_bundle(package: Path, destination: Path, manifest: Mapping[str, Any]) -> None:
    total = int(manifest["payload_uncompressed_bytes"])
    phase_start("offline_import", "导入离线资源", total, unit="B")
    copied = 0
    last_emit = 0.0
    with zipfile.ZipFile(package, "r", allowZip64=True) as archive:
        for member in archive.infolist():
            if member.filename == OFFLINE_MANIFEST:
                continue
            name = _safe_member_name(member.filename)
            target = (destination / Path(*PurePosixPath(name).parts)).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise AiRuntimeError("离线包试图写出临时导入目录。")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as incoming, target.open("xb") as outgoing:
                while block := incoming.read(COPY_BLOCK_BYTES):
                    outgoing.write(block)
                    copied += len(block)
                    now = time.monotonic()
                    if now - last_emit >= 0.4:
                        emit_progress(
                            "offline_import",
                            "导入离线资源",
                            copied,
                            total,
                            unit="B",
                            detail="正在解压本地离线包，无需联网",
                            current_resource=PurePosixPath(name).name,
                            downloaded_bytes=copied,
                            total_bytes=total,
                            heartbeat_at=heartbeat_timestamp(),
                        )
                        last_emit = now
    phase_end("offline_import", "离线资源已导入", total, unit="B")


def _replace_directory(source: Path, destination: Path, backup_root: Path) -> tuple[Path, Path | None]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if destination.exists() or destination.is_symlink():
        backup = backup_root / uuid.uuid4().hex
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(destination, backup)
    os.replace(source, destination)
    return destination, backup


def _merge_files(
    source: Path,
    destination: Path,
    backup_root: Path,
) -> list[tuple[Path, Path | None]]:
    adopted: list[tuple[Path, Path | None]] = []
    for incoming in sorted(path for path in source.rglob("*") if path.is_file()):
        relative = incoming.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if target.exists() or target.is_symlink():
            backup = backup_root / "files" / uuid.uuid4().hex
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup)
        os.replace(incoming, target)
        adopted.append((target, backup))
    return adopted


def _rollback_adopted(items: list[tuple[Path, Path | None]]) -> None:
    for destination, backup in reversed(items):
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination, ignore_errors=True)
        else:
            destination.unlink(missing_ok=True)
        if backup is not None and backup.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, destination)


def _rewrite_pyvenv(candidate: Path, managed_python: Path) -> None:
    config = candidate / "venv" / "pyvenv.cfg"
    if not config.is_file():
        raise AiRuntimeError("离线环境缺少 pyvenv.cfg。")
    lines = config.read_text(encoding="utf-8").splitlines()
    rewritten = [
        f"home = {managed_python}" if line.casefold().startswith("home =") else line
        for line in lines
    ]
    if not any(line.casefold().startswith("home =") for line in rewritten):
        rewritten.insert(0, f"home = {managed_python}")
    config.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def install_offline_bundle(
    layout: ContentRootLayout,
    package: Path,
    *,
    resources_root: Path | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Import, smoke-test and atomically activate a 16GB offline bundle."""

    package = Path(package).expanduser().resolve()
    manifest = inspect_offline_bundle(package)
    phase_start("preflight", "检查离线包与硬件", 3, unit="项")
    emit_progress(
        "preflight",
        "检查离线包与硬件",
        1,
        3,
        unit="项",
        detail=f"已识别 16GB 离线包 · {package.stat().st_size / 1024**3:.1f} GiB",
        current_resource=package.name,
        heartbeat_at=heartbeat_timestamp(),
    )
    gpu = _ensure_install_preconditions(layout, OFFLINE_PROFILE)
    emit_progress(
        "preflight",
        "检查离线包与硬件",
        2,
        3,
        unit="项",
        detail=f"{gpu.name or 'NVIDIA GPU'} · {gpu.memory_total_mib} MiB 显存",
        current_resource="NVIDIA GPU",
        heartbeat_at=heartbeat_timestamp(),
    )
    required = int(manifest["payload_uncompressed_bytes"]) + 2 * 1024**3
    free = shutil.disk_usage(layout.root).free
    if free < required:
        raise AiRuntimeError(f"数据盘空间不足：离线导入至少还需 {required / 1024**3:.1f} GiB。")
    phase_end("preflight", "离线包、显卡与空间检查通过", 3, unit="项")

    resources = locate_release_resources(resources_root)
    runner = runner or _run
    staging = layout.temp / f"offline-import-{uuid.uuid4().hex}"
    backup = layout.temp / f"offline-backup-{uuid.uuid4().hex}"
    candidate = layout.runtimes / "engines" / f"{AI_ENGINE_VERSION}-offline-{uuid.uuid4().hex}"
    adopted: list[tuple[Path, Path | None]] = []
    environment = os.environ.copy()
    environment.update(runtime_environment(layout))
    environment.update(
        {
            "UV_OFFLINE": "1",
            "UV_PYTHON_NO_REGISTRY": "1",
            "UV_MANAGED_PYTHON": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PHOTO_AI_CONTENT_ROOT": str(layout.root),
            "PHOTO_AI_MODEL_PROFILE": OFFLINE_PROFILE,
        }
    )
    previous_engine = active_engine(layout)
    try:
        staging.mkdir(parents=True, exist_ok=False)
        backup.mkdir(parents=True, exist_ok=False)
        _extract_bundle(package, staging, manifest)
        payload = staging / "payload"
        phase_start("offline_dependencies", "安装离线运行环境", 4, unit="项")

        managed_python_source = (
            payload
            / "python"
            / f"cpython-{PYTHON_RUNTIME_VERSION}-windows-x86_64-none"
        )
        managed_python = (
            layout.runtimes
            / "python"
            / f"cpython-{PYTHON_RUNTIME_VERSION}-windows-x86_64-none"
        )
        adopted.append(_replace_directory(managed_python_source, managed_python, backup))
        emit_progress(
            "offline_dependencies", "安装离线运行环境", 1, 4, unit="项",
            detail="已安装便携 Python", current_resource=f"Python {PYTHON_RUNTIME_VERSION}",
            heartbeat_at=heartbeat_timestamp(),
        )

        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.mkdir(parents=True, exist_ok=False)
        os.replace(payload / "engine" / "venv", candidate / "venv")
        _rewrite_pyvenv(candidate, managed_python)
        emit_progress(
            "offline_dependencies", "安装离线运行环境", 2, 4, unit="项",
            detail="已安装固定 Python/CUDA 依赖", current_resource="Torch / Transformers / 图像组件",
            heartbeat_at=heartbeat_timestamp(),
        )

        hf_source = payload / "models" / "huggingface" / "hub"
        for resource_id in PROFILE_SPECS[OFFLINE_PROFILE]["model_ids"]:
            spec = MODEL_SPECS[resource_id]
            if spec["provider"] != "huggingface":
                continue
            repository = "models--" + str(spec["repo_id"]).replace("/", "--")
            adopted.append(
                _replace_directory(
                    hf_source / repository,
                    layout.models / "huggingface" / "hub" / repository,
                    backup,
                )
            )
        adopted.extend(
            _merge_files(
                payload / "models" / "ollama",
                layout.models / "ollama",
                backup,
            )
        )
        emit_progress(
            "offline_dependencies", "安装离线运行环境", 3, 4, unit="项",
            detail="已导入 6 个视觉模型与 Qwen3-VL 8B", current_resource="16GB 模型",
            heartbeat_at=heartbeat_timestamp(),
        )

        ollama_source = payload / "tools" / "ollama-v0.33.2"
        adopted.append(
            _replace_directory(
                ollama_source,
                layout.tools / "ollama-v0.33.2",
                backup,
            )
        )
        python = _venv_python(candidate)
        if not python.is_file():
            raise AiRuntimeError("离线环境中的 Python 启动器缺失。")
        runner(
            [
                str(resources.uv), "pip", "install", "--offline", "--python", str(python),
                "--managed-python", "--no-deps", "--reinstall", str(resources.worker_wheel),
            ],
            environment,
            resources.root,
        )
        phase_end("offline_dependencies", "离线运行环境安装完成", 4, unit="项")

        write_json(
            candidate / ENGINE_MANIFEST,
            {
                "schema_version": AI_RUNTIME_SCHEMA,
                "engine_version": AI_ENGINE_VERSION,
                "product_version": PRODUCT_VERSION,
                "python_version": PYTHON_RUNTIME_VERSION,
                "worker_wheel": resources.worker_wheel.name,
                "worker_wheel_sha256": resources.worker_wheel_sha256,
                "profile_id": OFFLINE_PROFILE,
                "installation_source": "offline-bundle",
                "offline_package": package.name,
                "gpu": {
                    "name": gpu.name,
                    "memory_total_mib": gpu.memory_total_mib,
                    "driver_version": gpu.driver_version,
                },
                "created_at": _now(),
            },
        )
        runner(
            [
                str(python), "-m", "landscape_culler.ai_install_worker",
                "--content-root", str(layout.root), "--profile", OFFLINE_PROFILE,
                "--smoke-result", str(candidate / SMOKE_RESULT),
                "--offline",
            ],
            environment,
            resources.root,
        )
        smoke = read_json(candidate / SMOKE_RESULT)
        if smoke.get("status") != "passed":
            raise AiRuntimeError("离线环境实际自检未通过，未启用。")

        pointer_path = layout.runtimes / "current.json"
        settings_path = layout.state / "model-resources" / "settings.json"
        pointer_snapshot = _file_snapshot(pointer_path)
        settings_snapshot = _file_snapshot(settings_path)
        phase_start("activate", "启用离线环境", 1, unit="项")
        from .model_resources import activate_model_profile

        try:
            activate_model_profile(OFFLINE_PROFILE, layout.state)
            _atomic_activate(layout, candidate, OFFLINE_PROFILE)
        except Exception:
            _restore_file_snapshot(pointer_path, pointer_snapshot)
            _restore_file_snapshot(settings_path, settings_snapshot)
            raise
        phase_end("activate", "16GB 离线环境已启用", 1, unit="项")
        if previous_engine is not None and previous_engine != candidate:
            shutil.rmtree(previous_engine, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        adopted.clear()
        return ai_runtime_status(layout)
    except Exception:
        if candidate.exists() and active_engine(layout) != candidate:
            shutil.rmtree(candidate, ignore_errors=True)
        _rollback_adopted(adopted)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
