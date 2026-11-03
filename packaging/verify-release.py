"""Fail-closed release gates for the lightweight Windows core distribution."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.cookiejar
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

PRODUCT_VERSION = "0.10.0-beta.2"
PYTHON_PACKAGE_VERSION = "0.10.0b2"
FORBIDDEN_ARCHIVE_MODULE = re.compile(
    r"^\s*(?:"
    r"torch|torchvision|torchaudio|transformers|tokenizers|safetensors|"
    r"cv2|opencv|OpenColorIO|PyOpenColorIO|pyvips|rawpy|pyiqa|"
    r"huggingface_hub|open_clip|timm|scipy|sklearn|pandas|pyarrow"
    r")(?:[./\\]|$)",
    re.IGNORECASE,
)
FORBIDDEN_CORE_PATH = re.compile(
    r"(^|[./\\])(?:"
    r"torch|torchvision|torchaudio|transformers|tokenizers|safetensors|"
    r"cv2|opencv|opencolorio|pyopencolorio|pyvips|rawpy|pyiqa|"
    r"huggingface|open_clip|timm|scipy|sklearn|pandas|pyarrow|"
    r"ollama|models?"
    r")(?:[./\\]|$)",
    re.IGNORECASE,
)
MODEL_WEIGHT_SUFFIXES = {".bin", ".ckpt", ".gguf", ".onnx", ".pt", ".pth", ".safetensors"}
PREVIEW_ARCHIVE_MODULE = re.compile(r"^\s*(?:PIL|numpy)(?:[./\\]|$)", re.IGNORECASE)
PREVIEW_CORE_PATH = re.compile(r"(^|[./\\])(?:PIL|numpy)(?:[./\\]|$)", re.IGNORECASE)


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
    preview_dependencies = executable_name == "PhotoAI.Service.exe"
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
        if not preview_dependencies and PREVIEW_CORE_PATH.search(relative):
            raise ReleaseGateError(f"CoreWorker 意外包含预览依赖：{path}")
        if path.is_file() and path.suffix.casefold() in MODEL_WEIGHT_SUFFIXES:
            raise ReleaseGateError(f"轻量 core 意外包含模型权重：{path}")

    listing = archive_listing(executable)
    leaked = next(
        (line.strip() for line in listing.splitlines() if FORBIDDEN_ARCHIVE_MODULE.search(line)),
        None,
    )
    if leaked:
        raise ReleaseGateError(f"PyInstaller archive 含被禁止模块：{leaked}")
    if not preview_dependencies and any(PREVIEW_ARCHIVE_MODULE.search(line) for line in listing.splitlines()):
        raise ReleaseGateError("CoreWorker archive 意外包含 Pillow/NumPy。")
    if preview_dependencies:
        modules = {line.strip() for line in listing.splitlines()}
        missing = {
            "PIL.Image", "PIL.ImageEnhance", "PIL.ImageOps", "numpy",
            "landscape_culler.timelapse", "landscape_culler.timelapse_api",
            "landscape_culler._vendor.lrtlite.analysis",
            "landscape_culler._vendor.lrtlite.sequence",
            "landscape_culler._vendor.lrtlite.exif",
            "landscape_culler._vendor.lrtlite.xmp",
            "landscape_culler.collaborative_review",
            "landscape_culler.collaborative_review_api",
            "landscape_culler.collaborative_review_vision",
            "landscape_culler.collaborative_review_proxy",
            "landscape_culler.selection_export",
            "landscape_culler.toolbox_cli", "landscape_culler.toolbox_legacy_schemas",
            "landscape_culler.agent_connection",
        } - modules
        if missing:
            raise ReleaseGateError(f"Service 缺少工具箱或图片预览依赖：{sorted(missing)}")

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


def prepare_crop_probe(content_root: Path) -> dict:
    """Create existing modern/legacy recipes; never ask the service to run AI."""
    from PIL import Image
    from landscape_culler.content_root import initialize_content_root

    initialize_content_root(content_root, apply_environment=False, persist_registry=False)

    run_id = "20260101-120000-123456"
    run_dir = content_root / "projects" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    preview = content_root / "cache" / "previews" / "crop-probe.jpg"
    preview.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (180, 120), (36, 100, 164)).save(preview, "JPEG")
    bounds = {
        "original": (0., 0., 1., 1.), "balanced": (.1, .1, .9, .9),
        "tight": (.2, .2, .8, .8), "wide": (0., .2, 1., .8),
    }
    candidates = [{"id": key, "label": key, "bounds": dict(zip(
        ["left", "top", "right", "bottom"], value))} for key, value in bounds.items()]
    results, items, preserved = [], [], {}
    for index, style in enumerate(["lightroom", "golden"]):
        raw = content_root / f"crop-probe-{index}.ARW"
        raw.write_bytes(b"original RAW sentinel")
        preserved[raw] = raw.read_bytes()
        results.append({"path": str(raw), "preview": str(preview), "rating": 4,
                        "group_id": index + 1, "can_write_sidecar": True})
        items.append({"index": index, "path": str(raw), "filename": raw.name,
                      "group_id": index + 1, "rating": 4, "crop_candidates": candidates,
                      "crop_id": "original", "style_id": style, "style_strength": 70,
                      "style_options": [{"id": "lightroom"}], "base": {}, "angle": 0.,
                      "confirmed": False, "crop_confirmed": False})
    preserved[preview] = preview.read_bytes()
    results_file = run_dir / "results.json"
    results_file.write_text(json.dumps({"run_id": run_id, "input_root": str(content_root),
                                       "results": results}), encoding="utf-8")
    preserved[results_file] = results_file.read_bytes()
    # An installed project with any prior commit (including a failed write)
    # must still bootstrap and reopen after the desktop service restarts.
    transaction_file = run_dir / "xmp-commit-manifest-reopen-probe.json"
    transaction_file.write_text(json.dumps({
        "schema_version": 2, "commit": True, "created_at": "2026-10-31T12:00:00+00:00",
        "source_results": str(results_file), "skipped_count": 0,
        "records": [{"status": "failed_removed", "raw_path": results[0]["path"],
                     "xmp_path": str(Path(results[0]["path"]).with_suffix(".xmp"))}],
    }), encoding="utf-8")
    preserved[transaction_file] = transaction_file.read_bytes()
    plan_file = run_dir / "develop.json"
    plan_file.write_text(json.dumps({"schema_version": 5, "plan_id": "release-crop-probe",
                                    "run_id": run_id, "source_review_revision": 0,
                                    "revision": 0, "color_enabled": True, "items": items}), encoding="utf-8")
    return {"run_id": run_id, "plan_file": plan_file, "preserved": preserved}


def verify_service_crop_runtime(origin: str, token: str, probe: dict) -> None:
    """Exercise the packaged crop-edit endpoint, actual JPEG generation and persistence."""
    from PIL import Image

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(urllib.request.Request(origin + "/api/bootstrap", headers={
            "Authorization": f"Bearer {token}"}), timeout=15) as response:
        mutation_token = json.load(response)["token"]
    revision = 0
    expected_sizes = {"original": (180, 120), "balanced": (144, 96),
                      "tight": (108, 72), "wide": (180, 72)}
    for index in range(2):
        for crop_id in ["balanced", "tight", "wide", "original"]:
            request = urllib.request.Request(
                f"{origin}/api/runs/{probe['run_id']}/develop/{index}", method="PATCH",
                headers={"Content-Type": "application/json", "X-Photo-AI-Token": mutation_token},
                data=json.dumps({"base_revision": revision, "crop_id": crop_id}).encode())
            try:
                with opener.open(request, timeout=20) as response:
                    plan = json.load(response)
            except urllib.error.HTTPError as exc:
                raise ReleaseGateError(f"打包后台裁剪切换失败：照片 {index} / {crop_id}，HTTP {exc.code}") from exc
            revision += 1
            item = next(item for item in plan["items"] if item["index"] == index)
            if item.get("crop_id") != crop_id or plan.get("revision") != revision:
                raise ReleaseGateError("裁剪选择或方案版本未保存。")
            with opener.open(origin + item["preview_url"], timeout=15) as response:
                with Image.open(io.BytesIO(response.read())) as rendered:
                    rendered.load()
                    if rendered.format != "JPEG" or rendered.size != expected_sizes[crop_id]:
                        raise ReleaseGateError(f"裁剪预览尺寸错误：{crop_id} / {rendered.size}")
            persisted = json.loads(probe["plan_file"].read_text(encoding="utf-8"))
            if (persisted["items"][index]["crop_id"] != crop_id
                    or persisted["items"][index]["style_id"] != ["lightroom", "golden"][index]):
                raise ReleaseGateError("磁盘裁剪方案或既有配方未保留。")
    if any(path.read_bytes() != before for path, before in probe["preserved"].items()):
        raise ReleaseGateError("裁剪预览改变了原始照片、预览或评分文件。")


def verify_service_toolbox_runtime(origin: str, token: str, probe: dict,
                                   content_root: Path, *, require_media: bool) -> None:
    """Exercise the current product using its packaged HTTP service.

    Historical crop records are reader-only fixtures. New writes go through
    review and timelapse workspaces, keeping the source and legacy files intact.
    """
    from PIL import Image

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(urllib.request.Request(origin + "/api/bootstrap", headers={
            "Authorization": f"Bearer {token}"}), timeout=15) as response:
        mutation_token = json.load(response)["token"]

    def request(path: str, method="GET", payload=None, expected=200, raw=False):
        headers = {"Content-Type": "application/json", "X-Photo-AI-Token": mutation_token}
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(origin + path, data=data, method=method, headers=headers)
        try:
            response = opener.open(req, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code != expected:
                raise ReleaseGateError(f"工具箱运行检查 {method} {path}: HTTP {exc.code}: {exc.read()[:1500]!r}") from exc
            response = exc
        with response:
            body = response.read()
            if response.status != expected:
                raise ReleaseGateError(f"工具箱运行检查 {path}: expected {expected}, got {response.status}")
        return body if raw else json.loads(body)

    home = request("/", raw=True).decode("utf-8")
    if not all(name in home for name in ("摄影后期工具箱", 'data-toolbox-open="review"', 'data-toolbox-open="timelapse"')):
        raise ReleaseGateError("打包首页缺少摄影后期工具箱或新功能入口。")
    legacy_before = probe["plan_file"].read_bytes()
    request(f"/api/runs/{probe['run_id']}")
    request(f"/api/runs/{probe['run_id']}/develop")
    request(f"/api/runs/{probe['run_id']}/develop/0", "PATCH",
            {"base_revision": 0, "crop_id": "balanced"}, expected=410)
    if probe["plan_file"].read_bytes() != legacy_before:
        raise ReleaseGateError("旧项目读取或已移除接口修改了原有 develop 记录。")

    sequence = content_root / "toolbox-gate-sequence"
    sequence.mkdir()
    for index in range(12):
        with Image.new("RGB", (160, 90), (50 + index * 8, 90, 130)) as image:
            image.save(sequence / f"frame-{index:02}.jpg", "JPEG")
    originals = {path: path.read_bytes() for path in sequence.glob("*.jpg")}
    review = request("/api/review-workspaces", "POST", {"input_root": str(sequence), "name": "打包审片验收"})
    workspace_id = review["workspace_id"]
    asset_id = review["items"][0]["id"]
    saved = request(f"/api/review-workspaces/{workspace_id}/items/{asset_id}", "PATCH",
                    {"revision": review["revision"], "status": "keep", "memory": 3, "notes": "保留用户决定"})
    reopened = request(f"/api/review-workspaces/{workspace_id}")
    chosen = next(item for item in reopened["items"] if item["id"] == asset_id)
    if reopened["revision"] != saved["revision"] or chosen["user"]["memory"] != 3 or chosen["user"]["status"] != "keep":
        raise ReleaseGateError("打包审片选择未正确保存和重读。")
    preview = request(f"/api/review-workspaces/{workspace_id}/preview/{asset_id}", raw=True)
    with Image.open(io.BytesIO(preview)) as image:
        image.load()
        if image.size != (160, 90):
            raise ReleaseGateError("打包图片预览未保留原图尺寸。")

    prefix = "/api/toolbox/timelapse"
    capabilities = request(prefix + "/capabilities")
    if capabilities.get("engine") != "LRT Lite" or not capabilities.get("pillow"):
        raise ReleaseGateError("打包 LRT Lite 或 Pillow 能力不可用。")
    if require_media and not capabilities.get("can_render"):
        raise ReleaseGateError("候选声明包含媒体工具，但打包服务无法发现 FFmpeg/FFprobe。")

    def completed(job):
        deadline = time.monotonic() + 60
        while job["status"] in {"running", "queued"}:
            if time.monotonic() >= deadline:
                raise ReleaseGateError("工具箱合成样例任务超时。")
            time.sleep(.05)
            job = request(prefix + f"/jobs/{job['id']}")
        if job["status"] != "completed":
            raise ReleaseGateError(f"工具箱样例任务失败：{job.get('error')}")
        return job

    scanned = completed(request(prefix + "/scan", "POST", {"input_root": str(sequence)}))
    session_id = scanned["result"]["session_id"]
    processed = completed(request(prefix + f"/sessions/{session_id}/process", "POST",
                                  {"keyframe_mode": "even", "keyframe_count": 3, "holy_grail": False, "deflicker": True}))
    archive = request(prefix + f"/jobs/{processed['id']}/xmp", raw=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        if len([name for name in bundle.namelist() if name.endswith(".xmp")]) != 12:
            raise ReleaseGateError("打包延时摄影 XMP 导出数量错误。")
    if require_media:
        rendered = completed(request(prefix + f"/sessions/{session_id}/render", "POST",
                                     {"fps": 12, "width": 160, "height": 90, "crf": 20}))
        result = rendered["result"]
        if result.get("frames") != 12 or not result.get("verified"):
            raise ReleaseGateError("打包序列合成帧数验证失败。")
        video = request(prefix + f"/jobs/{rendered['id']}/video", raw=True)
        if len(video) < 100 or b"ftyp" not in video[:32]:
            raise ReleaseGateError("打包视频下载不是 MP4。")
    if any(path.read_bytes() != before for path, before in (originals | probe["preserved"]).items()):
        raise ReleaseGateError("工具箱运行检查修改了原片或旧项目。")
    print("release-gate: toolbox HTTP, legacy read-only, review persistence, JPEG preview, XMP"
          + (", portable MP4 verified" if require_media else " verified"))


def verify_cli_runtime(executable: Path, connection_path: Path, content_root: Path,
                       environment: dict, secrets_to_hide: list[str]) -> None:
    """Exercise the actual console binary against this service, without tokens in argv."""
    if not executable.is_file():
        raise ReleaseGateError("打包缺少 PhotoToolbox.CLI.exe。")
    verify_sidecar(executable)
    from landscape_culler.agent_connection import load_connection
    from PIL import Image

    descriptor = json.loads(connection_path.read_text(encoding="utf-8"))
    connection = load_connection(connection_path)
    forbidden = [*secrets_to_hide, *connection["headers"].values(), descriptor["credential"]["value"]]

    def command(arguments: list[str], *, success=True, connected=True):
        argv = [str(executable)]
        if connected:
            argv.extend(["--connection-file", str(connection_path)])
        argv.extend(arguments)
        result = subprocess.run(argv, cwd=executable.parent, env=environment,
                                capture_output=True, text=True, encoding="utf-8", errors="strict",
                                timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if any(secret and secret in (result.stdout + result.stderr) for secret in forbidden):
            raise ReleaseGateError("CLI 将认证凭据写入 stdout/stderr。")
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ReleaseGateError("CLI 必须返回单个 JSON 文档。") from exc
        if payload.get("protocol") != "photography-toolbox/1" or bool(payload.get("ok")) != success:
            raise ReleaseGateError(f"CLI 响应契约失败：{arguments[0]}")
        if (result.returncode == 0) != success:
            raise ReleaseGateError("CLI 退出码与 JSON 成功状态不符。")
        return payload.get("data")

    command(["schema", "--command", "review.decide"], connected=False)
    command(["capabilities"])
    command(["review", "list"])
    media = content_root / "cli-gate-photos"
    media.mkdir()
    with Image.new("RGB", (80, 60), (90, 80, 50)) as image:
        image.save(media / "source.jpg")
    original = (media / "source.jpg").read_bytes()
    review = command(["review", "create", "--input-root", str(media), "--name", "CLI 中文验收"])
    workspace_id = review["workspace_id"]
    asset_id = review["items"][0]["id"]
    command(["review", "decide", workspace_id, asset_id, "--revision", "0", "--status", "keep", "--memory", "3", "--notes", "CLI 用户决定"])
    reopened = command(["review", "get", workspace_id])
    chosen = next(item for item in reopened["items"] if item["id"] == asset_id)
    if chosen["user"]["status"] != "keep" or chosen["user"]["memory"] != 3 or chosen["user"]["notes"] != "CLI 用户决定":
        raise ReleaseGateError("CLI 用户选择未持久化或中文被损坏。")
    command(["review", "decide", workspace_id, asset_id, "--revision", "0", "--status", "reject"], success=False)
    if (media / "source.jpg").read_bytes() != original:
        raise ReleaseGateError("CLI 修改了源照片。")
    print("release-gate: real CLI JSON, DPAPI discovery, UTF-8, write/read, stale-revision rejection and secret-free output verified")


def verify_service_runtime(root: Path, resource_root: Path | None = None) -> None:
    executable = root / "PhotoAI.Service.exe"
    with tempfile.TemporaryDirectory(prefix="photoai-service-gate-", dir=root.parent) as value:
        content_root = Path(value)
        probe = prepare_crop_probe(content_root)
        environment = dict(os.environ)
        if resource_root is not None:
            environment["PHOTO_AI_RESOURCE_ROOT"] = str(resource_root)
        # Only bundled tools are available to the packaged-service acceptance
        # check. An installed Conda/Scoop copy must not hide missing resources.
        if os.name == "nt":
            environment["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
        for key in tuple(environment):
            if key.startswith(("PHOTO_TOOLBOX_", "CONDA_")) or key in {"PHOTO_AI_FFMPEG", "PHOTO_AI_FFPROBE", "LRT_EXIFTOOL"}:
                environment.pop(key, None)
        require_media = bool(resource_root and (resource_root / "tools" / "ffmpeg" / "ffmpeg.exe").is_file())
        process = subprocess.Popen(
            [str(executable), "--port", "0", "--content-root", str(content_root)],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
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
            verify_service_toolbox_runtime(origin, token, probe, content_root, require_media=require_media)
            connection_path = content_root / "state" / "cli-session.json"
            verify_cli_runtime(root / "PhotoToolbox.CLI.exe", connection_path, content_root,
                               environment, [token, control_token])
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
            if connection_path.exists():
                raise ReleaseGateError("Service 退出后遗留有效 CLI 凭据记录。")
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
    framepick = stage / "tools" / "framepick"
    for required in ("FramePick.exe", "LICENSE", "licenses/dotnet-LICENSE.TXT", "licenses/wpf-LICENSE"):
        if not (framepick / required).is_file():
            raise ReleaseGateError(f"缺少帧选组件或许可证：{required}")
    if (framepick / "Data").exists() or (framepick / "Cache").exists():
        raise ReleaseGateError("帧选发布载荷不得包含开发数据或缓存。")
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
        verify_service_runtime(args.service_dir.resolve(), args.stage.resolve())
        verify_stage(args.stage.resolve())
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile, ReleaseGateError) as exc:
        print(f"release-gate: FAILED: {exc}", file=sys.stderr)
        return 1
    print("release-gate: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
