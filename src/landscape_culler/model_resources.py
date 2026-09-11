from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .progress import emit_progress, heartbeat_timestamp, phase_end, phase_start
from .util import read_json, write_json

ProfileId = Literal["8gb", "16gb"]
MODEL_RESOURCE_SCHEMA = 1
DEFAULT_PROFILE: ProfileId = "16gb"
DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11435"
OLLAMA_VERSION = "v0.33.2"
OLLAMA_ARCHIVE_NAME = f"ollama-windows-amd64-{OLLAMA_VERSION}.zip"
OLLAMA_ARCHIVE_URL = (
    f"https://github.com/ollama/ollama/releases/download/{OLLAMA_VERSION}/"
    "ollama-windows-amd64.zip"
)
OLLAMA_ARCHIVE_SIZE = 1_460_134_793
OLLAMA_ARCHIVE_SOURCES = (
    ("国内 Ollama 镜像", "https://ollama.ac.cn/download/ollama-windows-amd64.zip"),
    ("GitHub 官方源", OLLAMA_ARCHIVE_URL),
)
OLLAMA_ARCHIVE_SHA256 = (
    "2439cbea65310b1aadf7d8fc41d7faf5d033f920d42e00a476c58bf9bff6950e"
)
OLLAMA_EXECUTABLE_SHA256 = (
    "c79df1e0c1bfa10ed813c7030ac4c3ba38bb0e350bd7322d9bb58320343235c6"
)
_OWNED_OLLAMA_PROCESS: subprocess.Popen[bytes] | None = None
HF_DOWNLOAD_ENDPOINTS = (
    ("国内镜像 Alpha", "https://alpha.hf-mirror.com"),
    ("国内镜像 Beta", "https://beta.hf-mirror.com"),
    ("国内镜像", "https://hf-mirror.com"),
    ("Hugging Face 官方源", "https://huggingface.co"),
)
OLLAMA_MODEL_REGISTRIES = (
    ("国内 Ollama 镜像", "ollama.ac.cn/library"),
    ("Ollama 官方源", "registry.ollama.ai/library"),
)
_HF_ENDPOINT_SELECTION: tuple[str, str] | None = None
_HF_ENDPOINT_SELECTION_LOCK = threading.Lock()


class _TransferTelemetry:
    """Emit throttled byte progress with resume, speed and ETA telemetry."""

    def __init__(
        self,
        phase: str,
        label: str,
        total: int,
        *,
        resumed: int = 0,
        resource: str | None = None,
    ) -> None:
        self.phase = phase
        self.label = label
        self.total = max(1, int(total))
        self.resumed = max(0, int(resumed))
        self.resource = resource or label
        self.started = time.monotonic()
        self.last_at = self.started
        self.last_bytes = self.resumed
        self.speed = 0.0
        phase_start(
            phase,
            label,
            self.total,
            current=min(self.resumed, self.total),
            unit="B",
            cached=self.resumed,
        )
        self.update(self.resumed, resource=self.resource, force=True)

    def update(
        self,
        current: int,
        *,
        resource: str | None = None,
        detail: str | None = None,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        current = max(self.resumed, int(current))
        interval = max(0.0, now - self.last_at)
        if not force and interval < 0.75 and current < self.total:
            return
        if interval > 0 and current >= self.last_bytes:
            instant = (current - self.last_bytes) / interval
            self.speed = instant if self.speed <= 0 else self.speed * 0.72 + instant * 0.28
        elapsed = max(0.0, now - self.started)
        eta = (
            max(0.0, self.total - current) / self.speed
            if self.speed > 0 and current < self.total
            else (0.0 if current >= self.total else None)
        )
        emit_progress(
            self.phase,
            self.label,
            min(current, self.total),
            self.total,
            unit="B",
            cached=min(self.resumed, self.total),
            detail=detail,
            current_resource=resource or self.resource,
            downloaded_bytes=current,
            total_bytes=self.total,
            bytes_per_second=self.speed,
            eta_seconds=eta,
            resumed_bytes=self.resumed,
            elapsed_seconds=elapsed,
            heartbeat_at=heartbeat_timestamp(),
        )
        self.last_at = now
        self.last_bytes = current

    def finish(self, *, resource: str | None = None, detail: str | None = None) -> None:
        self.update(self.total, resource=resource, detail=detail, force=True)
        phase_end(self.phase, self.label, self.total, unit="B", cached=self.resumed)


def _call_with_progress_heartbeat(
    callback: Any,
    *,
    phase: str,
    label: str,
    current: int,
    total: int,
    resource: str,
    detail: str,
    unit: str = "项",
) -> Any:
    """Keep a synchronous hash/check operation visibly alive without moving it."""

    stopped = threading.Event()
    started = time.monotonic()

    def heartbeat() -> None:
        while not stopped.wait(1.0):
            elapsed = max(0.0, time.monotonic() - started)
            emit_progress(
                phase,
                label,
                current,
                total,
                unit=unit,
                detail=f"{detail}（已运行 {int(elapsed)} 秒）",
                current_resource=resource,
                elapsed_seconds=elapsed,
                heartbeat_at=heartbeat_timestamp(),
            )

    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        return callback()
    finally:
        stopped.set()
        worker.join(timeout=2)


MODEL_SPECS: dict[str, dict[str, Any]] = {
    "dinov2-base": {
        "label": "DINOv2 Base",
        "purpose": "相似分组与代表图",
        "provider": "huggingface",
        "repo_id": "facebook/dinov2-base",
        "revision": "f9e44c814b77203eaa57a6bdbbd535f21ede1415",
        "files": ["config.json", "model.safetensors", "preprocessor_config.json"],
        "weight_sha256": {
            "model.safetensors": "d73036b56966966d07975d696bde331762f37297e2f095de8cea0040c3aa0841"
        },
        "estimated_bytes": 365_000_000,
    },
    "qrealign-mini": {
        "label": "Q-ReAlign Mini 0.8B",
        "purpose": "审美评分与风格复评",
        "provider": "huggingface",
        "repo_id": "q-future/Q-ReAlign-Mini-0.8B",
        "revision": "fe1f45a7574c9e9d908875af9f7e90cb946aa19f",
        "files": [
            "chat_template.jinja",
            "config.json",
            "generation_config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "processor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
        ],
        "weight_sha256": {
            "model.safetensors": "bde34df0375fff90d2dee716a127039c57d310c0c868b9f52f4fc2d1ead34aac"
        },
        "estimated_bytes": 2_240_000_000,
    },
    "grounding-dino-tiny": {
        "label": "Grounding DINO Tiny",
        "purpose": "构图主体定位",
        "provider": "huggingface",
        "repo_id": "IDEA-Research/grounding-dino-tiny",
        "revision": "a2bb814dd30d776dcf7e30523b00659f4f141c71",
        "files": [
            "added_tokens.json",
            "config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.txt",
        ],
        "weight_sha256": {
            "model.safetensors": "1a2412ef99bd74bcd3c2a246fa1e48581f8889a1300c9051974741314fc042f3"
        },
        "estimated_bytes": 690_000_000,
    },
    "sam2-hiera-tiny": {
        "label": "SAM 2 Hiera Tiny",
        "purpose": "构图主体遮罩",
        "provider": "huggingface",
        "repo_id": "facebook/sam2-hiera-tiny",
        "revision": "7c218beaf0bb87874785f32b582f640134fc1c09",
        "files": [
            "config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "processor_config.json",
        ],
        "weight_sha256": {
            "model.safetensors": "1449035ba6ba2ed524c646da989e5cb3a9b93b97f1c1b2d474e598c3b748d99f"
        },
        "estimated_bytes": 155_000_000,
    },
    "segformer-b2": {
        "label": "SegFormer B2",
        "purpose": "天空、水面与地景分割",
        "provider": "huggingface",
        "repo_id": "nvidia/segformer-b2-finetuned-ade-512-512",
        "revision": "de01bae28967510f9ddd496c60a969357195400c",
        "files": ["config.json", "preprocessor_config.json", "pytorch_model.bin"],
        "weight_sha256": {
            "pytorch_model.bin": "187ca07bea003a5717c63d04ea90b07f33cd033c0ebf44b4b89fce5070d6c8f3"
        },
        "estimated_bytes": 225_000_000,
    },
    "clip-vit-b32": {
        "label": "CLIP ViT-B/32",
        "purpose": "风格库语义召回",
        "provider": "huggingface",
        "repo_id": "openai/clip-vit-base-patch32",
        "revision": "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268",
        "files": [
            "config.json",
            "merges.txt",
            "preprocessor_config.json",
            "pytorch_model.bin",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.json",
        ],
        "weight_sha256": {
            "pytorch_model.bin": "a63082132ba4f97a80bea76823f544493bffa8082296d62d71581a4feff1576f"
        },
        "estimated_bytes": 610_000_000,
        "legacy_path": "models/clip-vit-base-patch32",
    },
    "qwen3-vl-4b": {
        "label": "Qwen3-VL 4B",
        "purpose": "8GB 档场景理解与深度评审",
        "provider": "ollama",
        "ollama_name": "qwen3-vl:4b-instruct-q4_K_M",
        "manifest_digest": "sha256:ee4b975b58c17ce268cd19d40db35d5edc64603035d2ffc1fee1968eb0947f7b",
        "estimated_bytes": 3_300_000_000,
    },
    "qwen3-vl-8b": {
        "label": "Qwen3-VL 8B",
        "purpose": "16GB 档场景理解与深度评审",
        "provider": "ollama",
        "ollama_name": "qwen3-vl:8b-instruct-q4_K_M",
        "manifest_digest": "sha256:0533d74300e4f9bc367d675d4e64ffd073d50ff16a2b4096cc2e8a1cf8c96319",
        "aliases": ["qwen3-vl:8b-instruct"],
        "estimated_bytes": 6_150_000_000,
    },
}

COMMON_MODEL_IDS = [
    "dinov2-base",
    "qrealign-mini",
    "grounding-dino-tiny",
    "sam2-hiera-tiny",
    "segformer-b2",
    "clip-vit-b32",
]
PROFILE_SPECS: dict[ProfileId, dict[str, Any]] = {
    "8gb": {
        "label": "8GB 显存",
        "description": "轻量档 · 4B 视觉模型，速度更快",
        "minimum_vram_mib": 7_000,
        "vlm_model_id": "qwen3-vl-4b",
        "model_ids": [*COMMON_MODEL_IDS, "qwen3-vl-4b"],
    },
    "16gb": {
        "label": "16GB 显存",
        "description": "完整档 · 8B 视觉模型，评审更细致",
        "minimum_vram_mib": 12_000,
        "vlm_model_id": "qwen3-vl-8b",
        "model_ids": [*COMMON_MODEL_IDS, "qwen3-vl-8b"],
    },
}

_HF_ALLOWED_SUFFIXES = {
    ".bin",
    ".json",
    ".jinja",
    ".model",
    ".py",
    ".safetensors",
    ".txt",
    ".yaml",
    ".yml",
}
_HF_EXCLUDED_PARTS = {
    "onnx",
    "openvino",
    "tensorflow",
    "tf_model.h5",
    "flax_model.msgpack",
    "rust_model.ot",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "model-resources" / "settings.json"


def load_model_resource_settings(data_dir: Path) -> dict[str, Any]:
    payload = (
        read_json(_settings_path(data_dir))
        if _settings_path(data_dir).is_file()
        else {}
    )
    profile = str(payload.get("active_profile") or "")
    return {
        "schema_version": MODEL_RESOURCE_SCHEMA,
        "active_profile": profile if profile in PROFILE_SPECS else None,
        "vlm_model": str(payload.get("vlm_model") or "") or None,
        "configured_at": payload.get("configured_at"),
    }


def _write_settings(
    data_dir: Path, profile_id: ProfileId, vlm_model: str
) -> dict[str, Any]:
    payload = {
        "schema_version": MODEL_RESOURCE_SCHEMA,
        "active_profile": profile_id,
        "vlm_model": vlm_model,
        "configured_at": _now(),
    }
    path = _settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)
    return payload


def _clear_settings(data_dir: Path) -> dict[str, Any]:
    payload = {
        "schema_version": MODEL_RESOURCE_SCHEMA,
        "active_profile": None,
        "vlm_model": None,
        "configured_at": _now(),
    }
    path = _settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)
    return payload


def activate_model_profile(profile_id: str, data_dir: Path) -> dict[str, Any]:
    """Publish a selected profile after its complete environment passed smoke tests."""

    if profile_id not in PROFILE_SPECS:
        raise ValueError("未知显存档位。")
    resource_id = str(PROFILE_SPECS[profile_id]["vlm_model_id"])
    return _write_settings(
        data_dir, profile_id, str(MODEL_SPECS[resource_id]["ollama_name"])
    )


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _portable_layout_root(runtime_root: Path) -> Path | None:
    runtime_root = Path(runtime_root).resolve()
    parent = runtime_root.parent
    marker = parent / "marker.json"
    if runtime_root.name.casefold() != "models" or not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return parent if payload.get("application") == "PhotoAI" else None


def _hf_hub_root(runtime_root: Path) -> Path:
    portable = _portable_layout_root(runtime_root)
    if portable is not None:
        return portable / "models" / "huggingface" / "hub"
    return Path(runtime_root) / "huggingface" / "hub"


def _ollama_models_root(runtime_root: Path) -> Path:
    portable = _portable_layout_root(runtime_root)
    if portable is not None:
        return portable / "models" / "ollama"
    return Path(runtime_root) / "ollama-models"


def _tools_root(runtime_root: Path) -> Path:
    portable = _portable_layout_root(runtime_root)
    return portable / "tools" if portable is not None else Path(runtime_root) / "tools"


def _downloads_root(runtime_root: Path) -> Path:
    portable = _portable_layout_root(runtime_root)
    return (
        portable / "downloads"
        if portable is not None
        else Path(runtime_root) / "downloads"
    )


def _temp_root(runtime_root: Path) -> Path:
    portable = _portable_layout_root(runtime_root)
    return portable / "temp" if portable is not None else Path(runtime_root) / "temp"


def _hf_cache_path(runtime_root: Path, repo_id: str) -> Path:
    return _hf_hub_root(runtime_root) / ("models--" + repo_id.replace("/", "--"))


def local_hf_model_path(runtime_root: Path, resource_id: str) -> Path:
    """Return the immutable, pinned local snapshot for one HF model."""

    spec = MODEL_SPECS.get(resource_id)
    if not spec or spec.get("provider") != "huggingface":
        raise ValueError(f"不是 Hugging Face 模型资源：{resource_id}")
    snapshot = (
        _hf_cache_path(runtime_root, str(spec["repo_id"]))
        / "snapshots"
        / str(spec["revision"])
    )
    if not snapshot.is_dir():
        raise FileNotFoundError(f"本地模型快照不存在：{resource_id}")
    return snapshot.resolve()


def _development_runtime_root() -> Path | None:
    """Return an explicitly selected source-checkout runtime, never a desktop root."""

    configured = os.environ.get("PHOTO_AI_LEGACY_RUNTIME_ROOT")
    if not configured or os.environ.get("PHOTO_AI_CONTENT_ROOT"):
        return None
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise RuntimeError("开发模型目录必须是绝对路径。")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("开发模型目录当前不可用。") from exc
    checkout = root.parent
    if (
        root.name.casefold() != ".runtime"
        or not (checkout / "pyproject.toml").is_file()
        or not (checkout / "src" / "landscape_culler" / "model_resources.py").is_file()
    ):
        raise RuntimeError("开发模型目录不属于 PhotoAI 源码工作区。")
    return root


def managed_hf_model_path(
    resource_id: str,
    *,
    models_root: Path | str | None = None,
) -> Path:
    """Resolve one complete, immutable snapshot from the owned Content Root.

    Runtime inference must never hand a repository ID to Transformers.  A clean
    installation intentionally has no mutable ``refs/main`` file, so accepting a
    repo ID here would make offline behavior depend on unrelated cache history.
    """

    configured = models_root or os.environ.get("PHOTO_AI_MODELS_DIR")
    development_root = None
    if configured is None or not str(configured).strip():
        development_root = _development_runtime_root()
        if development_root is None:
            raise RuntimeError(
                "受管模型目录尚未配置，请在“设置 → 资源”安装或修复当前模型套装。"
            )
        root = development_root
    else:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise RuntimeError("受管模型目录必须是绝对路径。")
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("受管模型目录当前不可用，请重新连接数据目录。") from exc
        portable = _portable_layout_root(root)
        if portable is None or root != (portable / "models").resolve():
            raise RuntimeError("受管模型目录不属于当前 PhotoAI 数据目录。")

    spec = MODEL_SPECS.get(resource_id)
    if not spec or spec.get("provider") != "huggingface":
        raise ValueError(f"不是 Hugging Face 模型资源：{resource_id}")
    revision = str(spec.get("revision") or "")
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise RuntimeError(f"模型资源没有固定 revision：{resource_id}")
    try:
        snapshot = local_hf_model_path(root, resource_id)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{spec['label']} 的固定本地快照缺失，请在“设置 → 资源”执行修复。"
        ) from exc

    repo_root = _hf_cache_path(root, str(spec["repo_id"])).resolve()
    if snapshot.parent.name != "snapshots" or snapshot.name != revision:
        raise RuntimeError(f"{spec['label']} 的本地快照 revision 不匹配。")
    for relative in spec.get("files", []):
        relative_path = Path(str(relative))
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise RuntimeError(f"{spec['label']} 的固定文件清单无效。")
        candidate = snapshot / relative_path
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(
                f"{spec['label']} 本地快照不完整，请在“设置 → 资源”执行修复。"
            ) from exc
        if not resolved.is_file() or not resolved.is_relative_to(repo_root):
            raise RuntimeError(f"{spec['label']} 本地快照包含无效文件。")
    return snapshot


def _hf_installed(
    runtime_root: Path,
    spec: dict[str, Any],
    *,
    calculate_size: bool = True,
) -> tuple[bool, int, list[str]]:
    repo_path = _hf_cache_path(runtime_root, str(spec["repo_id"]))
    locations = [repo_path]
    legacy = spec.get("legacy_path")
    if legacy:
        locations.append(runtime_root / str(legacy))
    existing = [path for path in locations if path.is_dir()]
    size = sum(_directory_size(path) for path in existing) if calculate_size else 0
    has_config = any((path / "config.json").is_file() for path in existing)
    if not has_config:
        has_config = any(path.joinpath("snapshots").is_dir() for path in existing)
    has_weight = any(
        item.suffix.casefold() in {".bin", ".safetensors", ".h5", ".msgpack"}
        for path in existing
        for item in path.rglob("*")
        if item.is_file()
    )
    return (
        bool(existing and has_config and has_weight),
        size,
        [str(path) for path in existing],
    )


def _hf_verification_roots(locations: list[str]) -> list[Path]:
    roots: list[Path] = []
    for value in locations:
        location = Path(value)
        snapshots = location / "snapshots"
        if snapshots.is_dir():
            roots.extend(path for path in snapshots.iterdir() if path.is_dir())
        roots.append(location)
    return roots


def _verify_hf(
    runtime_root: Path,
    spec: dict[str, Any],
    *,
    deep: bool = False,
) -> tuple[bool, list[str]]:
    installed, _size, locations = _hf_installed(
        runtime_root, spec, calculate_size=False
    )
    if not locations:
        return False, ["模型文件缺失"]
    issues: list[str] = []
    portable = _portable_layout_root(runtime_root)
    if portable is not None:
        roots = [
            _hf_cache_path(runtime_root, str(spec["repo_id"]))
            / "snapshots"
            / str(spec["revision"])
        ]
    else:
        roots = _hf_verification_roots(locations)
    for root in roots:
        config = root / "config.json"
        weights = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in {".bin", ".safetensors", ".h5", ".msgpack"}
        ]
        if not config.is_file() or not weights:
            continue
        try:
            if portable is not None:
                missing = [
                    filename
                    for filename in spec.get("files", [])
                    if not (root / str(filename)).is_file()
                ]
                if missing:
                    raise ValueError(f"缺少固定文件：{missing[0]}")
            payload = json.loads(config.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("config.json 不是对象")
            if not any(path.stat().st_size > 0 for path in weights):
                raise ValueError("权重文件为空")
            if deep and portable is not None:
                expected_weights = spec.get("weight_sha256") or {}
                for filename, expected in expected_weights.items():
                    matches = [path for path in weights if path.name == filename]
                    if not matches:
                        raise ValueError(f"缺少固定权重：{filename}")
                    if _sha256_file(matches[0]) != str(expected).casefold():
                        raise ValueError(f"权重摘要不匹配：{filename}")
        except (OSError, ValueError, TypeError) as exc:
            issues.append(f"模型文件损坏：{exc}")
            continue
        return True, []
    if installed:
        issues.append("模型配置或权重无法读取")
    else:
        issues.append("模型下载不完整")
    return False, issues


def _ollama_manifest_candidates(runtime_root: Path, name: str) -> list[Path]:
    model, _, tag = name.partition(":")
    tag = tag or "latest"
    base = (
        _ollama_models_root(runtime_root)
        / "manifests"
        / "registry.ollama.ai"
        / "library"
    )
    return [base / model / tag]


def _ollama_installed(
    runtime_root: Path, spec: dict[str, Any]
) -> tuple[bool, int, list[str], str | None]:
    names = [
        str(spec["ollama_name"]),
        *[str(value) for value in spec.get("aliases", [])],
    ]
    for name in names:
        for manifest in _ollama_manifest_candidates(runtime_root, name):
            if not manifest.is_file():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                size = sum(
                    int(item.get("size") or 0) for item in payload.get("layers", [])
                )
            except (OSError, ValueError, TypeError):
                size = manifest.stat().st_size
            return True, size, [str(manifest)], name
    return False, 0, [], None


def _ollama_blob_path(runtime_root: Path, digest: str) -> Path:
    return _ollama_models_root(runtime_root) / "blobs" / digest.replace(":", "-")


def _verify_ollama(
    runtime_root: Path,
    spec: dict[str, Any],
    *,
    deep: bool = False,
) -> tuple[bool, list[str]]:
    installed, _size, locations, _resolved_name = _ollama_installed(runtime_root, spec)
    if not installed or not locations:
        return False, ["模型清单缺失"]
    manifest = Path(locations[0])
    try:
        if deep and _portable_layout_root(runtime_root) is not None:
            expected_manifest = str(spec.get("manifest_digest") or "")
            if not _is_sha256_digest(expected_manifest):
                raise ValueError("未固定 Ollama 根清单摘要")
            if _sha256_file(manifest) != expected_manifest[7:]:
                raise ValueError("Ollama 根清单摘要不匹配")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        descriptors = [payload.get("config"), *list(payload.get("layers") or [])]
        descriptors = [item for item in descriptors if isinstance(item, dict)]
        if not descriptors:
            raise ValueError("清单没有模型分层")
        for item in descriptors:
            digest = str(item.get("digest") or "")
            expected_size = int(item.get("size") or 0)
            if not _is_sha256_digest(digest):
                raise ValueError("清单摘要无效")
            blob = _ollama_blob_path(runtime_root, digest)
            if not blob.is_file():
                raise ValueError(f"缺少模型分层 {digest[7:19]}")
            actual_size = blob.stat().st_size
            if expected_size > 0 and actual_size != expected_size:
                raise ValueError(f"模型分层大小不符 {digest[7:19]}")
            if deep and _sha256_file(blob) != digest[7:]:
                raise ValueError(f"模型分层摘要不符 {digest[7:19]}")
    except (OSError, ValueError, TypeError) as exc:
        return False, [f"模型下载不完整：{exc}"]
    return True, []


def _is_sha256_digest(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def _gpu_status() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total",
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
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
        first = next(line for line in result.stdout.splitlines() if line.strip())
        name, memory = [part.strip() for part in first.rsplit(",", 1)]
        return {"available": True, "name": name, "memory_total_mib": int(memory)}
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        return {"available": False, "name": None, "memory_total_mib": 0}


def _ollama_install_dir(runtime_root: Path) -> Path:
    return _tools_root(runtime_root) / f"ollama-{OLLAMA_VERSION}"


def _ollama_version_matches(executable: Path) -> bool:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    expected = re.escape(OLLAMA_VERSION.removeprefix("v"))
    return (
        re.search(rf"(?<![0-9.]){expected}(?![0-9.])", result.stdout + result.stderr)
        is not None
    )


def _ollama_component_status(
    runtime_root: Path, *, executable_self_test: bool = False
) -> dict[str, Any]:
    executable = _ollama_install_dir(runtime_root) / "ollama.exe"
    ready = False
    actual_hash = None
    if executable.is_file():
        try:
            with executable.open("rb") as handle:
                ready = executable.stat().st_size > 0 and handle.read(2) == b"MZ"
            if ready and _portable_layout_root(runtime_root) is not None:
                actual_hash = _sha256_file(executable)
                ready = actual_hash == OLLAMA_EXECUTABLE_SHA256
                if ready and executable_self_test:
                    ready = _ollama_version_matches(executable)
        except OSError:
            ready = False
    return {
        "id": "ollama",
        "label": "本地视觉模型运行组件",
        "version": OLLAMA_VERSION.removeprefix("v"),
        "installed": executable.is_file(),
        "verified": ready,
        "path": str(executable),
        "sha256": actual_hash,
        "automatic": True,
    }


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_ollama_component(runtime_root: Path) -> Path:
    download_dir = _downloads_root(runtime_root)
    archive = download_dir / OLLAMA_ARCHIVE_NAME
    current = _ollama_component_status(runtime_root)
    label = str(current["label"])
    if current["verified"]:
        archive.unlink(missing_ok=True)
        phase_start("ollama", label, 1, current=1, unit="项", cached=1)
        phase_end("ollama", label, 1, unit="项", cached=1)
        return Path(str(current["path"]))

    download_dir.mkdir(parents=True, exist_ok=True)
    if archive.is_file() and (
        archive.stat().st_size != OLLAMA_ARCHIVE_SIZE
        or _sha256_file(archive) != OLLAMA_ARCHIVE_SHA256
    ):
        archive.unlink()

    if archive.is_file():
        total = max(1, archive.stat().st_size)
        phase_start("ollama", label, total, current=total, unit="B", cached=total)
    else:
        part = archive.with_suffix(archive.suffix + ".part")
        if (
            part.is_file()
            and part.stat().st_size == OLLAMA_ARCHIVE_SIZE
            and _sha256_file(part) == OLLAMA_ARCHIVE_SHA256
        ):
            os.replace(part, archive)
            total = max(1, archive.stat().st_size)
            phase_start(
                "ollama", label, total, current=total, unit="B", cached=total
            )
        else:
            if part.is_file() and part.stat().st_size >= OLLAMA_ARCHIVE_SIZE:
                part.unlink()
            errors: list[str] = []
            for source_label, source_url in OLLAMA_ARCHIVE_SOURCES:
                try:
                    offset = part.stat().st_size if part.is_file() else 0
                    headers = {"User-Agent": "PhotoAI/0.9 local-runtime-installer"}
                    if offset:
                        headers["Range"] = f"bytes={offset}-"
                    request = urllib.request.Request(source_url, headers=headers)
                    with urllib.request.urlopen(request, timeout=600) as response:
                        partial = offset > 0 and int(response.getcode() or 0) == 206
                        if offset and not partial:
                            # Keep caches for every other resource, but restart
                            # this archive if an endpoint ignores byte ranges.
                            offset = 0
                        remaining = max(
                            0, int(response.headers.get("Content-Length") or 0)
                        )
                        total = max(1, offset + remaining)
                        if total != OLLAMA_ARCHIVE_SIZE:
                            raise RuntimeError(
                                f"{source_label} 返回的组件版本与固定版本不一致"
                            )
                        current_bytes = offset
                        if "meter" not in locals():
                            meter = _TransferTelemetry(
                                "ollama",
                                label,
                                total,
                                resumed=offset,
                                resource=OLLAMA_ARCHIVE_NAME,
                            )
                        meter.update(
                            current_bytes,
                            detail=f"连接 {source_label}",
                            force=True,
                        )
                        with part.open("ab" if partial else "wb") as output:
                            while True:
                                block = response.read(1024 * 1024)
                                if not block:
                                    break
                                output.write(block)
                                current_bytes += len(block)
                                meter.update(
                                    current_bytes,
                                    detail=(
                                        f"续传本地模型组件 · {source_label}"
                                        if offset
                                        else f"下载本地模型组件 · {source_label}"
                                    ),
                                )
                            output.flush()
                            os.fsync(output.fileno())
                    if part.stat().st_size != OLLAMA_ARCHIVE_SIZE:
                        raise RuntimeError(f"{source_label} 下载的组件大小不完整")
                    os.replace(part, archive)
                    if _sha256_file(archive) != OLLAMA_ARCHIVE_SHA256:
                        archive.unlink(missing_ok=True)
                        raise RuntimeError(f"{source_label} 下载的组件摘要不匹配")
                    meter.update(
                        OLLAMA_ARCHIVE_SIZE,
                        detail=f"下载完成 · {source_label}",
                        force=True,
                    )
                    break
                except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
                    errors.append(f"{source_label}: {exc}")
                    if archive.is_file():
                        archive.unlink(missing_ok=True)
            else:
                raise RuntimeError("本地模型运行组件下载源均不可用：" + "；".join(errors))

    if _sha256_file(archive) != OLLAMA_ARCHIVE_SHA256:
        archive.unlink(missing_ok=True)
        raise RuntimeError("本地模型运行组件下载校验失败，已删除损坏文件。")

    incoming = _temp_root(runtime_root) / f"ollama-install-{uuid.uuid4().hex}"
    incoming.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (incoming / member.filename).resolve()
                if not target.is_relative_to(incoming.resolve()):
                    raise RuntimeError("本地模型运行组件压缩包路径异常。")
            bundle.extractall(incoming)
        executable = next(incoming.rglob("ollama.exe"), None)
        if executable is None or executable.stat().st_size <= 0:
            raise RuntimeError("本地模型运行组件压缩包不完整。")
        source_root = executable.parent
        install_dir = _ollama_install_dir(runtime_root)
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_root), str(install_dir))
    finally:
        if incoming.exists():
            shutil.rmtree(incoming)

    verified = _ollama_component_status(runtime_root, executable_self_test=True)
    if not verified["verified"]:
        raise RuntimeError("本地模型运行组件安装后摘要或版本自检失败。")
    archive_size = max(1, archive.stat().st_size)
    archive.unlink()
    if "meter" in locals():
        meter.finish(
            resource="ollama.exe",
            detail="组件已下载、解压并通过版本校验",
        )
    else:
        phase_end("ollama", label, archive_size, unit="B", cached=archive_size)
    return Path(str(verified["path"]))


def _resource_status(
    runtime_root: Path,
    resource_id: str,
    *,
    deep_verification: bool = False,
) -> dict[str, Any]:
    spec = MODEL_SPECS[resource_id]
    selected_name = None
    if spec["provider"] == "ollama":
        installed, size, locations, selected_name = _ollama_installed(
            runtime_root, spec
        )
        verified, issues = _verify_ollama(runtime_root, spec, deep=deep_verification)
    else:
        installed, size, locations = _hf_installed(runtime_root, spec)
        verified, issues = _verify_hf(runtime_root, spec, deep=deep_verification)
    return {
        "id": resource_id,
        "label": spec["label"],
        "purpose": spec["purpose"],
        "provider": spec["provider"],
        "source": spec.get("repo_id") or spec.get("ollama_name"),
        "installed": installed,
        "verified": verified,
        "health": "ready" if verified else "invalid" if installed else "missing",
        "issues": issues,
        "installed_bytes": size,
        "estimated_bytes": int(spec["estimated_bytes"]),
        "locations": locations,
        "resolved_name": selected_name,
    }


def model_resources_status(runtime_root: Path, data_dir: Path) -> dict[str, Any]:
    runtime_root = Path(runtime_root).resolve()
    settings = load_model_resource_settings(data_dir)
    resources = {
        resource_id: _resource_status(runtime_root, resource_id)
        for resource_id in MODEL_SPECS
    }
    component = _ollama_component_status(runtime_root)
    gpu = _gpu_status()
    recommended: ProfileId = (
        "16gb" if int(gpu.get("memory_total_mib") or 0) >= 12_000 else "8gb"
    )
    profiles: list[dict[str, Any]] = []
    for profile_id, profile in PROFILE_SPECS.items():
        model_ids = list(profile["model_ids"])
        installed_count = sum(1 for key in model_ids if resources[key]["installed"])
        verified_count = sum(1 for key in model_ids if resources[key]["verified"])
        profiles.append(
            {
                "id": profile_id,
                "label": profile["label"],
                "description": profile["description"],
                "recommended": profile_id == recommended,
                "configured": profile_id == settings["active_profile"],
                "ready": verified_count == len(model_ids)
                and bool(component["verified"]),
                "installed_count": installed_count,
                "verified_count": verified_count,
                "model_count": len(model_ids),
                "installed_bytes": sum(
                    resources[key]["installed_bytes"] for key in model_ids
                ),
                "estimated_bytes": sum(
                    int(MODEL_SPECS[key]["estimated_bytes"]) for key in model_ids
                ),
                "model_ids": model_ids,
            }
        )
    installed_unique = [item for item in resources.values() if item["installed"]]
    return {
        "schema_version": MODEL_RESOURCE_SCHEMA,
        "runtime_root": str(runtime_root),
        "settings": settings,
        "gpu": gpu,
        "recommended_profile": recommended,
        "profiles": profiles,
        "resources": list(resources.values()),
        "components": [component],
        "installed_count": len(installed_unique),
        "installed_bytes": sum(
            int(item["installed_bytes"]) for item in installed_unique
        ),
    }


def active_model_profile_readiness(
    runtime_root: Path, data_dir: Path
) -> dict[str, Any]:
    runtime_root = Path(runtime_root).resolve()
    settings = load_model_resource_settings(data_dir)
    profile_id = settings.get("active_profile")
    component = _ollama_component_status(runtime_root)
    if profile_id not in PROFILE_SPECS:
        return {
            "ready": False,
            "active_profile": None,
            "label": None,
            "missing": [],
            "invalid": [],
            "component_ready": bool(component["verified"]),
            "message": "请先完整安装并启用一套 8GB 或 16GB AI 模型。",
        }
    profile = PROFILE_SPECS[profile_id]
    missing: list[str] = []
    invalid: list[str] = []
    for resource_id in profile["model_ids"]:
        spec = MODEL_SPECS[resource_id]
        if spec["provider"] == "ollama":
            installed, _size, _locations, _name = _ollama_installed(runtime_root, spec)
            verified, _issues = _verify_ollama(runtime_root, spec)
        else:
            installed, _size, _locations = _hf_installed(
                runtime_root, spec, calculate_size=False
            )
            verified, _issues = _verify_hf(runtime_root, spec)
        if not installed:
            missing.append(resource_id)
        elif not verified:
            invalid.append(resource_id)
    ready = not missing and not invalid and bool(component["verified"])
    if ready:
        message = f"{profile['label']} 模型与运行组件校验通过。"
    elif not component["verified"]:
        message = "本地 AI 运行组件尚未安装完整，请修复当前模型套装。"
    else:
        message = (
            f"{profile['label']} 模型不完整："
            f"缺少 {len(missing)} 个，需修复 {len(invalid)} 个。"
        )
    return {
        "ready": ready,
        "active_profile": profile_id,
        "label": profile["label"],
        "missing": missing,
        "invalid": invalid,
        "component_ready": bool(component["verified"]),
        "message": message,
    }


def active_vlm_model(data_dir: Path, fallback: str = "qwen3-vl:8b-instruct") -> str:
    settings = load_model_resource_settings(data_dir)
    configured = str(settings.get("vlm_model") or "").strip()
    if configured:
        return configured
    profile_id = settings.get("active_profile")
    if profile_id not in PROFILE_SPECS:
        return fallback
    resource_id = str(PROFILE_SPECS[profile_id]["vlm_model_id"])
    return str(MODEL_SPECS[resource_id]["ollama_name"])


def clip_model_reference(runtime_root: Path) -> str:
    if _portable_layout_root(runtime_root) is not None:
        return str(
            managed_hf_model_path("clip-vit-b32", models_root=runtime_root)
        )
    legacy = runtime_root / str(MODEL_SPECS["clip-vit-b32"]["legacy_path"])
    if (legacy / "config.json").is_file():
        return str(legacy)
    try:
        return str(local_hf_model_path(runtime_root, "clip-vit-b32"))
    except FileNotFoundError:
        return str(MODEL_SPECS["clip-vit-b32"]["repo_id"])


def _probe_hf_endpoint(
    endpoint: str, spec: dict[str, Any], *, timeout: float = 3.0
) -> tuple[float, float]:
    repo_id = urllib.parse.quote(str(spec["repo_id"]), safe="/")
    revision = urllib.parse.quote(str(spec["revision"]), safe="")
    candidates = [str(value) for value in spec.get("files", [])]
    filename = next((value for value in candidates if value.endswith("config.json")), None)
    filename = urllib.parse.quote(filename or "config.json", safe="/")
    url = f"{endpoint.rstrip('/')}/{repo_id}/resolve/{revision}/{filename}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PhotoAI/0.9 download-source-probe",
            "Range": "bytes=0-131071",
        },
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        sample = response.read(128 * 1024)
    elapsed = max(0.001, time.monotonic() - started)
    if not sample:
        raise OSError("下载源没有返回内容")
    return len(sample) / elapsed, elapsed


def _hf_endpoint_order(spec: dict[str, Any]) -> list[tuple[str, str]]:
    global _HF_ENDPOINT_SELECTION
    configured = str(os.environ.get("PHOTO_AI_HF_ENDPOINT") or "").strip().rstrip("/")
    candidates = list(HF_DOWNLOAD_ENDPOINTS)
    if configured and all(endpoint != configured for _label, endpoint in candidates):
        candidates.insert(0, ("自定义下载源", configured))
    with _HF_ENDPOINT_SELECTION_LOCK:
        selected = _HF_ENDPOINT_SELECTION
        if selected is None or selected not in candidates:
            # Measure the domestic mirrors against each other, never against
            # the overseas origin. The official source participates only when
            # every preferred endpoint is unreachable.
            domestic = [
                item for item in candidates if item[1] != "https://huggingface.co"
            ]
            measurements: list[tuple[float, float, str, str]] = []
            with ThreadPoolExecutor(max_workers=max(1, len(domestic))) as executor:
                futures = {
                    executor.submit(_probe_hf_endpoint, endpoint, spec): (label, endpoint)
                    for label, endpoint in domestic
                }
                for future, (label, endpoint) in futures.items():
                    try:
                        speed, elapsed = future.result()
                    except (OSError, TimeoutError, urllib.error.URLError):
                        continue
                    measurements.append((speed, -elapsed, label, endpoint))
            if measurements:
                _speed, _latency, label, endpoint = max(measurements)
                selected = (label, endpoint)
            else:
                selected = next(
                    (item for item in candidates if item[1] == "https://huggingface.co"),
                    candidates[0],
                )
            _HF_ENDPOINT_SELECTION = selected
    return [selected, *[item for item in candidates if item != selected]]


def _ollama_pull_sources(name: str) -> list[tuple[str, str]]:
    model, separator, tag = name.partition(":")
    tag = tag if separator else "latest"
    configured = str(os.environ.get("PHOTO_AI_OLLAMA_REGISTRY") or "").strip()
    registries = list(OLLAMA_MODEL_REGISTRIES)
    if configured:
        configured = configured.removeprefix("https://").removeprefix("http://")
        configured = configured.strip("/")
        if configured and all(registry != configured for _label, registry in registries):
            registries.insert(0, ("自定义 Ollama 下载源", configured))
    return [(label, f"{registry}/{model}:{tag}") for label, registry in registries]


def _canonical_ollama_name(name: str) -> str:
    """Compare Ollama names after expanding the default registry and namespace."""

    name = name.removeprefix("https://").removeprefix("http://").strip("/")
    parts = name.split("/")
    if ":" not in parts[-1]:
        parts[-1] += ":latest"
    if len(parts) == 1:
        parts = ["registry.ollama.ai", "library", *parts]
    elif "." not in parts[0] and ":" not in parts[0] and parts[0] != "localhost":
        parts = ["registry.ollama.ai", *parts]
    return "/".join(parts).casefold()


def _hf_files(spec: dict[str, Any], endpoint: str) -> list[tuple[str, int]]:
    from huggingface_hub import HfApi

    repo_id = str(spec["repo_id"])
    revision = str(spec["revision"])
    selected = {str(value) for value in spec.get("files", [])}
    info = HfApi(endpoint=endpoint).model_info(
        repo_id, revision=revision, files_metadata=True
    )
    files: list[tuple[str, int]] = []
    for sibling in info.siblings or []:
        name = str(sibling.rfilename)
        if selected and name not in selected:
            continue
        lowered = name.casefold()
        if any(part in lowered for part in _HF_EXCLUDED_PARTS):
            continue
        if Path(name).suffix.casefold() not in _HF_ALLOWED_SUFFIXES:
            continue
        files.append((name, max(0, int(sibling.size or 0))))
    if not any(
        Path(name).suffix.casefold() in {".bin", ".safetensors"} for name, _ in files
    ):
        raise RuntimeError(f"{repo_id} 没有找到可用的 PyTorch 权重。")
    return files


def _model_download_progress_class(
    progress_type: Any,
    meter: _TransferTelemetry,
    file_base: int,
    filename: str,
    label: str,
    resumed: int,
    source_label: str,
) -> type[Any]:
    class ModelDownloadProgress(progress_type):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)
            meter.update(
                file_base + int(self.n),
                resource=filename,
                detail=(
                    f"续传 {label} · {filename} · {source_label}"
                    if resumed
                    else f"下载 {label} · {filename} · {source_label}"
                ),
                force=True,
            )

        def update(self, amount: float = 1) -> bool | None:
            changed = super().update(amount)
            meter.update(
                file_base + int(self.n),
                resource=filename,
                detail=f"下载 {label} · {filename} · {source_label}",
            )
            return changed

    return ModelDownloadProgress


def _download_hf(runtime_root: Path, resource_id: str) -> None:
    from huggingface_hub import hf_hub_download
    from tqdm.auto import tqdm

    spec = MODEL_SPECS[resource_id]
    repo_id = str(spec["repo_id"])
    revision = str(spec["revision"])
    endpoints = _hf_endpoint_order(spec)
    files: list[tuple[str, int]] | None = None
    selected_source: tuple[str, str] | None = None
    metadata_errors: list[str] = []
    for source_label, endpoint in endpoints:
        try:
            files = _hf_files(spec, endpoint)
            selected_source = (source_label, endpoint)
            break
        except Exception as exc:  # Network/client errors vary by huggingface_hub version.
            metadata_errors.append(f"{source_label}: {exc}")
    if files is None or selected_source is None:
        raise RuntimeError("模型下载源均不可用：" + "；".join(metadata_errors))
    total = max(1, sum(max(size, 1) for _, size in files))
    current = 0
    cache_dir = _hf_hub_root(runtime_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    snapshot = (
        _hf_cache_path(runtime_root, repo_id) / "snapshots" / revision
    )
    cached_bytes = sum(
        max(size, 1)
        for filename, size in files
        if (snapshot / filename).is_file()
    )
    incomplete_bytes = sum(
        path.stat().st_size
        for path in _hf_cache_path(runtime_root, repo_id).glob("blobs/*.incomplete")
        if path.is_file()
    )
    resumed = min(total, cached_bytes + incomplete_bytes)
    meter = _TransferTelemetry(
        resource_id,
        str(spec["label"]),
        total,
        resumed=resumed,
        resource=str(spec["label"]),
    )
    meter.update(
        resumed,
        resource=str(spec["label"]),
        detail=f"自动选择下载源：{selected_source[0]}",
        force=True,
    )
    for filename, size in files:
        file_base = current
        cached_file = snapshot / filename
        if cached_file.is_file() and size > 0 and cached_file.stat().st_size != size:
            _remove_invalid_hf_file(cached_file, _hf_cache_path(runtime_root, repo_id))
        errors: list[str] = []
        ordered_sources = [
            selected_source,
            *[item for item in endpoints if item != selected_source],
        ]
        for source_label, endpoint in ordered_sources:
            progress_type = _model_download_progress_class(
                tqdm,
                meter,
                file_base,
                filename,
                str(spec["label"]),
                resumed,
                source_label,
            )
            try:
                hf_hub_download(
                    repo_id=repo_id,
                    revision=revision,
                    filename=filename,
                    cache_dir=cache_dir,
                    tqdm_class=progress_type,
                    endpoint=endpoint,
                )
                selected_source = (source_label, endpoint)
                break
            except Exception as exc:  # Preserve the partial cache, then fail over.
                errors.append(f"{source_label}: {exc}")
        else:
            raise RuntimeError(f"{filename} 下载失败：" + "；".join(errors))
        current += max(size, 1)
        meter.update(
            current,
            resource=filename,
            detail=f"已完成 {filename} · {selected_source[0]}",
            force=True,
        )
    meter.finish(detail=f"{spec['label']} 下载完成")


def _ollama_executable(runtime_root: Path) -> Path:
    configured = os.environ.get("PHOTO_AI_OLLAMA")
    portable_root = _portable_layout_root(runtime_root)
    if configured:
        configured_path = Path(configured).expanduser().resolve()
        if configured_path.is_file() and (
            portable_root is None
            or configured_path.is_relative_to((portable_root / "tools").resolve())
        ):
            return configured_path
    canonical = _ollama_install_dir(runtime_root) / "ollama.exe"
    if canonical.is_file():
        return canonical
    matches = sorted(
        _tools_root(runtime_root).glob("ollama-*/ollama.exe"), reverse=True
    )
    if not matches:
        raise RuntimeError("本地模型运行器尚未安装。请先安装应用运行组件。")
    return matches[0]


def _ollama_endpoint() -> str:
    return os.environ.get("PHOTO_AI_OLLAMA_ENDPOINT", DEFAULT_OLLAMA_ENDPOINT).rstrip(
        "/"
    )


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _ollama_request(
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 5.0,
    *,
    method: str | None = None,
):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{_ollama_endpoint()}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if payload is not None else "GET"),
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _ensure_ollama(runtime_root: Path, data_dir: Path) -> None:
    global _OWNED_OLLAMA_PROCESS
    # A managed ContentRoot must never probe or attach to the legacy fixed
    # development port.  Allocate its private loopback endpoint before the
    # first request; callers may still provide an explicit endpoint to reuse
    # an already-owned process.
    if (
        _portable_layout_root(runtime_root) is not None
        and "PHOTO_AI_OLLAMA_ENDPOINT" not in os.environ
    ):
        os.environ["PHOTO_AI_OLLAMA_ENDPOINT"] = (
            f"http://127.0.0.1:{_reserve_loopback_port()}"
        )
    try:
        with _ollama_request("/api/version", timeout=2):
            return
    except (OSError, urllib.error.URLError):
        pass
    if "PHOTO_AI_OLLAMA_ENDPOINT" not in os.environ:
        os.environ["PHOTO_AI_OLLAMA_ENDPOINT"] = (
            f"http://127.0.0.1:{_reserve_loopback_port()}"
        )
    executable = _ollama_executable(runtime_root)
    portable_root = _portable_layout_root(runtime_root)
    log_root = Path(
        os.environ.get("PHOTO_AI_LOGS_DIR")
        or (
            portable_root / "logs"
            if portable_root is not None
            else Path(data_dir) / "web"
        )
    )
    log_path = log_root / "ollama.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    endpoint = _ollama_endpoint()
    env.update(
        OLLAMA_HOST=endpoint.removeprefix("http://").removeprefix("https://"),
        OLLAMA_MODELS=str(_ollama_models_root(runtime_root)),
        OLLAMA_NO_CLOUD="true",
    )
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    with log_path.open("a", encoding="utf-8") as log:
        _OWNED_OLLAMA_PROCESS = subprocess.Popen(
            [str(executable), "serve"],
            cwd=runtime_root.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with _ollama_request("/api/version", timeout=2):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError("本地模型运行器未能启动。")


def ensure_owned_ollama(runtime_root: Path, data_dir: Path) -> None:
    """Ensure the private portable Ollama endpoint is accepting requests.

    A cached model profile does not pass through ``_download_ollama``.  The
    post-install smoke test still needs a live endpoint, so callers must be
    able to start the owned process without forcing a model download.
    """

    _ensure_ollama(Path(runtime_root).resolve(), Path(data_dir).resolve())


def shutdown_owned_ollama() -> None:
    """Stop only an Ollama process started by this Python worker."""

    global _OWNED_OLLAMA_PROCESS
    process = _OWNED_OLLAMA_PROCESS
    _OWNED_OLLAMA_PROCESS = None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _download_ollama(runtime_root: Path, data_dir: Path, resource_id: str) -> None:
    spec = MODEL_SPECS[resource_id]
    label = str(spec["label"])
    name = str(spec["ollama_name"])
    _ensure_ollama(runtime_root, data_dir)
    meter = _TransferTelemetry(
        resource_id,
        label,
        int(spec["estimated_bytes"]),
        resource=name,
    )
    first_update = True
    layer_progress: dict[str, tuple[int, int]] = {}
    fallback_current = 0
    errors: list[str] = []
    selected_source = ""
    for source_label, pull_name in _ollama_pull_sources(name):
        try:
            meter.update(
                sum(value[0] for value in layer_progress.values()),
                resource=name,
                detail=f"连接 {source_label}",
                force=True,
            )
            with _ollama_request(
                # Socket idle timeout, not a cap on total download duration.
                # A stalled mirror must fall back instead of waiting 24 hours.
                "/api/pull", {"name": pull_name, "stream": True}, timeout=60
            ) as response:
                completed_pull = False
                for raw in response:
                    if not raw.strip():
                        continue
                    payload = json.loads(raw.decode("utf-8"))
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    if payload.get("status") == "success":
                        completed_pull = True
                    digest = str(payload.get("digest") or "").strip()
                    completed = max(0, int(payload.get("completed") or 0))
                    layer_total = max(completed, int(payload.get("total") or 0))
                    if digest:
                        previous_completed, previous_total = layer_progress.get(
                            digest, (0, 0)
                        )
                        layer_progress[digest] = (
                            max(previous_completed, completed),
                            max(previous_total, layer_total),
                        )
                        current = sum(value[0] for value in layer_progress.values())
                        reported_total = sum(value[1] for value in layer_progress.values())
                    else:
                        fallback_current = max(fallback_current, completed)
                        current = max(
                            fallback_current,
                            sum(value[0] for value in layer_progress.values()),
                        )
                        reported_total = max(
                            layer_total, sum(value[1] for value in layer_progress.values())
                        )
                    meter.total = max(meter.total, reported_total, current)
                    if first_update and current:
                        meter.resumed = min(current, meter.total)
                        meter.last_bytes = meter.resumed
                    first_update = False
                    status = str(payload.get("status") or f"下载 {label}")
                    meter.update(
                        current,
                        resource=name,
                        detail=f"{status} · {source_label}",
                        force=bool(payload.get("status") and not current),
                    )
            if not completed_pull:
                raise RuntimeError("下载连接提前结束，已下载的数据层会在重试时复用。")
            if _canonical_ollama_name(pull_name) != _canonical_ollama_name(name):
                with _ollama_request(
                    "/api/copy",
                    {"source": pull_name, "destination": name},
                    timeout=120,
                ):
                    pass
                # Keep the mirror's small manifest as a resume reference. Both
                # names share blobs; no model weights are duplicated. Never
                # delete by an unnormalised name: the official FQDN and short
                # name identify the same manifest inside Ollama.
            ready, issues = _verify_ollama(runtime_root, spec)
            if not ready:
                raise RuntimeError("下载完成但模型未就绪：" + "；".join(issues))
            selected_source = source_label
            break
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"{source_label}: {exc}")
    else:
        raise RuntimeError(f"{label} 下载源均不可用：" + "；".join(errors))
    meter.finish(resource=name, detail=f"{label} 下载完成 · {selected_source}")


def _remove_invalid_hf_file(path: Path, repository: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(repository.resolve()):
        raise RuntimeError("拒绝清理受管模型缓存以外的文件。")
    # HF snapshots can reference a cached blob. Remove that bad blob as well
    # or hf_hub_download would just relink it. Never touch *.incomplete files.
    if path.is_symlink():
        path.unlink()
    resolved.unlink(missing_ok=True)


def _prepare_hf_resource_repair(runtime_root: Path, spec: dict[str, Any]) -> None:
    hub_root = _hf_hub_root(runtime_root).resolve()
    target = _hf_cache_path(runtime_root, str(spec["repo_id"]))
    if target.parent.resolve() != hub_root:
        raise RuntimeError("拒绝清理受管模型目录以外的 Hugging Face 资源。")
    snapshot = target / "snapshots" / str(spec["revision"])
    for filename in spec.get("files", []):
        path = snapshot / str(filename)
        if not path.is_file():
            continue
        invalid = path.stat().st_size == 0
        if path.suffix == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                invalid = True
        if invalid:
            _remove_invalid_hf_file(path, target)


def _prepare_ollama_resource_repair(runtime_root: Path, spec: dict[str, Any]) -> None:
    names = [
        str(spec["ollama_name"]),
        *[str(value) for value in spec.get("aliases", [])],
    ]
    for name in names:
        for manifest in _ollama_manifest_candidates(runtime_root, name):
            if not manifest.is_file():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            layers = payload.get("layers")
            descriptors = [
                payload.get("config"),
                *(layers if isinstance(layers, list) else []),
            ]
            for item in descriptors:
                if not isinstance(item, dict):
                    continue
                digest = str(item.get("digest") or "")
                if not _is_sha256_digest(digest):
                    continue
                blob = _ollama_blob_path(runtime_root, digest)
                if not blob.is_file():
                    continue
                expected_size = int(item.get("size") or 0)
                try:
                    invalid = expected_size > 0 and blob.stat().st_size != expected_size
                except OSError:
                    invalid = True
                if invalid:
                    blob.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)


def _prepare_resource_repair(runtime_root: Path, resource_id: str) -> None:
    spec = MODEL_SPECS[resource_id]
    if spec["provider"] == "ollama":
        _prepare_ollama_resource_repair(runtime_root, spec)
    else:
        _prepare_hf_resource_repair(runtime_root, spec)


def configure_model_profile(
    profile_id: str,
    runtime_root: Path,
    data_dir: Path,
    *,
    publish_settings: bool = True,
    allow_download: bool = True,
) -> dict[str, Any]:
    if profile_id not in PROFILE_SPECS:
        raise ValueError("未知显存档位。")
    runtime_root = Path(runtime_root).resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    profile = PROFILE_SPECS[profile_id]
    if allow_download:
        _download_ollama_component(runtime_root)
    elif not _ollama_component_status(runtime_root)["verified"]:
        raise RuntimeError("离线资源缺少可用的本地视觉模型运行组件。")
    for resource_id in profile["model_ids"]:
        spec = MODEL_SPECS[resource_id]
        estimated = max(1, int(spec["estimated_bytes"]))
        phase_start(resource_id, str(spec["label"]), estimated, unit="B")
        current = _call_with_progress_heartbeat(
            lambda resource_id=resource_id: _resource_status(
                runtime_root, resource_id, deep_verification=False
            ),
            phase=resource_id,
            label=str(spec["label"]),
            current=0,
            total=estimated,
            resource=str(spec["label"]),
            detail=(
                "检查本地缓存与模型文件"
                if allow_download
                else "检查离线文件结构"
            ),
            unit="B",
        )
        if current["verified"]:
            phase_end(
                resource_id,
                current["label"],
                estimated,
                unit="B",
                cached=estimated,
            )
            continue
        if not allow_download:
            raise RuntimeError(f"离线资源不完整：{current['label']} 未通过校验。")
        if current["installed"]:
            _prepare_resource_repair(runtime_root, resource_id)
        if spec["provider"] == "ollama":
            _download_ollama(runtime_root, data_dir, resource_id)
        else:
            _download_hf(runtime_root, resource_id)
    phase_start("verify", "校验整套模型", len(profile["model_ids"]), unit="项")
    verified: dict[str, dict[str, Any]] = {}
    for index, resource_id in enumerate(profile["model_ids"], start=1):
        resource_label = str(MODEL_SPECS[resource_id]["label"])
        current = _call_with_progress_heartbeat(
            lambda resource_id=resource_id: _resource_status(
                runtime_root, resource_id, deep_verification=False
            ),
            phase="verify",
            label="校验整套模型",
            current=index - 1,
            total=len(profile["model_ids"]),
            resource=resource_label,
            detail=(
                f"检查 {resource_label} 模型文件"
                if allow_download
                else f"检查 {resource_label} 文件结构"
            ),
        )
        verified[resource_id] = current
        emit_progress(
            "verify",
            "校验整套模型",
            index,
            len(profile["model_ids"]),
            unit="项",
            detail=(
                f"{resource_label} 校验通过"
                if current["verified"]
                else f"{resource_label}：{'；'.join(current.get('issues') or ['校验失败'])}"
            ),
            current_resource=resource_label,
            elapsed_seconds=0,
            heartbeat_at=heartbeat_timestamp(),
        )
    failed = [value for value in verified.values() if not value["verified"]]
    if failed:
        details = "；".join(
            f"{value['label']}：{'、'.join(value.get('issues') or ['校验未通过'])}"
            for value in failed[:3]
        )
        raise RuntimeError(f"模型未安装完整：{details}。点击重试可复用已下载文件。")
    if not _ollama_component_status(runtime_root)["verified"]:
        raise RuntimeError("本地 AI 运行组件完整性校验未通过。")
    phase_end("verify", "校验整套模型", len(profile["model_ids"]), unit="项")
    vlm_resource_id = str(profile["vlm_model_id"])
    vlm_status = verified[vlm_resource_id]
    vlm_model = str(
        vlm_status.get("resolved_name") or MODEL_SPECS[vlm_resource_id]["ollama_name"]
    )
    if publish_settings:
        phase_start("apply", "应用显存方案", 1, unit="项")
        _write_settings(
            data_dir, profile_id, vlm_model
        )  # only publish after every required model is ready
        os.environ["PHOTO_AI_VLM_MODEL"] = active_vlm_model(data_dir)
        phase_end("apply", "应用显存方案", 1, unit="项")
    return model_resources_status(runtime_root, data_dir)


def _delete_ollama(runtime_root: Path, data_dir: Path, spec: dict[str, Any]) -> None:
    installed, _size, _locations, resolved_name = _ollama_installed(runtime_root, spec)
    if not installed or not resolved_name:
        return
    _ensure_ollama(runtime_root, data_dir)
    with _ollama_request(
        "/api/delete",
        {"name": resolved_name},
        timeout=120,
        method="DELETE",
    ):
        pass


def delete_model_resource(
    resource_id: str, runtime_root: Path, data_dir: Path
) -> dict[str, Any]:
    if resource_id not in MODEL_SPECS:
        raise ValueError("未知模型资源。")
    runtime_root = Path(runtime_root).resolve()
    spec = MODEL_SPECS[resource_id]
    if spec["provider"] == "ollama":
        try:
            _delete_ollama(runtime_root, data_dir, spec)
        finally:
            shutdown_owned_ollama()
    else:
        target = _hf_cache_path(runtime_root, str(spec["repo_id"]))
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        legacy = spec.get("legacy_path")
        if legacy:
            legacy_path = runtime_root / str(legacy)
            if legacy_path.is_symlink() or legacy_path.is_file():
                legacy_path.unlink()
            elif legacy_path.is_dir():
                shutil.rmtree(legacy_path)
    return model_resources_status(runtime_root, data_dir)


def delete_model_profile(
    profile_id: str, runtime_root: Path, data_dir: Path
) -> dict[str, Any]:
    if profile_id not in PROFILE_SPECS:
        raise ValueError("未知显存档位。")

    before = model_resources_status(runtime_root, data_dir)
    resource_status = {item["id"]: item for item in before["resources"]}
    other_profiles = [item for item in before["profiles"] if item["id"] != profile_id]
    protected: set[str] = set()
    for other in other_profiles:
        other_spec = PROFILE_SPECS[other["id"]]
        # Keep the shared stack when the user has installed any part unique to the
        # other profile.  Deleting one VRAM option must not silently break the other.
        unique_ids = set(other_spec["model_ids"]) - set(
            PROFILE_SPECS[profile_id]["model_ids"]
        )
        if any(resource_status[item]["installed"] for item in unique_ids):
            protected.update(other_spec["model_ids"])

    for resource_id in reversed(PROFILE_SPECS[profile_id]["model_ids"]):
        if resource_id in protected:
            continue
        delete_model_resource(resource_id, runtime_root, data_dir)

    settings = load_model_resource_settings(data_dir)
    if settings["active_profile"] == profile_id:
        replacement = next(
            (item["id"] for item in other_profiles if item["ready"]), None
        )
        if replacement:
            activate_model_profile(replacement, data_dir)
        else:
            _clear_settings(data_dir)
    return model_resources_status(runtime_root, data_dir)
