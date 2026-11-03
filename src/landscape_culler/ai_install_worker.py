"""Heavy-environment phase of the post-install AI setup."""

from __future__ import annotations

import argparse
import base64
import gc
import io
import importlib
import json
import os
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .content_root import apply_runtime_environment, resolve_content_root
from .model_resources import (
    MODEL_SPECS,
    PROFILE_SPECS,
    configure_model_profile,
    ensure_owned_ollama,
    managed_hf_model_path,
    shutdown_owned_ollama,
)
from .progress import emit_progress, heartbeat_timestamp, phase_end, phase_start
from .util import write_json


def _release_cuda(torch: Any, *values: Any) -> None:
    del values
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


class _SmokeTelemetry:
    """Report model-by-model smoke progress even while one CUDA load is quiet."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.current = 0
        self.resource = "AI 运行组件"
        self.detail = "导入固定 AI 依赖"
        self.started = time.monotonic()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        phase_start("smoke", "实际运行 AI 自检", total, unit="项")
        self.worker = threading.Thread(target=self._heartbeat, daemon=True)
        self.worker.start()

    def _heartbeat(self) -> None:
        while not self.stopped.wait(1.0):
            with self.lock:
                current = self.current
                resource = self.resource
                detail = self.detail
            elapsed = max(0.0, time.monotonic() - self.started)
            emit_progress(
                "smoke",
                "实际运行 AI 自检",
                current,
                self.total,
                unit="项",
                detail=f"{detail}（已运行 {int(elapsed)} 秒）",
                current_resource=resource,
                elapsed_seconds=elapsed,
                heartbeat_at=heartbeat_timestamp(),
            )

    def advance(self, resource: str, detail: str) -> None:
        with self.lock:
            self.current = min(self.total, self.current + 1)
            self.resource = resource
            self.detail = detail
            current = self.current
        emit_progress(
            "smoke",
            "实际运行 AI 自检",
            current,
            self.total,
            unit="项",
            detail=detail,
            current_resource=resource,
            elapsed_seconds=max(0.0, time.monotonic() - self.started),
            heartbeat_at=heartbeat_timestamp(),
        )

    def working(self, resource: str, detail: str) -> None:
        with self.lock:
            self.resource = resource
            self.detail = detail
            current = self.current
        emit_progress(
            "smoke",
            "实际运行 AI 自检",
            current,
            self.total,
            unit="项",
            detail=detail,
            current_resource=resource,
            elapsed_seconds=max(0.0, time.monotonic() - self.started),
            heartbeat_at=heartbeat_timestamp(),
        )

    def finish(self, success: bool) -> None:
        self.stopped.set()
        self.worker.join(timeout=2)
        if success:
            phase_end("smoke", "AI 自检通过", self.total, unit="项")


def _run_smoke(layout: Any, profile_id: str) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    telemetry = _SmokeTelemetry(5)
    success = False
    try:
        import cv2
        import rawpy
        import torch
        import transformers

        checks["imports"] = {
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "opencv": cv2.__version__,
            "rawpy": rawpy.__version__,
        }
        telemetry.advance("AI 运行组件", "固定 AI 依赖可正常导入")
        if not torch.cuda.is_available():
            raise RuntimeError("Torch CUDA 不可用。")
        device = torch.device("cuda:0")
        value = (
            torch.ones((32, 32), device=device)
            @ torch.ones((32, 32), device=device)
        ).sum()
        torch.cuda.synchronize(device)
        checks["torch_cuda"] = {
            "device": torch.cuda.get_device_name(0),
            "result": float(value.item()),
        }
        telemetry.advance("Torch CUDA", "GPU 张量运算通过")
        from PIL import Image
        from transformers import (
            AutoImageProcessor,
            AutoModel,
        )

        image = Image.new("RGB", (96, 96), "#708090")

        telemetry.working("DINOv2 Base", "加载视觉特征模型到 GPU")
        dino_path = managed_hf_model_path("dinov2-base", models_root=layout.models)
        dino_processor = AutoImageProcessor.from_pretrained(
            dino_path, local_files_only=True
        )
        dino = (
            AutoModel.from_pretrained(dino_path, local_files_only=True)
            .eval()
            .to(device)
        )
        dino_inputs = {
            key: tensor.to(device)
            for key, tensor in dino_processor(images=image, return_tensors="pt").items()
        }
        with torch.inference_mode():
            dino_output = dino(**dino_inputs).last_hidden_state
        checks["dinov2"] = {
            "shape": list(dino_output.shape),
            "snapshot": dino_path.name,
        }
        del dino_output, dino_inputs, dino, dino_processor
        _release_cuda(torch)
        telemetry.advance("DINOv2 Base", "视觉特征实测通过")

        import pyiqa

        telemetry.working("Q-ReAlign Mini", "加载审美质量模型到 GPU")
        with tempfile.TemporaryDirectory(dir=layout.temp) as temporary:
            sample = Path(temporary) / "smoke.jpg"
            image.save(sample, "JPEG", quality=90)
            qrealign_path = managed_hf_model_path(
                "qrealign-mini", models_root=layout.models
            )
            metric = pyiqa.create_metric(
                "qrealign", device="cuda", model=str(qrealign_path)
            )
            with torch.inference_mode():
                qrealign_value = metric(str(sample), task_="quality")
            checks["qrealign"] = {
                "score": float(qrealign_value.reshape(-1)[0].item())
            }
            del qrealign_value, metric
        _release_cuda(torch)
        telemetry.advance("Q-ReAlign Mini", "审美质量实测通过")

        from .vision_provider import cloud_enabled
        if cloud_enabled(layout.state):
            # Cloud connectivity is tested explicitly in Settings, never charged by installation.
            checks["cloud_vision"] = {"configured": True, "remote_inference_tested": False}
            checks["profile"] = profile_id
            checks["content_root"] = str(layout.root)
            success = True
            return checks
        endpoint = os.environ.get("PHOTO_AI_OLLAMA_ENDPOINT", "").rstrip("/")
        if not endpoint:
            raise RuntimeError("Ollama 自检端口未建立。")
        vlm_resource_id = str(PROFILE_SPECS[profile_id]["vlm_model_id"])
        vlm_model = str(MODEL_SPECS[vlm_resource_id]["ollama_name"])
        telemetry.working(vlm_model, "启动视觉语言模型并进行首次推理")
        image_buffer = io.BytesIO()
        image.save(image_buffer, "JPEG")
        request = urllib.request.Request(
            f"{endpoint}/api/generate",
            data=json.dumps(
                {
                    "model": vlm_model,
                    "prompt": "Describe the main color of this image in a few words.",
                    "images": [base64.b64encode(image_buffer.getvalue()).decode("ascii")],
                    "stream": False,
                    "think": False,
                    "keep_alive": 0,
                    "options": {"num_predict": 16, "temperature": 0},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=240) as response:
            ollama_result = json.loads(response.read().decode("utf-8"))
        if not str(ollama_result.get("response") or "").strip():
            raise RuntimeError("Qwen3-VL 实际推理没有返回内容。")
        checks["ollama_qwen"] = {
            "model": vlm_model,
            "completed": True,
            "vision_tested": True,
        }
        telemetry.advance(vlm_model, "视觉语言模型实际推理通过")

        checks["profile"] = profile_id
        checks["content_root"] = str(layout.root)
        success = True
        return checks
    except Exception as exc:
        raise RuntimeError(
            f"{telemetry.resource} 实际运行自检失败：{exc}。"
            "可重试此步骤；若反复失败，请删除该模型后重新安装。"
        ) from exc
    finally:
        telemetry.finish(success)


def install_models_and_smoke(
    content_root: Path,
    profile_id: str,
    result: Path,
    *,
    offline: bool = False,
) -> None:
    # This Worker intentionally runs below CONTENT_ROOT/runtimes. The desktop
    # install-location guard must not reject the already-owned root merely
    # because the managed Python executable is one of its descendants.
    layout = resolve_content_root(
        content_root,
        apply_environment=False,
        allow_managed_runtime=True,
    )
    apply_runtime_environment(layout)
    offline_value = "1" if offline else "0"
    os.environ.update(
        {
            "HF_HUB_OFFLINE": offline_value,
            "TRANSFORMERS_OFFLINE": offline_value,
            "UV_OFFLINE": offline_value,
            "PHOTO_AI_CONTENT_ROOT": str(layout.root),
        }
    )
    payload: dict[str, Any]
    try:
        model_state = configure_model_profile(
            profile_id,
            layout.models,
            layout.state,
            publish_settings=False,
            allow_download=not offline,
        )
        os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        # A fully cached Ollama model skips the download path that normally
        # starts the private server.  Always establish the endpoint before
        # the Qwen smoke request so revalidation behaves like a first install.
        from .vision_provider import cloud_enabled
        if not cloud_enabled(layout.state):
            ensure_owned_ollama(layout.models, layout.state)
        checks = _run_smoke(layout, profile_id)
        payload = {
            "schema_version": 1,
            "status": "passed",
            "profile_id": profile_id,
            "models_ready": any(
                item.get("id") == profile_id and item.get("ready")
                for item in model_state.get("profiles", [])
            ),
            "checks": checks,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        if not payload["models_ready"]:
            raise RuntimeError("模型套装未通过完整性门禁。")
    except Exception as exc:
        payload = {
            "schema_version": 1,
            "status": "failed",
            "profile_id": profile_id,
            "error": str(exc),
            "completed_at": datetime.now(UTC).isoformat(),
        }
        write_json(result, payload)
        raise
    finally:
        shutdown_owned_ollama()
    write_json(result, payload)


def check_worker_code(content_root: Path, profile_id: str, result: Path) -> None:
    from .constants import AI_WORKER_CODE_VERSION, VLM_PROMPT_VERSION
    layout = resolve_content_root(content_root, apply_environment=False, allow_managed_runtime=True)
    apply_runtime_environment(layout)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", UV_OFFLINE="1")
    modules = ["scoring", "cli"]
    for name in modules:
        importlib.import_module(f"landscape_culler.{name}")
    write_json(result, {"schema_version": 1, "status": "passed", "profile_id": profile_id,
                        "worker_code_version": AI_WORKER_CODE_VERSION, "vlm_prompt_version": VLM_PROMPT_VERSION,
                        "checks": {"worker_imports": modules}, "completed_at": datetime.now(UTC).isoformat()})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-root", type=Path, required=True)
    parser.add_argument("--profile", choices=("8gb", "16gb"), required=True)
    parser.add_argument("--smoke-result", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--worker-only", action="store_true")
    args = parser.parse_args()
    if args.worker_only:
        check_worker_code(args.content_root, args.profile, args.smoke_result)
    else:
        install_models_and_smoke(
            args.content_root,
            args.profile,
            args.smoke_result,
            offline=args.offline,
        )
    print(json.dumps({"status": "passed", "profile": args.profile}))


if __name__ == "__main__":
    main()
