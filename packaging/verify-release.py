"""Fail-closed release gates for the lightweight Windows core distribution."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PRODUCT_VERSION = "0.9.0-beta.2"
PYTHON_PACKAGE_VERSION = "0.9.0b2"
FORBIDDEN_ARCHIVE_MODULE = re.compile(
    r"^\s*(?:"
    r"torch|torchvision|torchaudio|transformers|tokenizers|safetensors|"
    r"cv2|opencv|OpenColorIO|PyOpenColorIO|pyvips|rawpy|pyiqa|"
    r"huggingface_hub|open_clip|timm|numpy|scipy|sklearn|pandas|pyarrow"
    r")(?:[./\\]|$)",
    re.IGNORECASE,
)
FORBIDDEN_CORE_PATH = re.compile(
    r"(^|[./\\])(?:"
    r"torch|torchvision|torchaudio|transformers|tokenizers|safetensors|"
    r"cv2|opencv|opencolorio|pyopencolorio|pyvips|rawpy|pyiqa|"
    r"huggingface|open_clip|timm|numpy|scipy|sklearn|pandas|pyarrow|"
    r"ollama|models?"
    r")(?:[./\\]|$)",
    re.IGNORECASE,
)
MODEL_WEIGHT_SUFFIXES = {".bin", ".ckpt", ".gguf", ".onnx", ".pt", ".pth", ".safetensors"}


class ReleaseGateError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_sidecar(path: Path) -> None:
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        raise ReleaseGateError(f"缺少 SHA-256 sidecar：{sidecar}")
    expected = sidecar.read_text(encoding="ascii").strip().split()[0].casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ReleaseGateError(f"SHA-256 sidecar 格式无效：{sidecar}")
    if sha256(path) != expected:
        raise ReleaseGateError(f"摘要不匹配：{path}")


def archive_listing(executable: Path) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller.utils.cliutils.archive_viewer",
            "--recursive",
            "--brief",
            str(executable),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise ReleaseGateError(
            f"无法检查 PyInstaller archive：{executable}\n{result.stderr[-2000:]}"
        )
    return result.stdout


def verify_core(root: Path, executable_name: str, protocol: str) -> None:
    executable = root / executable_name
    if not executable.is_file():
        raise ReleaseGateError(f"缺少 sidecar：{executable}")
    verify_sidecar(executable)

    if executable.stat().st_size > 180 * 1024 * 1024:
        raise ReleaseGateError(f"sidecar 主程序异常过大：{executable}")
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if FORBIDDEN_CORE_PATH.search(relative):
            raise ReleaseGateError(f"轻量 core 意外包含 AI/图像依赖：{path}")
        if path.is_file() and path.suffix.casefold() in MODEL_WEIGHT_SUFFIXES:
            raise ReleaseGateError(f"轻量 core 意外包含模型权重：{path}")

    listing = archive_listing(executable)
    leaked = next(
        (line.strip() for line in listing.splitlines() if FORBIDDEN_ARCHIVE_MODULE.search(line)),
        None,
    )
    if leaked:
        raise ReleaseGateError(f"PyInstaller archive 含被禁止模块：{leaked}")

    result = subprocess.run(
        [str(executable), "--help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if result.returncode != 0:
        raise ReleaseGateError(
            f"sidecar --help 自检失败：{executable}\n{result.stderr[-2000:]}"
        )
    self_test = subprocess.run(
        [str(executable), "--self-test"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if (
        self_test.returncode != 0
        or protocol not in self_test.stdout
        or '"status":"passed"' not in self_test.stdout
    ):
        raise ReleaseGateError(f"sidecar --self-test 失败：{executable}")


def verify_worker_runtime(root: Path) -> None:
    executable = root / "PhotoAI.CoreWorker.exe"
    with tempfile.TemporaryDirectory(prefix="photoai-worker-gate-", dir=root.parent) as value:
        scratch = Path(value)
        spec = scratch / "job.json"
        result_path = scratch / "result.json"
        spec.write_text(
            json.dumps(
                {
                    "protocol": "PHOTO_AI_WORKER/1",
                    "job_id": "release-gate",
                    "command": "ping",
                    "payload": {"release": PRODUCT_VERSION},
                    "result_path": str(result_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(executable), "--job-spec", str(spec)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or '"event":"completed"' not in result.stdout:
            raise ReleaseGateError(
                f"CoreWorker 协议自检失败：{result.stderr[-2000:]}"
            )
        record = json.loads(result_path.read_text(encoding="utf-8"))
        if record.get("status") != "completed" or record.get("result", {}).get(
            "pong"
        ) is not True:
            raise ReleaseGateError("CoreWorker 原子结果自检失败。")

        install_probe = scratch / "installer-probe.json"
        install_result = scratch / "installer-probe-result.json"
        install_probe.write_text(
            json.dumps(
                {
                    "protocol": "PHOTO_AI_WORKER/1",
                    "job_id": "release-gate-installer",
                    "command": "ai-runtime-self-test",
                    "result_path": str(install_result),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        probe = subprocess.run(
            [str(executable), "--job-spec", str(install_probe)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if probe.returncode != 0:
            raise ReleaseGateError(
                f"CoreWorker AI 安装模块自检失败：{probe.stdout[-2000:]}"
            )
        install_record = json.loads(install_result.read_text(encoding="utf-8"))
        install_payload = install_record.get("result", {})
        if not (
            install_record.get("status") == "completed"
            and install_payload.get("ai_runtime") is True
            and install_payload.get("model_resources") is True
        ):
            raise ReleaseGateError("CoreWorker 缺少 AI 环境安装模块。")

        verify_xmp_cleanup_runtime(executable, scratch)


def verify_xmp_cleanup_runtime(executable: Path, scratch: Path) -> None:
    """Run the actual packaged CLI on disposable XMPs, never user photos."""
    source_root = Path(__file__).resolve().parents[1] / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from landscape_culler.xmp_cleanup import (
        create_xmp_cleanup_plan, plan_path, transactions_root,
    )

    photos = scratch / "cleanup-photos"
    (photos / "nested").mkdir(parents=True)
    xmp_files = [photos / "sample.xmp", photos / "nested" / "sample.XMP"]
    for path in xmp_files:
        path.write_bytes(b"<x:xmpmeta xmlns:x='adobe:ns:meta/' />")
    preserved = {
        photos / "sample.ARW": b"RAW fixture must not change",
        photos / "sample.jpg": b"JPEG fixture must not change",
        photos / "metadata.xml": b"XML fixture must not change",
        scratch / "outside.xmp": b"out-of-scope XMP must not change",
    }
    for path, payload in preserved.items():
        path.write_bytes(payload)
    data = scratch / "cleanup-state"
    plan = create_xmp_cleanup_plan(data, root=photos, recursive=True)
    result_path = scratch / "cleanup-result.json"
    spec = scratch / "cleanup-job.json"
    spec.write_text(json.dumps({
        "protocol": "PHOTO_AI_WORKER/1",
        "job_id": "release-gate-xmp-cleanup",
        "command": "cli",
        "argv": ["xmp-cleanup-execute", "--plan",
                 str(plan_path(data, plan["plan_id"])),
                 "--transactions-dir", str(transactions_root(data))],
        "result_path": str(result_path),
    }), encoding="utf-8")
    completed = subprocess.run(
        [str(executable), "--job-spec", str(spec)], check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not result_path.is_file():
        raise ReleaseGateError(
            f"CoreWorker XMP 清理实测失败：{completed.stdout[-2000:]}"
            f"\n{completed.stderr[-2000:]}"
        )
    record = json.loads(result_path.read_text(encoding="utf-8"))
    result = record.get("result", {})
    if (record.get("status") != "completed" or result.get("status") != "completed"
            or result.get("deleted_count") != 2 or result.get("remaining_count") != 0
            or any(path.exists() for path in xmp_files)
            or any(path.read_bytes() != payload for path, payload in preserved.items())):
        raise ReleaseGateError("CoreWorker XMP 清理结果或文件保护检查失败。")


def verify_service_runtime(root: Path) -> None:
    executable = root / "PhotoAI.Service.exe"
    with tempfile.TemporaryDirectory(prefix="photoai-service-gate-", dir=root.parent) as value:
        content_root = Path(value)
        process = subprocess.Popen(
            [str(executable), "--port", "0", "--content-root", str(content_root)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            assert process.stdout is not None
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(process.stdout.readline)
                try:
                    line = future.result(timeout=15).strip()
                except TimeoutError as exc:
                    raise ReleaseGateError(
                        "Service 在 15 秒内没有返回握手。"
                    ) from exc
            prefix = "PHOTO_AI_SERVICE/1 "
            if not line.startswith(prefix):
                raise ReleaseGateError(f"Service 握手协议无效：{line[:500]}")
            handshake = json.loads(line.removeprefix(prefix))
            origin = str(handshake.get("origin") or "")
            token = str(handshake.get("token") or "")
            control_token = str(handshake.get("control_token") or "")
            if (
                not re.fullmatch(r"http://127\.0\.0\.1:\d+", origin)
                or len(token) < 32
                or len(control_token) < 32
                or token == control_token
            ):
                raise ReleaseGateError("Service 握手端口或令牌无效。")
            request = urllib.request.Request(
                origin + "/api/service/shutdown",
                method="POST",
                headers={"Authorization": f"Bearer {control_token}"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status != 200:
                    raise ReleaseGateError("Service 认证关闭自检失败。")
            if process.wait(timeout=15) != 0:
                raise ReleaseGateError("Service 自检退出码非零。")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def read_worker_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise ReleaseGateError("AI Worker wheel 缺少唯一 METADATA。")
        metadata = archive.read(metadata_files[0]).decode("utf-8", "strict")
    match = re.search(r"^Version:\s*(\S+)\s*$", metadata, re.MULTILINE)
    if not match:
        raise ReleaseGateError("AI Worker wheel 未声明版本。")
    return match.group(1)


def verify_versions(project_root: Path) -> None:
    tauri = json.loads(
        (project_root / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )
    npm = json.loads((project_root / "desktop/package.json").read_text(encoding="utf-8"))
    cargo = (project_root / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    cargo_match = re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.MULTILINE)
    python_version = (project_root / "src/landscape_culler/version.py").read_text(
        encoding="utf-8"
    )
    python_match = re.search(
        r'^PRODUCT_VERSION\s*=\s*"([^"]+)"', python_version, re.MULTILINE
    )
    versions = {
        str(tauri.get("version")),
        str(npm.get("version")),
        cargo_match.group(1) if cargo_match else "",
        python_match.group(1) if python_match else "",
    }
    if versions != {PRODUCT_VERSION}:
        raise ReleaseGateError(f"产品版本不一致：{sorted(versions)}")


def verify_stage(stage: Path) -> None:
    manifests = stage / "manifests"
    tools = stage / "tools"
    required = [
        tools / "uv.exe",
        tools / "exiftool-13.59/exiftool-13.59_64/exiftool.exe",
        manifests / "ai-requirements.lock",
        manifests / "model-manifest.json",
        manifests / "release-manifest.json",
        stage / "integrations/photo-ai-lightroom.lrplugin/Info.lua",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ReleaseGateError("staging 缺少资源：" + ", ".join(missing))
    if (stage / "integrations/photo-ai-lightroom.lrplugin/bridge-path.txt").exists():
        raise ReleaseGateError("Lightroom 模板携带开发机 bridge-path.txt。")

    wheels = list((manifests / "ai-worker").glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseGateError("staging 必须包含唯一 AI Worker wheel。")
    if read_worker_version(wheels[0]) != PYTHON_PACKAGE_VERSION:
        raise ReleaseGateError("AI Worker wheel 版本与产品清单不一致。")

    verify_sidecar(tools / "uv.exe")
    verify_sidecar(tools / "exiftool-13.59/exiftool-13.59_64/exiftool.exe")
    verify_sidecar(wheels[0])
    verify_sidecar(manifests / "release-manifest.json")

    lock = (manifests / "ai-requirements.lock").read_text(encoding="utf-8")
    requirement_starts = [
        line
        for line in lock.splitlines()
        if line and not line.startswith((" ", "#", "--"))
    ]
    if not requirement_starts or "--hash=sha256:" not in lock:
        raise ReleaseGateError("AI requirements 不是带哈希的固定清单。")
    if any("==" not in line for line in requirement_starts):
        raise ReleaseGateError("AI requirements 包含未固定版本。")

    model_manifest = json.loads(
        (manifests / "model-manifest.json").read_text(encoding="utf-8")
    )
    source_root = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source_root))
    from landscape_culler.model_resources import MODEL_SPECS
    from landscape_culler.version import (
        AI_ENGINE_VERSION,
        API_VERSION,
        PYTHON_RUNTIME_VERSION,
        SERVICE_PROTOCOL,
        WORKER_PROTOCOL,
    )

    for model in model_manifest.get("huggingface", {}).values():
        if not re.fullmatch(r"[0-9a-f]{40}", str(model.get("revision", ""))):
            raise ReleaseGateError("模型清单包含非固定 Hugging Face revision。")
        for weight_hash in model.get("weight_files", {}).values():
            if not re.fullmatch(r"[0-9a-f]{64}", str(weight_hash)):
                raise ReleaseGateError("模型清单包含无效权重 SHA-256。")
    for model in model_manifest.get("ollama", {}).values():
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(model.get("manifest_digest", ""))):
            raise ReleaseGateError("模型清单包含可变 Ollama tag。")
    for resource_id, spec in MODEL_SPECS.items():
        provider = str(spec["provider"])
        declared = model_manifest.get(provider, {}).get(resource_id)
        if not isinstance(declared, dict):
            raise ReleaseGateError(f"模型清单缺少运行时资源：{resource_id}")
        if provider == "huggingface":
            if (
                declared.get("repo_id") != spec.get("repo_id")
                or declared.get("revision") != spec.get("revision")
                or declared.get("weight_files") != spec.get("weight_sha256")
            ):
                raise ReleaseGateError(f"模型清单与运行时代码不一致：{resource_id}")
        elif (
            declared.get("name") != spec.get("ollama_name")
            or declared.get("manifest_digest") != spec.get("manifest_digest")
        ):
            raise ReleaseGateError(f"模型清单与运行时代码不一致：{resource_id}")

    for path in stage.rglob("*"):
        if ".runtime" in path.parts:
            raise ReleaseGateError(f"发布资源包含开发 runtime：{path}")
        if path.is_file() and path.suffix.casefold() in MODEL_WEIGHT_SUFFIXES:
            raise ReleaseGateError(f"基础安装包包含模型权重：{path}")

    release = json.loads(
        (manifests / "release-manifest.json").read_text(encoding="utf-8-sig")
    )
    if release.get("version") != PRODUCT_VERSION:
        raise ReleaseGateError("release manifest 产品版本不一致。")
    expected_protocols = {
        "api_version": API_VERSION,
        "service_protocol": SERVICE_PROTOCOL,
        "worker_protocol": WORKER_PROTOCOL,
        "ai_engine_version": AI_ENGINE_VERSION,
        "managed_python_version": PYTHON_RUNTIME_VERSION,
    }
    for key, expected in expected_protocols.items():
        if release.get(key) != expected:
            raise ReleaseGateError(f"release manifest {key} 与运行时代码不一致。")
    if release.get("model_weights_included") is not False:
        raise ReleaseGateError("release manifest 未声明模型权重排除。")
    for entry in release.get("resources", []):
        path = stage / str(entry.get("path", ""))
        if not path.is_file():
            raise ReleaseGateError(f"release manifest 指向缺失资源：{path}")
        if path.stat().st_size != int(entry.get("size", -1)) or sha256(path) != entry.get(
            "sha256"
        ):
            raise ReleaseGateError(f"release manifest 资源漂移：{path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--service-dir", type=Path, required=True)
    parser.add_argument("--worker-dir", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify_versions(args.project_root.resolve())
        verify_core(
            args.service_dir.resolve(), "PhotoAI.Service.exe", "PHOTO_AI_SERVICE/1"
        )
        verify_core(
            args.worker_dir.resolve(), "PhotoAI.CoreWorker.exe", "PHOTO_AI_WORKER/1"
        )
        verify_worker_runtime(args.worker_dir.resolve())
        verify_service_runtime(args.service_dir.resolve())
        verify_stage(args.stage.resolve())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile, ReleaseGateError) as exc:
        print(f"release-gate: FAILED: {exc}", file=sys.stderr)
        return 1
    print("release-gate: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
