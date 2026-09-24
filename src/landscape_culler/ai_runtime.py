"""Install and activate the optional AI engine without using machine Python.

The desktop/core distribution imports this module, so it intentionally imports
no numerical or AI package.  Every mutable path is rooted in ``CONTENT_ROOT``;
the only system-level dependency it probes is the NVIDIA display driver.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .content_root import ContentRootLayout, resolve_resource_path, runtime_environment
from .progress import emit_progress, heartbeat_timestamp, phase_end, phase_start
from .util import read_json, write_json
from .version import AI_ENGINE_VERSION, PRODUCT_VERSION, PYTHON_RUNTIME_VERSION

AI_RUNTIME_SCHEMA = 1
MINIMUM_NVIDIA_DRIVER_MAJOR = 580
ENGINE_POINTER = "current.json"
ENGINE_MANIFEST = "engine.json"
SMOKE_RESULT = "smoke-test.json"
PROFILE_ESTIMATED_BYTES = {"8gb": 12_500_000_000, "16gb": 16_000_000_000}
PROFILE_MINIMUM_VRAM_MIB = {"8gb": 7_000, "16gb": 12_000}
DOMESTIC_PYTHON_MIRROR = (
    "https://registry.npmmirror.com/-/binary/python-build-standalone"
)
DOMESTIC_PYPI_INDEX = "https://mirrors.aliyun.com/pypi/simple"
DOMESTIC_PYTORCH_WHEELS = "https://mirrors.aliyun.com/pytorch-wheels/cu130/"
OFFICIAL_PYPI_INDEX = "https://pypi.org/simple"


class AiRuntimeError(RuntimeError):
    """The managed AI environment could not be installed or activated."""


@dataclass(frozen=True, slots=True)
class NvidiaStatus:
    available: bool
    name: str | None
    memory_total_mib: int
    driver_version: str | None
    compatible_driver: bool


@dataclass(frozen=True, slots=True)
class ReleaseResources:
    root: Path
    uv: Path
    requirements: Path
    wheelhouse: Path
    worker_wheel: Path
    worker_wheel_sha256: str


CommandRunner = Callable[[list[str], Mapping[str, str], Path | None], None]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parents[2]


def release_resources_root() -> Path:
    configured = os.environ.get("PHOTO_AI_RELEASE_RESOURCES")
    if configured:
        return Path(configured).expanduser().resolve()
    bundled_resources = os.environ.get("PHOTO_AI_RESOURCE_ROOT")
    candidates = [
        Path(bundled_resources).expanduser().resolve() / "manifests"
        if bundled_resources
        else _bundle_root() / "__missing_bundle_resources__",
        _bundle_root() / "resources",
        _bundle_root() / "packaging" / "resources",
        Path(sys.executable).resolve().parent / "resources",
    ]
    return next((path for path in candidates if path.is_dir()), candidates[0])


def _read_sidecar_hash(path: Path) -> str:
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise AiRuntimeError(f"缺少发布资源摘要：{sidecar.name}")
    value = sidecar.read_text(encoding="utf-8").strip().split()[0].casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AiRuntimeError(f"发布资源摘要格式无效：{sidecar.name}")
    return value


def locate_release_resources(root: Path | None = None) -> ReleaseResources:
    root = (root or release_resources_root()).resolve()
    requirements = root / "ai-requirements.lock"
    wheelhouse = root / "wheels"
    wheel_candidates = sorted((root / "ai-worker").glob("*.whl"))
    bundled_resources = os.environ.get("PHOTO_AI_RESOURCE_ROOT")
    uv_candidates = [
        root / "uv.exe",
        Path(bundled_resources).expanduser().resolve() / "tools" / "uv.exe"
        if bundled_resources
        else root / "__missing_uv.exe",
        Path(sys.executable).resolve().parent / "tools" / "uv.exe",
    ]
    configured_uv = os.environ.get("PHOTO_AI_UV")
    if configured_uv:
        uv_candidates.insert(0, Path(configured_uv))
    uv = next((path for path in uv_candidates if path.is_file()), uv_candidates[0])
    if not uv.is_file():
        raise AiRuntimeError("安装包缺少固定版本的 uv.exe。")
    if not requirements.is_file():
        raise AiRuntimeError("安装包缺少带哈希的 AI 依赖清单。")
    if not wheelhouse.is_dir():
        raise AiRuntimeError("安装包缺少已审核的本地 wheel 目录。")
    if len(wheel_candidates) != 1:
        raise AiRuntimeError("安装包必须包含且只包含一个 AI Worker wheel。")
    worker_wheel = wheel_candidates[0]
    expected = _read_sidecar_hash(worker_wheel)
    if _sha256(worker_wheel) != expected:
        raise AiRuntimeError("AI Worker wheel 摘要不匹配。")
    return ReleaseResources(
        root=root,
        uv=uv,
        requirements=requirements,
        wheelhouse=wheelhouse,
        worker_wheel=worker_wheel,
        worker_wheel_sha256=expected,
    )


def detect_nvidia() -> NvidiaStatus:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
        line = next(value for value in result.stdout.splitlines() if value.strip())
        name, memory, driver = [part.strip() for part in line.rsplit(",", 2)]
        major = int(driver.split(".", 1)[0])
        return NvidiaStatus(
            True, name, int(memory), driver, major >= MINIMUM_NVIDIA_DRIVER_MAJOR
        )
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        return NvidiaStatus(False, None, 0, None, False)


def _venv_python(engine: Path) -> Path:
    return engine / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _engine_pointer(layout: ContentRootLayout) -> Path:
    return layout.runtimes / ENGINE_POINTER


def _engine_dir(layout: ContentRootLayout, relative: str) -> Path:
    return resolve_resource_path(layout.runtimes, relative, must_exist=True)


def active_engine(layout: ContentRootLayout) -> Path | None:
    pointer = _engine_pointer(layout)
    if not pointer.is_file():
        return None
    try:
        payload = read_json(pointer)
        relative = str(payload["engine_path"])
        engine = _engine_dir(layout, relative)
        manifest = read_json(engine / ENGINE_MANIFEST)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if manifest.get("engine_version") != AI_ENGINE_VERSION:
        return None
    python = _venv_python(engine)
    smoke = engine / SMOKE_RESULT
    if not python.is_file() or not smoke.is_file():
        return None
    try:
        if read_json(smoke).get("status") != "passed":
            return None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return engine


def ai_runtime_status(layout: ContentRootLayout) -> dict[str, Any]:
    gpu = detect_nvidia()
    engine = active_engine(layout)
    smoke: dict[str, Any] = {}
    if engine is not None:
        try:
            smoke = read_json(engine / SMOKE_RESULT)
        except (OSError, ValueError, json.JSONDecodeError):
            engine = None
    return {
        "schema_version": AI_RUNTIME_SCHEMA,
        "engine_version": AI_ENGINE_VERSION,
        "ready": engine is not None,
        "engine_path": str(engine) if engine else None,
        "gpu": {
            "available": gpu.available,
            "name": gpu.name,
            "memory_total_mib": gpu.memory_total_mib,
            "driver_version": gpu.driver_version,
            "compatible_driver": gpu.compatible_driver,
            "minimum_driver_major": MINIMUM_NVIDIA_DRIVER_MAJOR,
        },
        "smoke_test": smoke,
    }


def ai_resources_status(layout: ContentRootLayout) -> dict[str, Any]:
    """Return the four-layer readiness gate consumed by Settings and workflows."""

    from .model_resources import model_resources_status

    engine = ai_runtime_status(layout)
    models = model_resources_status(layout.models, layout.state)
    if models.get("cloud_vision") and engine.get("engine_path"):
        worker_root = Path(engine["engine_path"]) / "venv" / "Lib" / "site-packages" / "landscape_culler"
        if not (worker_root / "vision_provider.py").is_file():
            engine["ready"] = False
            engine["message"] = "计算环境需要更新以支持云端模型，请重新配置当前档位（复用已下载模型）。"
    component = next(
        (item for item in models.get("components", []) if item.get("id") == "ollama"),
        {"verified": False},
    )
    smoke_ready = bool(engine.get("smoke_test", {}).get("status") == "passed")
    if not models.get("cloud_vision") and engine.get("smoke_test", {}).get("checks", {}).get("cloud_vision"):
        smoke_ready = False  # Switching back to local requires an actual Qwen smoke test.
    smoke_profile = engine.get("smoke_test", {}).get("profile_id")
    any_models_ready = any(
        bool(item.get("ready")) for item in models.get("profiles", [])
    )
    for profile in models.get("profiles", []):
        model_ready = bool(profile.get("ready"))
        profile["layers"] = {
            "engine": bool(engine.get("ready")),
            "ollama": bool(models.get("cloud_vision") or component.get("verified")),
            "models": model_ready,
            "smoke_test": smoke_ready and smoke_profile == profile.get("id"),
        }
        profile["ready"] = all(profile["layers"].values())
    models.update(
        engine=engine,
        readiness_layers={
            "engine": bool(engine.get("ready")),
            "ollama": bool(models.get("cloud_vision") or component.get("verified")),
            "models": any_models_ready,
            "smoke_test": smoke_ready,
        },
        content_root=str(layout.root),
        runtime_root=str(layout.runtimes),
    )
    return models


def _run(command: list[str], env: Mapping[str, str], cwd: Path | None) -> None:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    tail: list[str] = []
    assert process.stdout is not None
    output: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            for raw in process.stdout:
                output.put(raw)
        finally:
            output.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    phase = str(env.get("PHOTO_AI_COMMAND_PHASE") or "").strip()
    label = str(env.get("PHOTO_AI_COMMAND_LABEL") or phase).strip()
    resource = str(env.get("PHOTO_AI_COMMAND_RESOURCE") or label).strip()
    detail = str(env.get("PHOTO_AI_COMMAND_DETAIL") or label).strip()
    try:
        watch_roots = [
            Path(value)
            for value in json.loads(str(env.get("PHOTO_AI_COMMAND_WATCH_ROOTS") or "[]"))
            if str(value).strip()
        ]
    except (TypeError, ValueError, json.JSONDecodeError):
        watch_roots = []

    def watched_size() -> int:
        total = 0
        for root in watch_roots:
            if root.is_file():
                try:
                    total += root.stat().st_size
                except OSError:
                    pass
                continue
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except OSError:
                    continue
        return total

    started = time.monotonic()
    last_heartbeat = started
    last_scan = started
    baseline_size = watched_size() if watch_roots else 0
    last_size = baseline_size
    measured_size = baseline_size
    measured_speed = 0.0
    finished_output = False
    while not finished_output:
        try:
            raw = output.get(timeout=0.25)
        except queue.Empty:
            raw = ""
        if raw is None:
            finished_output = True
        elif raw:
            line = raw.rstrip("\r\n")
            if line:
                print(line, flush=True)
                tail = [*tail[-19:], line]
        now = time.monotonic()
        if phase and now - last_heartbeat >= 1.0:
            # Never recursively scan a multi-gigabyte uv cache while child
            # output is waiting. Doing so throttled package output to one line
            # per scan and made an already-completed install appear frozen.
            if raw == "" and watch_roots and now - last_scan >= 2.5:
                measured_size = watched_size()
                interval = max(0.001, now - last_scan)
                measured_speed = max(0.0, measured_size - last_size) / interval
                last_size = measured_size
                last_scan = now
            elapsed = max(0.0, now - started)
            transfer_phase = phase in {"python", "dependencies"}
            emit_progress(
                phase,
                label,
                0,
                0,
                detail=f"{detail}（已运行 {int(elapsed)} 秒）",
                current_resource=resource,
                downloaded_bytes=(
                    max(0, measured_size - baseline_size) if transfer_phase else None
                ),
                bytes_per_second=measured_speed if transfer_phase else None,
                elapsed_seconds=elapsed,
                heartbeat_at=heartbeat_timestamp(),
            )
            last_heartbeat = now
    return_code = process.wait()
    if return_code != 0:
        raise AiRuntimeError(f"依赖安装命令失败（{return_code}）：\n" + "\n".join(tail))


def _stage_environment(
    environment: Mapping[str, str],
    *,
    phase: str,
    label: str,
    resource: str,
    detail: str,
    watch_roots: list[Path],
) -> dict[str, str]:
    staged = dict(environment)
    staged.update(
        PHOTO_AI_COMMAND_PHASE=phase,
        PHOTO_AI_COMMAND_LABEL=label,
        PHOTO_AI_COMMAND_RESOURCE=resource,
        PHOTO_AI_COMMAND_DETAIL=detail,
        PHOTO_AI_COMMAND_WATCH_ROOTS=json.dumps([str(path) for path in watch_roots]),
    )
    return staged


def _run_source_attempts(
    runner: CommandRunner,
    attempts: list[tuple[str, list[str], Mapping[str, str]]],
    *,
    phase: str,
    label: str,
    resource: str,
    cwd: Path | None,
) -> None:
    errors: list[str] = []
    for index, (source_label, command, environment) in enumerate(attempts):
        emit_progress(
            phase,
            label,
            0,
            0,
            detail=f"连接 {source_label}",
            current_resource=resource,
            heartbeat_at=heartbeat_timestamp(),
        )
        try:
            runner(command, environment, cwd)
            return
        except AiRuntimeError as exc:
            errors.append(f"{source_label}: {exc}")
            if index + 1 < len(attempts):
                emit_progress(
                    phase,
                    label,
                    0,
                    0,
                    detail=f"{source_label} 不可用，自动切换备用源",
                    current_resource=resource,
                    heartbeat_at=heartbeat_timestamp(),
                )
    raise AiRuntimeError("；".join(errors))


def _ensure_install_preconditions(
    layout: ContentRootLayout, profile_id: str
) -> NvidiaStatus:
    if profile_id not in PROFILE_ESTIMATED_BYTES:
        raise AiRuntimeError("未知显存档位。")
    gpu = detect_nvidia()
    if not gpu.available:
        raise AiRuntimeError("未检测到可用的 NVIDIA 显卡。")
    if not gpu.compatible_driver:
        raise AiRuntimeError(
            f"NVIDIA 驱动版本为 {gpu.driver_version or '未知'}；CUDA 13 需要 R580 或更新驱动。"
        )
    required_vram = PROFILE_MINIMUM_VRAM_MIB[profile_id]
    if gpu.memory_total_mib < required_vram:
        raise AiRuntimeError(
            f"{profile_id.upper()} 配置至少需要 {required_vram} MiB 显存，当前为 {gpu.memory_total_mib} MiB。"
        )
    free = shutil.disk_usage(layout.root).free
    required = PROFILE_ESTIMATED_BYTES[profile_id]
    if free < required:
        raise AiRuntimeError(f"数据盘空间不足：至少还需 {required / 1024**3:.1f} GiB。")
    return gpu


def _atomic_activate(layout: ContentRootLayout, engine: Path, profile_id: str) -> None:
    relative = engine.relative_to(layout.runtimes).as_posix()
    pointer = {
        "schema_version": AI_RUNTIME_SCHEMA,
        "engine_version": AI_ENGINE_VERSION,
        "engine_path": relative,
        "profile_id": profile_id,
        "activated_at": _now(),
    }
    write_json(_engine_pointer(layout), pointer)


def _file_snapshot(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _restore_file_snapshot(path: Path, snapshot: bytes | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.restore")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("xb") as handle:
            handle.write(snapshot)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_ai_profile(
    layout: ContentRootLayout,
    profile_id: str,
    *,
    resources_root: Path | None = None,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Create a complete staged environment and publish it only after smoke tests."""

    phase_start("preflight", "检查显卡与存储空间", 2, unit="项")
    emit_progress(
        "preflight",
        "检查显卡与存储空间",
        0,
        2,
        unit="项",
        detail="正在读取 NVIDIA 显卡、驱动和显存信息",
        current_resource="NVIDIA GPU",
        heartbeat_at=heartbeat_timestamp(),
    )
    gpu = _ensure_install_preconditions(layout, profile_id)
    emit_progress(
        "preflight",
        "显卡与驱动符合要求",
        1,
        2,
        unit="项",
        detail=(
            f"{gpu.name or 'NVIDIA GPU'} · {gpu.memory_total_mib} MiB 显存 · "
            f"驱动 {gpu.driver_version or '未知'}"
        ),
        current_resource="NVIDIA GPU",
        heartbeat_at=heartbeat_timestamp(),
    )
    free_bytes = shutil.disk_usage(layout.root).free
    emit_progress(
        "preflight",
        "数据目录空间符合要求",
        2,
        2,
        unit="项",
        detail=(
            f"可用 {free_bytes / 1024**3:.1f} GiB；"
            f"本配置至少需要 {PROFILE_ESTIMATED_BYTES[profile_id] / 1024**3:.1f} GiB"
        ),
        current_resource=str(layout.root),
        heartbeat_at=heartbeat_timestamp(),
    )
    resources = locate_release_resources(resources_root)
    phase_end("preflight", "硬件与空间检查通过", 2, unit="项")
    runner = runner or _run
    engines = layout.runtimes / "engines"
    engines.mkdir(parents=True, exist_ok=True)
    # A venv is not relocatable: pyvenv.cfg and entry points may contain its
    # absolute creation path. Build the candidate at its permanent unique path
    # and make it current only by atomically replacing current.json.
    candidate = engines / f"{AI_ENGINE_VERSION}-{uuid.uuid4().hex}"
    candidate.mkdir(parents=True, exist_ok=False)
    managed_python = layout.runtimes / "python"
    environment = os.environ.copy()
    environment.update(runtime_environment(layout))
    environment.update(
        {
            "UV_PYTHON_INSTALL_DIR": str(managed_python),
            "UV_CACHE_DIR": str(layout.cache / "uv"),
            "UV_PYTHON_NO_REGISTRY": "1",
            "UV_MANAGED_PYTHON": "1",
            "HF_HUB_OFFLINE": "0",
            "TRANSFORMERS_OFFLINE": "0",
            "PHOTO_AI_CONTENT_ROOT": str(layout.root),
            "PHOTO_AI_MODEL_PROFILE": profile_id,
        }
    )
    try:
        phase_start("python", "安装受管 Python", 1, unit="项")
        python_command = [
            str(resources.uv),
            "python",
            "install",
            PYTHON_RUNTIME_VERSION,
            "--install-dir",
            str(managed_python),
            "--no-bin",
            "--no-registry",
            "--managed-python",
        ]
        _run_source_attempts(
            runner,
            [
                (
                    "国内 npmmirror",
                    [*python_command, "--mirror", DOMESTIC_PYTHON_MIRROR],
                    _stage_environment(
                        environment,
                        phase="python",
                        label="安装受管 Python",
                        resource=f"Python {PYTHON_RUNTIME_VERSION}",
                        detail="从国内 npmmirror 下载或复用固定版本 Python",
                        watch_roots=[managed_python, layout.cache / "uv"],
                    ),
                ),
                (
                    "Python 官方源",
                    python_command,
                    _stage_environment(
                        environment,
                        phase="python",
                        label="安装受管 Python",
                        resource=f"Python {PYTHON_RUNTIME_VERSION}",
                        detail="国内源不可用，改用官方源续传固定版本 Python",
                        watch_roots=[managed_python, layout.cache / "uv"],
                    ),
                ),
            ],
            phase="python",
            label="安装受管 Python",
            resource=f"Python {PYTHON_RUNTIME_VERSION}",
            cwd=resources.root,
        )
        phase_end("python", "受管 Python 已安装", 1, unit="项")
        phase_start("venv", "创建隔离计算环境", 1, unit="项")
        runner(
            [
                str(resources.uv),
                "venv",
                str(candidate / "venv"),
                "--python",
                PYTHON_RUNTIME_VERSION,
                "--managed-python",
                "--no-project",
            ],
            _stage_environment(
                environment,
                phase="venv",
                label="创建隔离计算环境",
                resource="Python venv",
                detail="创建版本化隔离环境",
                watch_roots=[candidate / "venv"],
            ),
            resources.root,
        )
        phase_end("venv", "隔离计算环境已创建", 1, unit="项")
        phase_start("dependencies", "安装固定 AI 依赖", 1, unit="项")
        python = _venv_python(candidate)
        dependency_command = [
            str(resources.uv),
            "pip",
            "sync",
            "--python",
            str(python),
            "--managed-python",
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--strict",
            "--index-strategy",
            "unsafe-best-match",
            "--find-links",
            str(resources.wheelhouse),
            str(resources.requirements),
        ]
        _run_source_attempts(
            runner,
            [
                (
                    "阿里云 Python / PyTorch 镜像",
                    [
                        *dependency_command[:-1],
                        "--default-index",
                        DOMESTIC_PYPI_INDEX,
                        "--find-links",
                        DOMESTIC_PYTORCH_WHEELS,
                        dependency_command[-1],
                    ],
                    _stage_environment(
                        environment,
                        phase="dependencies",
                        label="安装固定 AI 依赖",
                        resource="Torch / Transformers / 图像组件",
                        detail="从阿里云镜像安装固定 AI 依赖",
                        watch_roots=[candidate / "venv", layout.cache / "uv"],
                    ),
                ),
                (
                    "Python / PyTorch 官方源",
                    [
                        *dependency_command[:-1],
                        "--default-index",
                        OFFICIAL_PYPI_INDEX,
                        "--torch-backend",
                        "cu130",
                        dependency_command[-1],
                    ],
                    _stage_environment(
                        environment,
                        phase="dependencies",
                        label="安装固定 AI 依赖",
                        resource="Torch / Transformers / 图像组件",
                        detail="国内源不可用，改用官方源续装固定 AI 依赖",
                        watch_roots=[candidate / "venv", layout.cache / "uv"],
                    ),
                ),
            ],
            phase="dependencies",
            label="安装固定 AI 依赖",
            resource="Torch / Transformers / 图像组件",
            cwd=resources.root,
        )
        phase_end("dependencies", "固定 AI 依赖已安装", 1, unit="项")
        phase_start("worker", "安装 AI Worker", 1, unit="项")
        runner(
            [
                str(resources.uv),
                "pip",
                "install",
                "--python",
                str(python),
                "--managed-python",
                "--no-deps",
                "--reinstall",
                str(resources.worker_wheel),
            ],
            _stage_environment(
                environment,
                phase="worker",
                label="安装 AI Worker",
                resource=resources.worker_wheel.name,
                detail="安装与桌面程序同版本的 AI Worker",
                watch_roots=[candidate / "venv"],
            ),
            resources.root,
        )
        phase_end("worker", "AI Worker 已安装", 1, unit="项")
        write_json(
            candidate / ENGINE_MANIFEST,
            {
                "schema_version": AI_RUNTIME_SCHEMA,
                "engine_version": AI_ENGINE_VERSION,
                "product_version": PRODUCT_VERSION,
                "python_version": PYTHON_RUNTIME_VERSION,
                "worker_wheel": resources.worker_wheel.name,
                "worker_wheel_sha256": resources.worker_wheel_sha256,
                "profile_id": profile_id,
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
                str(python),
                "-m",
                "landscape_culler.ai_install_worker",
                "--content-root",
                str(layout.root),
                "--profile",
                profile_id,
                "--smoke-result",
                str(candidate / SMOKE_RESULT),
            ],
            environment,
            resources.root,
        )
        smoke = read_json(candidate / SMOKE_RESULT)
        if smoke.get("status") != "passed":
            raise AiRuntimeError("AI 实际自检未通过，未启用新环境。")

        previous_engine = active_engine(layout)
        pointer_path = _engine_pointer(layout)
        settings_path = layout.state / "model-resources" / "settings.json"
        pointer_snapshot = _file_snapshot(pointer_path)
        settings_snapshot = _file_snapshot(settings_path)
        phase_start("activate", "启用计算环境", 1, unit="项")
        from .model_resources import activate_model_profile

        try:
            activate_model_profile(profile_id, layout.state)
            _atomic_activate(layout, candidate, profile_id)
        except Exception:
            _restore_file_snapshot(pointer_path, pointer_snapshot)
            _restore_file_snapshot(settings_path, settings_snapshot)
            raise
        phase_end("activate", "计算环境已启用", 1, unit="项")
        if previous_engine is not None and previous_engine != candidate:
            shutil.rmtree(previous_engine, ignore_errors=True)
    except Exception:
        if candidate.exists() and active_engine(layout) != candidate:
            shutil.rmtree(candidate, ignore_errors=True)
        raise
    return ai_runtime_status(layout)


def delete_ai_runtime(layout: ContentRootLayout) -> dict[str, Any]:
    """Remove managed runtimes only; models, projects and user data remain."""

    pointer = _engine_pointer(layout)
    pointer.unlink(missing_ok=True)
    for directory in (
        layout.runtimes / "engines",
        layout.runtimes / "python",
        layout.runtimes / ".staging",
    ):
        if directory.is_dir() and directory.resolve().is_relative_to(
            layout.runtimes.resolve()
        ):
            shutil.rmtree(directory, ignore_errors=False)
    return ai_runtime_status(layout)
