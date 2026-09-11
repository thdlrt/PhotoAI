from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .ai_runtime import active_engine, ai_resources_status
from .constants import (
    OLLAMA_ENDPOINT,
    PROPRIETARY_RAW_EXTENSIONS,
    VLM_MODEL_ID,
)
from .content_root import ContentRootLayout, resolve_content_root, runtime_environment
from .creative_lut import (
    ORDINARY_XMP_LUT_LIMITATION,
    CreativeLutEngine,
    CreativeLutError,
)
from .develop import (
    DEVELOP_SCHEMA_VERSION,
    STYLE_PRESETS,
    confirm_all,
    confirm_recommended_styles,
    develop_summary,
    load_develop_plan,
    merge_confirmed_develop,
    skip_all_crops,
    update_color_mode,
    update_develop_item,
    update_develop_options,
    update_style_global,
    update_style_group,
)
from .export_state import (
    abandon_export_attempt,
    activate_export_attempt,
    create_export_attempt,
    create_export_spec,
    export_spec_path,
    export_summary,
    finish_export_attempt,
    load_export_spec,
    record_export_result,
)
from .lightroom_bridge import (
    PLUGIN_VERSION,
    cancel_lightroom_batch,
    detect_lightroom_classic_15_3,
    get_lightroom_plugin_status,
    install_lightroom_plugin,
    installed_lightroom_plugin_dir,
    read_lightroom_batch_status,
    write_lightroom_look_descriptor,
    write_lightroom_plugin_config,
)
from .lut_export_worker import LUT_EXPORT_PROTOCOL
from .model_resources import (
    MODEL_SPECS,
    PROFILE_SPECS,
    active_model_profile_readiness,
    active_vlm_model,
    clip_model_reference,
    delete_model_profile,
    delete_model_resource,
    model_resources_status,
)
from .progress import parse_progress_line
from .settings_transfer import (
    SETTINGS_TRANSFER_FILENAME,
    SETTINGS_TRANSFER_MEDIA_TYPE,
    SettingsTransferError,
    build_settings_export,
    normalize_settings_payload,
    write_local_preferences,
)
from .style_library import (
    inspect_xmp_preset,
    load_style_index,
    managed_style_root,
    registration_state_path,
    runtime_supports_amount,
    style_index_path,
    style_resource_id,
    sync_style_library,
)
from .toolbox import (
    DEFAULT_JPEG_EXTENSIONS,
    DEFAULT_RAW_EXTENSIONS,
    create_raw_jpeg_plan,
    list_raw_jpeg_transactions,
    load_plan,
    load_raw_jpeg_transaction,
)
from .toolbox import (
    plan_path as raw_jpeg_plan_path,
)
from .toolbox import (
    transactions_root as raw_jpeg_transactions_root,
)
from .util import (
    cache_key,
    full_fingerprint,
    quick_fingerprint,
    read_json,
    runs_root,
    write_json,
)
from .version import PRODUCT_VERSION
from .xmp_cleanup import (
    create_xmp_cleanup_plan,
    list_xmp_cleanup_transactions,
    load_xmp_cleanup_plan,
    load_xmp_cleanup_transaction,
)
from .xmp_cleanup import plan_path as xmp_cleanup_plan_path
from .xmp_cleanup import transactions_root as xmp_cleanup_transactions_root

PACKAGE_ROOT = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    BUNDLE_ROOT = Path(sys._MEIPASS).resolve()
    PROJECT_ROOT = Path(sys.executable).resolve().parent
    STATIC_ROOT = BUNDLE_ROOT / "landscape_culler" / "static"
    TEMPLATE_ROOT = BUNDLE_ROOT / "landscape_culler" / "templates"
else:
    BUNDLE_ROOT = PACKAGE_ROOT
    PROJECT_ROOT = PACKAGE_ROOT.parents[1]
    STATIC_ROOT = PACKAGE_ROOT / "static"
    TEMPLATE_ROOT = PACKAGE_ROOT / "templates"
DEFAULT_PENDING = os.environ.get("PHOTO_AI_DEFAULT_PHOTOS", "")
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}(?:-\d{6})?$")
PROJECT_ID_RE = re.compile(r"^[0-9a-f]{20}$")
STYLE_PREVIEW_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MAX_STYLE_IMPORT_FILES = 500
MAX_STYLE_IMPORT_BYTES = 96 * 1024 * 1024


GROUP_PROGRESS_PLAN = [
    {"key": "previews", "label": "读取照片", "weight": 15},
    {"key": "features", "label": "生成相似特征", "weight": 65},
    {"key": "grouping", "label": "自动整理分组", "weight": 10},
    {"key": "finalize", "label": "保存分组工程", "weight": 10},
]
FAST_SCORE_PROGRESS_PLAN = [
    {"key": "previews", "label": "读取照片", "weight": 5},
    {"key": "features", "label": "读取相似特征", "weight": 5},
    {"key": "aesthetic", "label": "通用审美评分", "weight": 60},
    {"key": "local_rank", "label": "组内综合排名", "weight": 10},
    {"key": "global_rank", "label": "跨组统一排名", "weight": 10},
    {"key": "finalize", "label": "生成选片结果", "weight": 10},
]
DEEP_SCORE_PROGRESS_PLAN = [
    {"key": "previews", "label": "读取照片", "weight": 3},
    {"key": "features", "label": "读取相似特征", "weight": 3},
    {"key": "aesthetic", "label": "通用审美评分", "weight": 14},
    {"key": "local_vlm", "label": "构图组内评审", "weight": 43},
    {"key": "local_rank", "label": "组内综合排名", "weight": 4},
    {"key": "global_vlm", "label": "候选跨组评审", "weight": 23},
    {"key": "global_rank", "label": "跨组统一排名", "weight": 5},
    {"key": "finalize", "label": "生成选片结果", "weight": 5},
]
RAW_JPEG_EXECUTE_PROGRESS_PLAN = [
    {"key": "verify", "label": "复核目录", "weight": 15},
    {"key": "recycle", "label": "移入回收区", "weight": 80},
    {"key": "finalize", "label": "保存记录", "weight": 5},
]
RAW_JPEG_ROLLBACK_PROGRESS_PLAN = [
    {"key": "verify", "label": "复核回收文件", "weight": 20},
    {"key": "restore", "label": "恢复原位置", "weight": 75},
    {"key": "finalize", "label": "更新记录", "weight": 5},
]
XMP_CLEANUP_EXECUTE_PROGRESS_PLAN = [
    {"key": "verify", "label": "复核 XMP", "weight": 15},
    {"key": "delete", "label": "永久删除 XMP", "weight": 80},
    {"key": "finalize", "label": "保存记录", "weight": 5},
]
XMP_CLEANUP_ROLLBACK_PROGRESS_PLAN = [
    {"key": "verify", "label": "复核回收文件", "weight": 20},
    {"key": "restore", "label": "恢复 XMP", "weight": 75},
    {"key": "finalize", "label": "更新记录", "weight": 5},
]
LIGHTROOM_APPLY_PROGRESS_PLAN = [
    {"key": "launch", "label": "启动 Lightroom", "weight": 10},
    {"key": "queue", "label": "提交处理队列", "weight": 10},
    {"key": "process", "label": "Lightroom 自动处理", "weight": 75},
    {"key": "finalize", "label": "保存处理结果", "weight": 5},
]
STYLE_RECOMMEND_PROGRESS_PLAN = [
    {"key": "launch", "label": "等待 Lightroom 插件", "weight": 5},
    {"key": "presets", "label": "核对可用预设", "weight": 5},
    {"key": "representative", "label": "选择各组代表图", "weight": 10},
    {"key": "scene", "label": "读取场景特征", "weight": 10},
    {"key": "recall", "label": "召回候选预设", "weight": 10},
    {"key": "process", "label": "Lightroom 真实预览", "weight": 35},
    {"key": "collect", "label": "校验 Lightroom 预览", "weight": 5},
    {"key": "rerank", "label": "AI 质量复评", "weight": 15},
    {"key": "finalize", "label": "保存推荐结果", "weight": 5},
]
STYLE_PREVIEW_PROGRESS_PLAN = [
    {"key": "launch", "label": "等待 Lightroom 插件", "weight": 15},
    {"key": "process", "label": "Lightroom 重渲强度预览", "weight": 70},
    {"key": "finalize", "label": "保存预设与强度", "weight": 15},
]
STYLE_LUT_PREVIEW_PROGRESS_PLAN = [
    {"key": "lut", "label": "渲染创意 LUT 预览", "weight": 85},
    {"key": "finalize", "label": "保存 LUT 与强度", "weight": 15},
]
LUT_EXPORT_PROGRESS_PLAN = [
    {"key": "verify", "label": "核对基础 JPEG 与 LUT", "weight": 15},
    {"key": "render", "label": "渲染创意 LUT", "weight": 80},
    {"key": "finalize", "label": "保存导出结果", "weight": 5},
]
DEVELOP_JOB_PROGRESS_PLAN = [
    {"key": "develop", "label": "智能构图", "weight": 100},
]
DEFAULT_JOB_STAGES = {
    "group": "照片分类",
    "score": "AI 评分",
    "develop": "智能构图",
    "xmp_commit": "写入 XMP",
    "rollback": "回滚 XMP",
    "raw_jpeg_execute": "整理 RAW / 成片",
    "raw_jpeg_rollback": "撤销 RAW / 成片整理",
    "xmp_cleanup_execute": "永久删除文件夹 XMP",
    "xmp_cleanup_rollback": "恢复文件夹 XMP",
    "lightroom_apply": "Lightroom 自动处理",
    "style_recommend": "AI 风格推荐",
    "style_preview": "重渲风格强度",
    "lut_export": "渲染创意 LUT",
    "model_download": "下载 AI 模型",
}


def _worker_command(cli_args: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", *cli_args]
    return [sys.executable, "-m", "landscape_culler.cli", *cli_args]


def _job_progress_plan(kind: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    if kind == "model_download":
        if context.get("offline_import"):
            return [
                {"key": "launch", "label": "启动离线导入", "weight": 0.5},
                {"key": "preflight", "label": "检查离线包与硬件", "weight": 2.5},
                {"key": "offline_import", "label": "导入离线资源", "weight": 65.0},
                {
                    "key": "offline_dependencies",
                    "label": "安装离线运行环境",
                    "weight": 8.0,
                },
                {"key": "verify", "label": "校验整套模型", "weight": 6.0},
                {"key": "smoke", "label": "实际运行 AI 自检", "weight": 14.0},
                {"key": "activate", "label": "启用离线环境", "weight": 4.0},
            ]
        profile_id = str(context.get("profile_id") or "")
        profile = PROFILE_SPECS.get(profile_id)
        if not profile:
            return []
        model_ids = list(profile["model_ids"])
        total_bytes = max(
            1, sum(int(MODEL_SPECS[key]["estimated_bytes"]) for key in model_ids)
        )
        managed = bool(context.get("managed_engine"))
        model_weight = 67.5 if managed else 90.0
        steps = [
            {
                "key": key,
                "label": str(MODEL_SPECS[key]["label"]),
                "weight": model_weight
                * int(MODEL_SPECS[key]["estimated_bytes"])
                / total_bytes,
            }
            for key in model_ids
        ]
        if managed:
            return [
                {"key": "launch", "label": "启动安装程序", "weight": 0.5},
                {"key": "preflight", "label": "检查显卡与空间", "weight": 2.0},
                {"key": "python", "label": "安装受管 Python", "weight": 3.0},
                {"key": "venv", "label": "创建隔离环境", "weight": 2.0},
                {"key": "dependencies", "label": "安装固定依赖", "weight": 11.0},
                {"key": "worker", "label": "安装 AI Worker", "weight": 2.0},
                {"key": "ollama", "label": "安装本地模型组件", "weight": 3.0},
                *steps,
                {"key": "verify", "label": "校验整套模型", "weight": 3.0},
                {"key": "smoke", "label": "实际运行 AI 自检", "weight": 4.0},
                {"key": "activate", "label": "启用显存方案", "weight": 2.0},
            ]
        return [
            {"key": "runtime", "label": "准备本地运行组件", "weight": 3.0},
            *steps,
            {"key": "verify", "label": "校验整套模型", "weight": 5.0},
            {"key": "apply", "label": "应用显存方案", "weight": 2.0},
        ]
    if kind == "group":
        return GROUP_PROGRESS_PLAN
    if kind == "score":
        return (
            DEEP_SCORE_PROGRESS_PLAN
            if context.get("mode") == "deep"
            else FAST_SCORE_PROGRESS_PLAN
        )
    if kind == "develop":
        return DEVELOP_JOB_PROGRESS_PLAN
    if kind == "raw_jpeg_execute":
        return RAW_JPEG_EXECUTE_PROGRESS_PLAN
    if kind == "raw_jpeg_rollback":
        return RAW_JPEG_ROLLBACK_PROGRESS_PLAN
    if kind == "xmp_cleanup_execute":
        return XMP_CLEANUP_EXECUTE_PROGRESS_PLAN
    if kind == "xmp_cleanup_rollback":
        return XMP_CLEANUP_ROLLBACK_PROGRESS_PLAN
    if kind == "lightroom_apply":
        return LIGHTROOM_APPLY_PROGRESS_PLAN
    if kind == "style_recommend":
        return STYLE_RECOMMEND_PROGRESS_PLAN
    if kind == "style_preview":
        if context.get("requires_lightroom") is False or context.get("lut_id"):
            return STYLE_LUT_PREVIEW_PROGRESS_PLAN
        return STYLE_PREVIEW_PROGRESS_PLAN
    if kind == "lut_export":
        return LUT_EXPORT_PROGRESS_PLAN
    return []


def _initial_job_progress(plan: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not plan:
        return None
    first = plan[0]
    return {
        "overall_percent": 0.0,
        "stage_percent": 0.0,
        "current": 0,
        "total": 0,
        "unit": "",
        "cached": 0,
        "stage_key": first["key"],
        "stage_label": first["label"],
        "nodes": [
            {
                "key": step["key"],
                "label": step["label"],
                "status": "active" if index == 0 else "pending",
            }
            for index, step in enumerate(plan)
        ],
    }


def _apply_job_progress(
    job: dict[str, Any], event: dict[str, Any]
) -> tuple[bool, bool]:
    """Apply one pipeline event and return (phase_changed, is_boundary)."""

    plan = _job_progress_plan(str(job.get("kind", "")), dict(job.get("context") or {}))
    if not plan:
        return False, False
    phase = str(event.get("phase", ""))
    index = next(
        (position for position, step in enumerate(plan) if step["key"] == phase), None
    )
    if index is None:
        return False, False
    progress = job.get("progress")
    if not isinstance(progress, dict):
        progress = _initial_job_progress(plan)
        job["progress"] = progress
    assert progress is not None
    old_phase = progress.get("stage_key")
    old_index = next(
        (position for position, step in enumerate(plan) if step["key"] == old_phase),
        -1,
    )
    if index < old_index:
        return False, event.get("event") in {"phase_start", "phase_end"}
    total = max(0, int(event.get("total") or 0))
    current = max(0, int(event.get("current") or 0))
    if total:
        current = min(current, total)
    boundary = event.get("event") in {"phase_start", "phase_end"}
    stage_fraction = (
        1.0
        if event.get("event") == "phase_end"
        else (current / total if total else 0.0)
    )
    completed_weight = sum(float(step["weight"]) for step in plan[:index])
    calculated = completed_weight + float(plan[index]["weight"]) * stage_fraction
    overall = max(float(progress.get("overall_percent") or 0.0), min(99.9, calculated))
    nodes: list[dict[str, str]] = []
    phase_finished = event.get("event") == "phase_end"
    for position, step in enumerate(plan):
        if position < index or (position == index and phase_finished):
            status = "completed"
        elif position == index:
            status = "active"
        else:
            status = "pending"
        nodes.append({"key": step["key"], "label": step["label"], "status": status})
    label = str(event.get("label") or plan[index]["label"])
    unit = str(event.get("unit") or "")
    if (
        str(job.get("kind")) in {"style_recommend", "style_preview"}
        and phase == "process"
        and label == "Lightroom 自动处理"
    ):
        label = str(plan[index]["label"])
        unit = "项预览"
    progress.update(
        overall_percent=round(overall, 1),
        stage_percent=round(stage_fraction * 100.0, 1),
        current=current,
        total=total,
        unit=unit,
        cached=max(0, int(event.get("cached") or 0)),
        stage_key=phase,
        stage_label=label,
        nodes=nodes,
    )
    telemetry_fields = (
        "detail",
        "current_resource",
        "downloaded_bytes",
        "total_bytes",
        "bytes_per_second",
        "eta_seconds",
        "resumed_bytes",
        "elapsed_seconds",
        "heartbeat_at",
    )
    for key in telemetry_fields:
        if key in event:
            progress[key] = event[key]
        else:
            progress.pop(key, None)
    job["stage"] = label
    if event.get("detail"):
        job["message"] = str(event["detail"])[-500:]
    elif total:
        job["message"] = f"{current} / {total}{progress['unit']}"
    return old_phase != phase, boundary


def _complete_job_progress(job: dict[str, Any]) -> None:
    progress = job.get("progress")
    if not isinstance(progress, dict):
        return
    progress["overall_percent"] = 100.0
    progress["stage_percent"] = 100.0
    if progress.get("total"):
        progress["current"] = progress["total"]
    for node in progress.get("nodes", []):
        node["status"] = "completed"


def _fail_job_progress(job: dict[str, Any]) -> None:
    progress = job.get("progress")
    if not isinstance(progress, dict):
        return
    for node in progress.get("nodes", []):
        if node.get("status") == "active":
            node["status"] = "failed"


def _job_failure_message(job: dict[str, Any], exit_code: int) -> str:
    unsigned_code = exit_code & 0xFFFFFFFF
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    stage = str(progress.get("stage_label") or job.get("stage") or "AI 任务")
    if unsigned_code == 0xC0000005 and job.get("kind") in {"group", "score"}:
        return (
            f"{stage}启动失败：AI 进程发生 Windows 访问冲突（0xC0000005）。"
            "当前分组和缓存已保留；请关闭占用显卡的程序或重启 Windows，"
            "若仍失败请更新或重新安装 NVIDIA 驱动后再评分。"
        )
    if job.get("worker_error"):
        return str(job["worker_error"])[-1500:]
    tail = job.get("log_tail")
    if isinstance(tail, list) and tail:
        return str(tail[-1])[-500:]
    return f"工作进程异常退出（代码 {exit_code}）。"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _worker_protocol_event(line: str) -> dict[str, Any] | None:
    if not line.startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("protocol") != "PHOTO_AI_WORKER/1":
        return None
    return payload


def _reserve_loopback_endpoint() -> str:
    """Select a per-job Ollama endpoint instead of claiming a fixed port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def runtime_data_dir() -> Path:
    configured = os.environ.get("PHOTO_AI_DATA_DIR")
    return (
        Path(configured).resolve()
        if configured
        else (PROJECT_ROOT / ".runtime" / "data").resolve()
    )


def runtime_cache_dir(data_dir: Path) -> Path:
    return Path(os.environ.get("PHOTO_AI_CACHE_DIR") or data_dir / "cache")


def _json(path: Path, default: Any) -> Any:
    try:
        return read_json(path) if path.is_file() else default
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _existing_dir(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_absolute() or not path.is_dir():
        raise HTTPException(422, f"{label}不存在或不是文件夹：{path}")
    return path


def _scoped_dir(value: str, label: str, allowed_root: Path | None) -> Path:
    path = _existing_dir(value, label)
    if allowed_root is not None and not _inside(path, allowed_root):
        raise HTTPException(422, f"{label}必须位于：{allowed_root}")
    return path


def _scoped_toolbox_dir(value: str, label: str, allowed_root: Path | None) -> Path:
    original = Path(value).expanduser()
    try:
        lexical = Path(os.path.abspath(original))
    except OSError as exc:
        raise HTTPException(422, f"{label}路径无效：{value}") from exc
    is_junction = getattr(os.path, "isjunction", lambda _value: False)
    for component in (lexical, *lexical.parents):
        if component.parent == component:
            continue
        try:
            if component.is_symlink() or is_junction(component):
                raise HTTPException(
                    422, f"{label}不能经过链接或目录联接点：{component}"
                )
        except OSError as exc:
            raise HTTPException(422, f"{label}无法安全检查：{component}") from exc
    return _scoped_dir(value, label, allowed_root)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _run_file(data_dir: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(404, "选片批次不存在。")
    path = runs_root(data_dir) / run_id / "results.json"
    if not path.is_file():
        raise HTTPException(404, "选片批次不存在。")
    return path


def _lightroom_state() -> str:
    if os.name != "nt":
        return "closed"
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Lightroom.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return "running" if '"lightroom.exe"' in result.stdout.casefold() else "closed"


def _gpu() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    if result.returncode != 0 or not result.stdout.strip():
        return {"available": False}
    fields = [value.strip() for value in result.stdout.splitlines()[0].split(",")]
    return {
        "available": True,
        "name": fields[0],
        "memory_total_mib": int(fields[1]),
        "memory_free_mib": int(fields[2]),
        "utilization_percent": int(fields[3]),
    }


def _unload_local_vlm() -> None:
    endpoint = os.environ.get("PHOTO_AI_OLLAMA_ENDPOINT", "").rstrip("/")
    if not endpoint:
        if os.environ.get("PHOTO_AI_CONTENT_ROOT"):
            # The managed worker owns its random Ollama endpoint and process.
            # The core service must not probe the legacy fixed port while no
            # managed endpoint is explicitly attached to this process.
            return
        endpoint = OLLAMA_ENDPOINT
    names = {
        VLM_MODEL_ID,
        str(MODEL_SPECS["qwen3-vl-4b"]["ollama_name"]),
        str(MODEL_SPECS["qwen3-vl-8b"]["ollama_name"]),
        *[str(value) for value in MODEL_SPECS["qwen3-vl-8b"].get("aliases", [])],
    }
    for name in names:
        payload = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
        request = urllib.request.Request(
            f"{endpoint}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5):
                pass
        except (OSError, urllib.error.URLError):
            pass


class JobManager:
    """Run isolated jobs while keeping mutable state in the selected data root."""

    AI_KINDS = frozenset({
        "group", "score", "develop", "style_recommend", "style_preview", "lut_export",
    })

    RETRYABLE_KINDS = frozenset({
        "group",
        "score",
        "develop",
        "style_recommend",
        "style_preview",
        "lut_export",
        "model_download",
        "lightroom_apply",
        "xmp_commit",
        "rollback",
        "raw_jpeg_execute",
        "raw_jpeg_rollback",
        "xmp_cleanup_execute",
        "xmp_cleanup_rollback",
    })

    def __init__(
        self,
        data_dir: Path,
        project_root: Path,
        content_layout: ContentRootLayout | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.project_root = project_root
        self.content_layout = content_layout
        self.jobs_dir = data_dir / "web" / "jobs"
        self.logs_dir = (
            content_layout.logs / "jobs"
            if content_layout is not None
            else Path(os.environ.get("PHOTO_AI_LOGS_DIR") or data_dir / "web" / "logs")
        )
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.processes: dict[str, subprocess.Popen[str]] = {}
        for path in self.jobs_dir.glob("*.json"):
            job = _json(path, None)
            if not isinstance(job, dict) or not job.get("id"):
                continue
            if job.get("status") in {"queued", "running", "cancelling"}:
                message = "服务重启，任务已中断；已有缓存仍会复用。"
                if job.get("kind") in {
                    "lightroom_apply",
                    "style_recommend",
                    "style_preview",
                } and job.get("context", {}).get("batch_id"):
                    try:
                        result = self._cancel_unfinished_lightroom(
                            job,
                            "Web service restarted while the Lightroom job was active",
                        )
                        job["result"] = result
                        message = "服务重启，Lightroom 批次已请求取消。"
                    except (OSError, ValueError, TypeError, RuntimeError) as exc:
                        message = (
                            f"服务重启，任务已中断；Lightroom 取消标记写入失败：{exc}"
                        )
                job.update(status="interrupted", finished_at=_now(), message=message)
                write_json(path, job)
            self._restore_retry_spec(job)
            self.jobs[job["id"]] = job

    def _restore_retry_spec(self, job: dict[str, Any]) -> None:
        """Recover retry metadata for jobs created before retry support existed."""

        if job.get("kind") not in self.RETRYABLE_KINDS or isinstance(
            job.get("retry_spec"), dict
        ):
            return
        spec = _json(self.jobs_dir / f"{job.get('id', '')}.spec.json", None)
        argv = spec.get("argv") if isinstance(spec, dict) else None
        valid_spec = bool(
            isinstance(spec, dict)
            and spec.get("protocol") == "PHOTO_AI_WORKER/1"
            and spec.get("command") == "cli"
            and isinstance(argv, list)
            and all(isinstance(value, str) for value in argv)
        )
        if not valid_spec:
            command = job.get("command")
            argv = None
            if isinstance(command, list) and all(
                isinstance(value, str) for value in command
            ):
                if "--worker" in command:
                    argv = command[command.index("--worker") + 1 :]
                elif "-m" in command:
                    module_index = command.index("-m")
                    if (
                        module_index + 1 < len(command)
                        and command[module_index + 1] == "landscape_culler.cli"
                    ):
                        argv = command[module_index + 2 :]
            if not argv:
                return
        job["retry_spec"] = {
            "kind": str(job["kind"]),
            "argv": list(argv),
            "context": {
                "title": str(job.get("title") or job["kind"]),
                **dict(job.get("context") or {}),
            },
        }

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if self.content_layout is not None and os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
                    return
            except (OSError, subprocess.SubprocessError):
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    def _job_command(self, kind: str, cli_args: list[str], job_id: str) -> list[str]:
        managed_ai = kind in self.AI_KINDS
        if self.content_layout is None:
            return _worker_command(cli_args)
        if managed_ai:
            engine = active_engine(self.content_layout)
            if engine is None:
                raise RuntimeError("AI 计算环境尚未完整安装或自检未通过。")
            python = (
                engine
                / "venv"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            if not python.is_file():
                raise RuntimeError("AI 计算环境缺少受管 Python。")
            # The managed AI wheel carries the same versioned worker entrypoint
            # as CoreWorker.  Keep every installed job on PHOTO_AI_WORKER/1;
            # never bypass the job-spec/result boundary with a direct CLI call.
            worker_command = [str(python), "-m", "landscape_culler.core_worker"]
        else:
            worker = os.environ.get("PHOTO_AI_CORE_WORKER") or os.environ.get(
                "PHOTO_AI_CORE_WORKER_EXE"
            )
            if worker:
                worker_command = [worker]
            elif getattr(sys, "frozen", False):
                executable = (
                    Path(sys.executable).resolve().parent / "PhotoAI.CoreWorker.exe"
                )
                if not executable.is_file():
                    raise RuntimeError("安装目录缺少 PhotoAI.CoreWorker.exe。")
                worker_command = [str(executable)]
            else:
                worker_command = [sys.executable, "-m", "landscape_culler.core_worker"]
        spec_path = self.jobs_dir / f"{job_id}.spec.json"
        result_path = self.jobs_dir / f"{job_id}.worker-result.json"
        write_json(
            spec_path,
            {
                "protocol": "PHOTO_AI_WORKER/1",
                "job_id": job_id,
                "command": "cli",
                "argv": cli_args,
                "result_path": str(result_path),
            },
        )
        return [
            *worker_command,
            "--job-spec",
            str(spec_path),
            "--result",
            str(result_path),
        ]

    def _save(self, job: dict[str, Any]) -> None:
        write_json(self.jobs_dir / f"{job['id']}.json", job)

    def public(self, job: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: value
            for key, value in job.items()
            if key not in {"command", "retry_spec"}
        }
        retry_mode = (
            "export_failed"
            if job.get("context", {}).get("export_spec_id")
            else "develop"
            if job.get("kind") == "develop"
            else "job"
        )
        result["retryable"] = bool(
            job.get("kind") in self.RETRYABLE_KINDS
            and (
                isinstance(job.get("retry_spec"), dict)
                or retry_mode in {"export_failed", "develop"}
            )
        )
        result["retry_mode"] = retry_mode if result["retryable"] else None
        return result

    def active(self) -> dict[str, Any] | None:
        with self.lock:
            found = next(
                (
                    item
                    for item in self.jobs.values()
                    if item.get("status") in {"queued", "running", "cancelling"}
                ),
                None,
            )
            return self.public(found) if found else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            ordered = sorted(
                self.jobs.values(),
                key=lambda item: item.get("created_at", ""),
                reverse=True,
            )
            return [self.public(item) for item in ordered[:limit]]

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return self.public(self.jobs[job_id])

    def wait(self, job_id: str, poll_interval: float = 0.05) -> dict[str, Any]:
        """Wait for a queued worker without performing its work in the caller."""

        while True:
            job = self.get(job_id)
            if job.get("status") not in {"queued", "running", "cancelling"}:
                return job
            time.sleep(max(0.01, min(0.5, float(poll_interval))))

    def start(
        self, kind: str, cli_args: list[str], context: dict[str, Any]
    ) -> dict[str, Any]:
        with self.lock:
            active = self.active()
            if active:
                raise RuntimeError(f"已有任务正在运行：{active['title']}")
            job_id = (
                f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            )
            context = dict(context)
            title = context.pop("title", kind)
            retry_spec = {
                "kind": kind,
                "argv": list(cli_args),
                "context": {"title": title, **context},
            }
            plan = _job_progress_plan(kind, context)
            progress = _initial_job_progress(plan)
            message = "已加入本机任务队列。"
            if (
                kind in {"lightroom_apply", "style_recommend", "style_preview"}
                and progress
                and progress.get("stage_key") == "launch"
            ):
                heartbeat_state = str(context.get("lightroom_state") or "unknown")
                message = {
                    "offline": "Lightroom 插件尚未连接，正在等待插件上线。",
                    "stale": "Lightroom 插件心跳已断开，正在等待重新连接。",
                    "invalid": "Lightroom 插件心跳无效，正在等待重新连接。",
                    "online": "正在确认 Lightroom 插件连接。",
                }.get(heartbeat_state, "正在等待 Lightroom 插件连接。")
            job = {
                "id": job_id,
                "kind": kind,
                "title": title,
                "status": "queued",
                "stage": progress["stage_label"]
                if progress
                else DEFAULT_JOB_STAGES.get(kind, "等待启动"),
                "message": message,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "context": context,
                "progress": progress,
                "log_tail": [],
                "result": None,
                "retry_spec": retry_spec,
                "command": self._job_command(kind, cli_args, job_id),
            }
            self.jobs[job_id] = job
            self._save(job)
            threading.Thread(
                target=self._run, args=(job_id,), daemon=True, name=f"photo-ai-{kind}"
            ).start()
            return self.public(job)

    @staticmethod
    def _replace_cli_option(args: list[str], option: str, value: str) -> None:
        try:
            index = args.index(option)
        except ValueError:
            return
        if index + 1 < len(args):
            args[index + 1] = value

    def retry(self, job_id: str) -> dict[str, Any]:
        """Start a fresh attempt from a failed job's private immutable recipe."""

        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            previous = self.jobs[job_id]
            previous_result = previous.get("result")
            partial_style = bool(
                previous.get("kind") in {"style_recommend", "style_preview"}
                and previous.get("status") == "completed"
                and isinstance(previous_result, dict)
                and previous_result.get("style_status") == "partial"
            )
            if (
                previous.get("status") not in {"failed", "interrupted"}
                and not partial_style
            ):
                raise RuntimeError("只有失败或意外中断的任务可以重试。")
            if previous.get("context", {}).get("export_spec_id"):
                raise RuntimeError("导出任务必须通过失败项清单重试。")
            retry_spec = previous.get("retry_spec")
            if not isinstance(retry_spec, dict):
                raise TypeError("这个旧任务没有可用的重试信息，请从当前步骤重新开始。")
            kind = str(retry_spec.get("kind") or "")
            if kind not in self.RETRYABLE_KINDS or kind == "develop":
                raise RuntimeError("这个任务需要从当前步骤重新生成后再试。")
            args = list(retry_spec.get("argv") or [])
            if not args or not all(isinstance(value, str) for value in args):
                raise RuntimeError("任务重试信息不完整，请从当前步骤重新开始。")
            context = dict(retry_spec.get("context") or {})
            context["retry_of_job_id"] = job_id
            if kind in {"lightroom_apply", "style_recommend", "style_preview"}:
                batch_id = f"retry-{kind.replace('_', '-')}-{uuid.uuid4().hex[:20]}"
                self._replace_cli_option(args, "--batch-id", batch_id)
                context["batch_id"] = batch_id
                context["lightroom_state"] = "unknown"

        return self.start(kind, args, context)

    def _stage(self, kind: str, line: str) -> str | None:
        if "候选跨组评审" in line:
            return "候选跨组评审"
        if "构图组内评审" in line:
            return "构图组内评审"
        if "通用审美评分" in line:
            return "通用审美评分"
        if "DINOv2" in line:
            return "生成视觉特征"
        if "读取预览" in line:
            return "读取照片预览"
        return None

    def _find_result(self, job: dict[str, Any]) -> Any:
        context = job["context"]
        if job["kind"] in {"group", "score"}:
            runs = [
                path
                for path in runs_root(self.data_dir).glob("*")
                if path.is_dir() and RUN_ID_RE.fullmatch(path.name)
            ]
            if runs:
                latest = max(
                    runs,
                    key=lambda path: (
                        (path / "results.json").stat().st_mtime_ns
                        if (path / "results.json").is_file()
                        else 0
                    ),
                )
                return {"run_id": latest.name}
        if job["kind"] == "xmp_commit":
            root = Path(context["results_path"]).parent
            manifests = [
                *root.glob("xmp-commit-manifest-*.json"),
                *root.glob("xmp-commit-manifest.json"),
            ]
            if manifests:
                latest = max(manifests, key=lambda path: path.stat().st_mtime_ns)
                return {"manifest_path": str(latest), **_json(latest, {})}
        if job["kind"] == "rollback":
            manifest = Path(context["manifest_path"])
            return _json(
                manifest.parent / f"xmp-rollback-result-{manifest.stem}.json", None
            )
        if job["kind"] in {"raw_jpeg_execute", "raw_jpeg_rollback"}:
            transaction_id = context.get("plan_id") or context.get("transaction_id")
            if not transaction_id:
                return None
            try:
                return load_raw_jpeg_transaction(
                    self.data_dir, transaction_id, reconcile=True
                )[0]
            except (OSError, ValueError, TypeError):
                return None
        if job["kind"] in {"xmp_cleanup_execute", "xmp_cleanup_rollback"}:
            transaction_id = context.get("plan_id") or context.get("transaction_id")
            if not transaction_id:
                return None
            try:
                return load_xmp_cleanup_transaction(
                    self.data_dir, str(transaction_id), reconcile=True
                )[0]
            except (OSError, ValueError, TypeError):
                return None
        if job["kind"] == "develop":
            run_id = str(context.get("run_id") or "")
            plan = _json(runs_root(self.data_dir) / run_id / "develop.json", None)
            if isinstance(plan, dict):
                return {
                    "run_id": run_id,
                    "plan_id": plan.get("plan_id"),
                    "develop_revision": plan.get("revision"),
                    "review_revision": plan.get("source_review_revision"),
                }
        if job["kind"] == "lightroom_apply":
            batch_id = context.get("batch_id")
            if not batch_id:
                return None
            try:
                return read_lightroom_batch_status(self.data_dir, str(batch_id))
            except (OSError, ValueError, TypeError, KeyError):
                return None
        if job["kind"] in {"style_recommend", "style_preview"}:
            run_id = str(context.get("run_id") or "")
            run_dir = runs_root(self.data_dir) / run_id
            develop = _json(run_dir / "develop.json", None)
            if isinstance(develop, dict):
                recommendation = _json(run_dir / "style-recommendations.json", {})
                worker = (
                    recommendation.get("worker")
                    if isinstance(recommendation, dict)
                    and isinstance(recommendation.get("worker"), dict)
                    else {}
                )
                failed_group_ids = [
                    str(value) for value in worker.get("failed_group_ids", [])
                ]
                successful_group_ids = [
                    str(value) for value in worker.get("successful_group_ids", [])
                ]
                return {
                    "run_id": run_id,
                    "develop_revision": develop.get("revision"),
                    "batch_id": context.get("batch_id"),
                    "style_status": recommendation.get("status")
                    if isinstance(recommendation, dict)
                    else None,
                    "succeeded_group_count": len(successful_group_ids),
                    "failed_group_count": len(failed_group_ids),
                }
        if job["kind"] == "lut_export":
            payload = _json(
                self.jobs_dir / f"{job['id']}.worker-result.json",
                None,
            )
            if (
                isinstance(payload, dict)
                and payload.get("protocol") == "PHOTO_AI_WORKER/1"
                and payload.get("status") == "completed"
                and isinstance(payload.get("result"), dict)
            ):
                return payload["result"]
        if job["kind"] == "model_download":
            if self.content_layout is not None:
                return ai_resources_status(self.content_layout)
            return model_resources_status(self.project_root / ".runtime", self.data_dir)
        return None

    def _request_lightroom_cancel(
        self, job: dict[str, Any], reason: str
    ) -> dict[str, Any] | None:
        if job.get("kind") not in {
            "lightroom_apply",
            "style_recommend",
            "style_preview",
        }:
            return None
        batch_id = job.get("context", {}).get("batch_id")
        if not batch_id:
            return None
        return cancel_lightroom_batch(self.data_dir, str(batch_id), reason=reason)

    def _cancel_unfinished_lightroom(
        self, job: dict[str, Any], reason: str
    ) -> dict[str, Any] | None:
        """Stop orphan queue work, but preserve a genuine terminal batch result."""

        if job.get("kind") not in {
            "lightroom_apply",
            "style_recommend",
            "style_preview",
        }:
            return None
        current = self._find_result(job)
        if isinstance(current, dict) and current.get("status") in {
            "complete",
            "failed",
            "cancelled",
            "incomplete",
        }:
            return current
        return self._request_lightroom_cancel(job, reason)

    def _job_environment(self, job: dict[str, Any]) -> dict[str, str]:
        """Build a worker environment without requiring uninstalled AI models."""

        env = os.environ.copy()
        env.update(
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
            PYTHONUNBUFFERED="1",
            PHOTO_AI_PROGRESS="json",
        )
        runtime_root = self.project_root / ".runtime"
        if self.content_layout is None:
            # Source-checkout Web runs retain the historical `.runtime` layout.
            # Name it explicitly so model inference can use the same resources
            # that the development resource page just verified. Installed builds
            # never receive this escape hatch and keep the Content Root boundary.
            env.pop("PHOTO_AI_CONTENT_ROOT", None)
            env.pop("PHOTO_AI_MODELS_DIR", None)
            env["PHOTO_AI_LEGACY_RUNTIME_ROOT"] = str(runtime_root.resolve())
        runtime_env: dict[str, Path | str] = {
            "UV_CACHE_DIR": runtime_root / "uv-cache",
            "UV_PYTHON_INSTALL_DIR": runtime_root / "uv-python",
            "PIP_CACHE_DIR": runtime_root / "pip-cache",
            "XDG_CACHE_HOME": runtime_root / "xdg-cache",
            "MPLCONFIGDIR": runtime_root / "matplotlib",
            "HF_HOME": runtime_root / "huggingface",
            "HF_HUB_CACHE": runtime_root / "huggingface" / "hub",
            "TRANSFORMERS_CACHE": runtime_root / "huggingface" / "transformers",
            "TORCH_HOME": runtime_root / "torch",
            "OLLAMA_MODELS": runtime_root / "ollama-models",
            "PHOTO_AI_DATA_DIR": self.data_dir,
            "TEMP": runtime_root / "temp",
            "TMP": runtime_root / "temp",
        }
        if self.content_layout is not None:
            env.pop("PHOTO_AI_LEGACY_RUNTIME_ROOT", None)
            runtime_root = self.content_layout.models
            env.update(runtime_environment(self.content_layout))
            runtime_env = {
                "MPLCONFIGDIR": self.content_layout.cache / "matplotlib",
                "TRANSFORMERS_CACHE": self.content_layout.cache
                / "huggingface"
                / "transformers",
            }
        for key, path in runtime_env.items():
            resolved_path = Path(path)
            resolved_path.mkdir(parents=True, exist_ok=True)
            env[key] = str(resolved_path)
        # Only inference jobs need a CLIP snapshot. File tools, XMP operations,
        # Lightroom and the installer must also work without any AI models.
        if job.get("kind") in self.AI_KINDS:
            env["PHOTO_AI_CLIP_MODEL"] = clip_model_reference(runtime_root)
        else:
            env.pop("PHOTO_AI_CLIP_MODEL", None)
        env["PHOTO_AI_VLM_MODEL"] = active_vlm_model(
            self.data_dir, fallback=VLM_MODEL_ID
        )
        tool_root = (
            self.content_layout.tools
            if self.content_layout is not None
            else runtime_root / "tools"
        )
        portable_exiftool = (
            tool_root / "exiftool-13.59" / "exiftool-13.59_64" / "exiftool.exe"
        )
        if portable_exiftool.is_file():
            env["PHOTO_AI_EXIFTOOL"] = str(portable_exiftool)
        portable_ollama = tool_root / "ollama-v0.33.2" / "ollama.exe"
        if portable_ollama.is_file():
            env["PHOTO_AI_OLLAMA"] = str(portable_ollama)
        if self.content_layout is not None:
            ollama_endpoint = _reserve_loopback_endpoint()
            env["PHOTO_AI_OLLAMA_ENDPOINT"] = ollama_endpoint
            env["OLLAMA_HOST"] = ollama_endpoint.removeprefix("http://")
        else:
            env["OLLAMA_HOST"] = "127.0.0.1:11435"
        env["OLLAMA_NO_CLOUD"] = "true"
        return {key: str(value) for key, value in env.items()}

    def _run(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] != "queued":
                return
            job.update(status="running", started_at=_now())
            self._save(job)
        try:
            env = self._job_environment(job)
        except Exception as exc:  # noqa: BLE001 - durable worker launch boundary
            with self.lock:
                _fail_job_progress(job)
                message = f"无法启动本机工作进程：{exc}"
                job.update(
                    status="failed",
                    stage="失败",
                    message=message[-500:],
                    finished_at=_now(),
                    log_tail=[*job.get("log_tail", []), message[-1000:]][-25:],
                )
                self._save(job)
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        log_path = self.logs_dir / f"{job_id}.log"
        try:
            with self.lock:
                if job["status"] != "running":
                    return
                process = subprocess.Popen(
                    job["command"],
                    cwd=self.project_root,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=flags,
                )
                self.processes[job_id] = process
                job["pid"] = process.pid
                self._save(job)
            assert process.stdout is not None
            last_save = 0.0
            with log_path.open("a", encoding="utf-8") as log:
                for raw_line in process.stdout:
                    line = ANSI_RE.sub("", raw_line.replace("\r", "")).strip()
                    if not line:
                        continue
                    worker_event = _worker_protocol_event(line)
                    if worker_event is not None:
                        event = str(worker_event.get("event") or "worker")
                        if event == "failed":
                            message = str(worker_event.get("error") or "Worker 执行失败。")
                            with self.lock:
                                job["worker_error"] = message[-1500:]
                                job["message"] = message[-1500:]
                                job["log_tail"] = [
                                    *job["log_tail"],
                                    f"Worker failed: {message}"[-1000:],
                                ][-25:]
                        log.write(f"Worker protocol: {event}\n")
                        if event == "failed":
                            log.write(message + "\n")
                        log.flush()
                        continue
                    progress_event = parse_progress_line(line)
                    with self.lock:
                        if progress_event is not None:
                            phase_changed, boundary = _apply_job_progress(
                                job, progress_event
                            )
                            current = progress_event["current"]
                            total = progress_event["total"]
                            unit = progress_event["unit"]
                            friendly = f"进度 | {progress_event['label']} | {current}/{total}{unit}"
                            if progress_event.get("cached"):
                                friendly += f" | 缓存 {progress_event['cached']}"
                            log.write(friendly + "\n")
                            log.flush()
                            if (
                                phase_changed
                                or boundary
                                or time.monotonic() - last_save > 0.5
                            ):
                                self._save(job)
                                last_save = time.monotonic()
                            continue
                        log.write(line + "\n")
                        log.flush()
                        legacy_stage = self._stage(job["kind"], line)
                        if legacy_stage and not job.get("progress"):
                            job["stage"] = legacy_stage
                        job["message"] = line[-500:]
                        job["log_tail"] = [*job["log_tail"], line[-1000:]][-25:]
                        if time.monotonic() - last_save > 0.8:
                            self._save(job)
                            last_save = time.monotonic()
            code = process.wait()
            with self.lock:
                job.update(exit_code=code, finished_at=_now())
                if (
                    code != 0
                    and job["kind"]
                    in {"lightroom_apply", "style_recommend", "style_preview"}
                    and job["status"] != "cancelling"
                ):
                    try:
                        job["result"] = self._cancel_unfinished_lightroom(
                            job,
                            f"Lightroom worker exited with code {code}",
                        )
                    except (OSError, ValueError, TypeError, RuntimeError) as exc:
                        job["message"] = (
                            f"Lightroom 工作进程异常退出；取消标记写入失败：{exc}"
                        )
                if job["status"] == "cancelled":
                    pass
                elif code == 0 and not (
                    job["status"] == "cancelling"
                    and job["kind"]
                    in {"lightroom_apply", "style_recommend", "style_preview"}
                ):
                    _complete_job_progress(job)
                    result = self._find_result(job)
                    if (
                        isinstance(result, dict)
                        and result.get("style_status") == "partial"
                    ):
                        message = (
                            f"已完成 {int(result.get('succeeded_group_count') or 0)} 个组；"
                            f"{int(result.get('failed_group_count') or 0)} 个组可重跑。"
                        )
                    else:
                        message = (
                            "任务已完成。"
                            if job["status"] != "cancelling"
                            else "任务在取消请求到达前已经完成。"
                        )
                    job.update(
                        status="completed",
                        stage="已完成",
                        message=message,
                        result=result,
                    )
                elif job["status"] == "cancelling":
                    result = (
                        self._find_result(job) or job.get("result")
                        if job["kind"]
                        in {
                            "raw_jpeg_execute",
                            "raw_jpeg_rollback",
                            "xmp_cleanup_execute",
                            "xmp_cleanup_rollback",
                            "lightroom_apply",
                            "style_recommend",
                            "style_preview",
                        }
                        else None
                    )
                    job.update(
                        status="cancelled",
                        stage="已取消",
                        message="任务已停止；已经移动的文件可在工具箱中撤销。",
                        result=result,
                    )
                else:
                    _fail_job_progress(job)
                    job.update(
                        status="failed",
                        stage="失败",
                        message=_job_failure_message(job, code),
                    )
                self._save(job)
        except Exception as exc:
            with self.lock:
                cancellation_error: Exception | None = None
                if job.get("kind") in {
                    "lightroom_apply",
                    "style_recommend",
                    "style_preview",
                }:
                    try:
                        job["result"] = self._cancel_unfinished_lightroom(
                            job,
                            f"Lightroom worker raised {type(exc).__name__}",
                        )
                    except (OSError, ValueError, TypeError, RuntimeError) as marker_exc:
                        cancellation_error = marker_exc
                if job.get("status") == "cancelling" and job.get("kind") in {
                    "lightroom_apply",
                    "style_recommend",
                    "style_preview",
                }:
                    job.update(
                        status="cancelled",
                        stage="已取消",
                        finished_at=_now(),
                        message="Lightroom 批次已请求取消。",
                        result=self._find_result(job) or job.get("result"),
                    )
                else:
                    message = f"{type(exc).__name__}: {exc}"
                    if cancellation_error is not None:
                        message += f"；Lightroom 取消标记写入失败：{cancellation_error}"
                    job.update(
                        status="failed",
                        stage="失败",
                        finished_at=_now(),
                        message=message,
                    )
                self._save(job)
        finally:
            with self.lock:
                self.processes.pop(job_id, None)

    def cancel(self, job_id: str) -> dict[str, Any]:
        process: subprocess.Popen[str] | None = None
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            job = self.jobs[job_id]
            if job["status"] not in {"queued", "running"}:
                return self.public(job)
            result = self._request_lightroom_cancel(
                job, "cancelled from the Web interface"
            )
            if result is not None:
                job["result"] = result
            process = self.processes.get(job_id)
            is_lightroom = job.get("kind") in {
                "lightroom_apply",
                "style_recommend",
                "style_preview",
            }
            if is_lightroom:
                job.update(status="cancelling", stage="正在取消")
                self._save(job)
                return self.public(job)
            if job["status"] == "queued" or process is None:
                job.update(
                    status="cancelled",
                    stage="已取消",
                    finished_at=_now(),
                    message="任务已停止；已经下载的完整文件和断点缓存会保留。",
                )
                self._save(job)
                return self.public(job)
            job.update(status="cancelling", stage="正在取消")
            self._save(job)

        if process is not None and process.poll() is None:
            self._terminate_process(process)
            if self.content_layout is None:
                _unload_local_vlm()
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] == "cancelling":
                job.update(
                    status="cancelled",
                    stage="已取消",
                    finished_at=_now(),
                    message="任务已停止；已经下载的完整文件和断点缓存会保留。",
                )
                self._save(job)
            return self.public(job)

    def shutdown(self) -> None:
        with self.lock:
            processes = list(self.processes.items())
            active_jobs = [
                job
                for job in self.jobs.values()
                if job.get("status") in {"queued", "running", "cancelling"}
            ]
            for job in active_jobs:
                try:
                    result = self._cancel_unfinished_lightroom(
                        job, "Web service is shutting down"
                    )
                    if result is not None:
                        job["result"] = result
                except (OSError, ValueError, TypeError, RuntimeError) as exc:
                    job["message"] = f"服务正在关闭；Lightroom 取消标记写入失败：{exc}"
                job.update(status="cancelling", stage="服务正在关闭")
                self._save(job)
            for _job_id, process in processes:
                if process.poll() is None:
                    self._terminate_process(process)
                    if self.content_layout is None:
                        _unload_local_vlm()
        for _job_id, process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


class RunScoreBody(BaseModel):
    base_revision: int = Field(ge=0)
    retain_ratio: float | None = Field(None, ge=0.05, le=1.0)
    mode: Literal["fast", "deep"] | None = None


class GroupBody(BaseModel):
    input_path: str
    retain_ratio: float = Field(0.30, ge=0.05, le=1.0)
    mode: Literal["fast", "deep"] = "deep"


class ProjectCreateBody(BaseModel):
    input_path: str = Field(min_length=1, max_length=2000)


class ProjectGroupBody(BaseModel):
    retain_ratio: float = Field(0.30, ge=0.05, le=1.0)
    mode: Literal["fast", "deep"] = "deep"


class GroupEditBody(BaseModel):
    group_id: int | None = Field(None, ge=1, le=100000)
    base_revision: int = Field(0, ge=0)


class BulkGroupEditBody(BaseModel):
    indexes: list[int] = Field(min_length=1, max_length=5000)
    group_id: int | None = Field(None, ge=1, le=100000)
    new_group: bool = False
    direction: Literal["previous", "next"] | None = None
    base_revision: int = Field(0, ge=0)


class ExcludeItemsBody(BaseModel):
    indexes: list[int] = Field(min_length=1, max_length=5000)
    excluded: bool = True
    base_revision: int = Field(0, ge=0)


class RawJpegPreviewBody(BaseModel):
    layout: Literal["mixed", "separate"] = "mixed"
    direction: Literal["jpeg", "raw", "both"] = "jpeg"
    mixed_path: str | None = Field(None, max_length=2000)
    raw_path: str | None = Field(None, max_length=2000)
    jpeg_path: str | None = Field(None, max_length=2000)
    raw_extensions: list[str] = Field(
        default_factory=lambda: [value.lstrip(".") for value in DEFAULT_RAW_EXTENSIONS],
        min_length=1,
        max_length=30,
    )
    jpeg_extensions: list[str] = Field(
        default_factory=lambda: [
            value.lstrip(".") for value in DEFAULT_JPEG_EXTENSIONS
        ],
        min_length=1,
        max_length=30,
    )
    recursive: bool = False


class RawJpegExecuteBody(BaseModel):
    plan_id: str = Field(pattern=r"^\d{8}-\d{6}-[0-9a-f]{8}$")


class RawJpegRollbackBody(BaseModel):
    transaction_id: str = Field(pattern=r"^\d{8}-\d{6}-[0-9a-f]{8}$")


class XmpCleanupPreviewBody(BaseModel):
    root_path: str = Field(min_length=1, max_length=2000)
    recursive: bool = False


class XmpCleanupExecuteBody(BaseModel):
    plan_id: str = Field(pattern=r"^\d{8}-\d{6}-[0-9a-f]{8}$")


class XmpCleanupRollbackBody(BaseModel):
    transaction_id: str = Field(pattern=r"^\d{8}-\d{6}-[0-9a-f]{8}$")


class DevelopGenerateBody(BaseModel):
    base_revision: int = Field(ge=0)


class DevelopEditBody(BaseModel):
    base_revision: int = Field(ge=0)
    crop_id: str | None = Field(None, min_length=1, max_length=40)
    style_id: str | None = Field(None, min_length=1, max_length=40)
    style_strength: int | None = Field(None, ge=0, le=100)
    confirmed: bool | None = None


class DevelopConfirmBody(BaseModel):
    base_revision: int = Field(ge=0)


class DevelopOptionsBody(BaseModel):
    base_revision: int = Field(ge=0)
    color_enabled: bool | None = None
    mode: Literal["skip", "auto", "style"] | None = None


class StyleLibrarySyncBody(BaseModel):
    include_lightroom_presets: bool = True
    include_user_uploads: bool = True


class StyleLibraryItemVisibilityBody(BaseModel):
    hidden: bool


class StyleImportFileBody(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    content_base64: str = Field(min_length=1)


class StyleLibraryImportBody(BaseModel):
    files: list[StyleImportFileBody] = Field(
        min_length=1, max_length=MAX_STYLE_IMPORT_FILES
    )


class StyleRecommendationBody(BaseModel):
    base_revision: int = Field(ge=0)
    scope: Literal["global", "group"]
    group_id: int | None = Field(None, ge=1)
    force: bool = False

    @model_validator(mode="after")
    def validate_scope(self) -> StyleRecommendationBody:
        if self.scope == "group" and self.group_id is None:
            raise ValueError("按组推荐必须指定 group_id。")
        if self.scope == "global" and self.group_id is not None:
            raise ValueError("全局统一推荐不能指定 group_id。")
        return self


class AllGroupsStyleRecommendationBody(BaseModel):
    base_revision: int = Field(ge=0)


class StylePreviewBody(BaseModel):
    base_revision: int = Field(ge=0)
    scope: Literal["global", "group"] = "group"
    group_id: int | None = Field(None, ge=1)
    preset_id: str | None = Field(None, min_length=1, max_length=256)
    preset_hash: str | None = Field(None, min_length=1, max_length=128)
    lut_id: str | None = Field(None, min_length=1, max_length=256)
    lut_hash: str | None = Field(None, min_length=1, max_length=128)
    amount: int = Field(100, ge=0, le=200)
    strength: int | None = Field(None, ge=0, le=200)

    @model_validator(mode="after")
    def validate_resource(self) -> StylePreviewBody:
        if self.scope == "group" and self.group_id is None:
            raise ValueError("按组预览必须指定 group_id。")
        if self.scope == "global" and self.group_id is not None:
            raise ValueError("全局统一预览不能指定 group_id。")
        preset_pair = self.preset_id is not None or self.preset_hash is not None
        lut_pair = self.lut_id is not None or self.lut_hash is not None
        if preset_pair == lut_pair:
            raise ValueError(
                "必须且只能提供一组 preset_id/preset_hash 或 lut_id/lut_hash。"
            )
        if preset_pair and (self.preset_id is None or self.preset_hash is None):
            raise ValueError("preset_id 与 preset_hash 必须成对提供。")
        if lut_pair and (self.lut_id is None or self.lut_hash is None):
            raise ValueError("lut_id 与 lut_hash 必须成对提供。")
        if self.strength is not None:
            if "amount" in self.model_fields_set and self.amount != self.strength:
                raise ValueError("strength 与 amount 必须一致。")
            self.amount = self.strength
        return self


class StyleGroupBody(BaseModel):
    base_revision: int = Field(ge=0)
    preset_id: str | None = Field(None, max_length=256)
    preset_hash: str | None = Field(None, max_length=128)
    lut_id: str | None = Field(None, max_length=256)
    lut_hash: str | None = Field(None, max_length=128)
    amount: int = Field(100, ge=0, le=200)
    strength: int | None = Field(None, ge=0, le=200)
    status: Literal["pending", "confirmed", "skipped"] = "confirmed"

    @model_validator(mode="after")
    def validate_resource(self) -> StyleGroupBody:
        if self.strength is not None:
            if "amount" in self.model_fields_set and self.amount != self.strength:
                raise ValueError("strength 与 amount 必须一致。")
            self.amount = self.strength
        if self.status == "skipped":
            return self
        preset_pair = self.preset_id is not None or self.preset_hash is not None
        lut_pair = self.lut_id is not None or self.lut_hash is not None
        if preset_pair == lut_pair:
            raise ValueError(
                "必须且只能提供一组 preset_id/preset_hash 或 lut_id/lut_hash。"
            )
        if preset_pair and (not self.preset_id or not self.preset_hash):
            raise ValueError("preset_id 与 preset_hash 必须成对提供。")
        if lut_pair and (not self.lut_id or not self.lut_hash):
            raise ValueError("lut_id 与 lut_hash 必须成对提供。")
        return self


class ExportPrepareBody(BaseModel):
    base_revision: int = Field(ge=0)
    develop_revision: int | None = Field(None, ge=0)
    xmp: bool = True
    jpeg: bool = False
    jpeg_output_dir: str | None = Field(None, max_length=2000)
    jpeg_settings: dict[str, Any] = Field(default_factory=dict)


class ExportExecuteBody(BaseModel):
    retry_failed_only: bool = True


class LightroomConfigureBody(BaseModel):
    executable_path: str | None = Field(None, max_length=2000)


class ModelResourcesConfigureBody(BaseModel):
    profile_id: Literal["8gb", "16gb"]


class ModelResourcesOfflineImportBody(BaseModel):
    package_path: str = Field(min_length=1, max_length=4000)


class SettingsImportBody(BaseModel):
    settings: dict[str, Any]


class RatingBody(BaseModel):
    rating: int | None = Field(None, ge=0, le=5)
    base_revision: int = Field(0, ge=0)


class BulkRatingBody(BaseModel):
    indexes: list[int] = Field(min_length=1, max_length=5000)
    rating: int | None = Field(None, ge=0, le=5)
    base_revision: int = Field(0, ge=0)


class RollbackBody(BaseModel):
    transaction_id: str
    confirmation: str


class DeleteProjectBody(BaseModel):
    confirmation: str


def _review_path(run_file: Path) -> Path:
    return run_file.parent / "review.json"


def _review(run_file: Path) -> dict[str, Any]:
    return {
        "revision": 0,
        "ratings": {},
        "groups": {},
        "excluded": {},
        "updated_at": None,
        **_json(_review_path(run_file), {}),
    }


def _apply_review(payload: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    cloned = json.loads(json.dumps(payload))
    ratings = (
        review.get("ratings", {}) if isinstance(review.get("ratings"), dict) else {}
    )
    group_overrides = (
        review.get("groups", {}) if isinstance(review.get("groups"), dict) else {}
    )
    excluded_overrides = (
        review.get("excluded", {}) if isinstance(review.get("excluded"), dict) else {}
    )
    effective_group_overrides: dict[str, int] = {}
    effective_exclusion_changes: dict[str, bool] = {}
    for index, item in enumerate(cloned.get("results", [])):
        key = str(index)
        original_excluded = bool(item.get("excluded", False))
        excluded = (
            bool(excluded_overrides[key])
            if key in excluded_overrides
            else original_excluded
        )
        item["ai_excluded"] = original_excluded
        item["excluded"] = excluded
        item["manual_excluded_override"] = excluded != original_excluded
        if excluded != original_excluded:
            effective_exclusion_changes[key] = excluded
        original_group = int(item.get("group_id", index + 1))
        item["ai_group_id"] = original_group
        try:
            override = (
                int(group_overrides[key]) if key in group_overrides else original_group
            )
        except (TypeError, ValueError):
            override = original_group
        if override > 0 and override != original_group:
            item["group_id"] = override
            item["manual_group_override"] = True
            if not excluded:
                effective_group_overrides[key] = override
        else:
            item["group_id"] = original_group
            item["manual_group_override"] = False
        item["ai_rating"] = int(item.get("rating", 0))
        if key in ratings:
            item["manual_rating"] = int(ratings[key])
            item["rating"] = int(ratings[key])
            item["manual_override"] = True
            original = list(item.get("keywords", []))
            keywords = [
                value
                for value in original
                if not value.startswith("AI|候选") and not value.startswith("人工|")
            ]
            if item["rating"] == 5:
                keywords.append("人工|终选")
            elif item["rating"] >= 3:
                keywords.append("人工|候选")
            else:
                keywords.append("人工|暂不推荐")
            item["keywords"] = keywords
        else:
            item["manual_override"] = False
    group_sizes: dict[int, int] = {}
    for item in cloned.get("results", []):
        if item.get("excluded"):
            continue
        group_id = int(item["group_id"])
        group_sizes[group_id] = group_sizes.get(group_id, 0) + 1
    for item in cloned.get("results", []):
        item["group_size"] = (
            0 if item.get("excluded") else group_sizes[int(item["group_id"])]
        )
    active_results = [
        item for item in cloned.get("results", []) if not item.get("excluded")
    ]
    workflow_state = str(cloned.get("workflow_state") or "scored")
    cloned["workflow_state"] = workflow_state
    cloned["review_revision"] = int(review.get("revision", 0))
    cloned["image_count"] = len(cloned.get("results", []))
    cloned["active_image_count"] = len(active_results)
    cloned["excluded_count"] = cloned["image_count"] - cloned["active_image_count"]
    cloned["candidate_count"] = sum(
        1 for item in active_results if int(item.get("rating", 0)) >= 3
    )
    cloned["strong_count"] = sum(
        1 for item in active_results if int(item.get("rating", 0)) >= 4
    )
    cloned["manual_adjusted_count"] = len(ratings)
    cloned["manual_group_adjusted_count"] = len(effective_group_overrides)
    cloned["manual_excluded_adjusted_count"] = len(effective_exclusion_changes)
    cloned["group_count"] = len(group_sizes)
    cloned["needs_rescore"] = workflow_state == "scored" and bool(
        effective_group_overrides or effective_exclusion_changes
    )
    cloned["xmp_ready"] = workflow_state == "scored" and not cloned["needs_rescore"]
    return cloned


def _materialize_grouping(
    run_file: Path, payload: dict[str, Any] | None = None
) -> Path:
    payload = payload or _apply_review(read_json(run_file), _review(run_file))
    for item in payload.get("results", []):
        raw = Path(item.get("path", ""))
        if raw.is_file() and not item.get("source_key"):
            item["source_key"] = cache_key(raw)
    target = run_file.parent / f"grouping.snapshot-{uuid.uuid4().hex}.json"
    write_json(target, payload)
    return target


def _run_summary(run_file: Path) -> dict[str, Any]:
    payload = _apply_review(read_json(run_file), _review(run_file))
    ratings = [
        int(item.get("rating", 0))
        for item in payload.get("results", [])
        if not item.get("excluded")
    ]
    develop = develop_summary(
        load_develop_plan(run_file.parent), int(payload.get("review_revision", 0))
    )
    return {
        "run_id": payload.get("run_id", run_file.parent.name),
        "input_root": payload.get("input_root"),
        "name": Path(payload.get("input_root", run_file.parent.name)).name,
        "image_count": payload.get("image_count", len(ratings)),
        "active_image_count": len(ratings),
        "excluded_count": payload.get("excluded_count", 0),
        "candidate_count": sum(value >= 3 for value in ratings),
        "strong_count": sum(value >= 4 for value in ratings),
        "rejected_count": sum(value == 0 for value in ratings),
        "manual_adjusted_count": payload.get("manual_adjusted_count", 0),
        "manual_group_adjusted_count": payload.get("manual_group_adjusted_count", 0),
        "manual_excluded_adjusted_count": payload.get(
            "manual_excluded_adjusted_count", 0
        ),
        "group_count": payload.get("group_count", 0),
        "workflow_state": payload.get("workflow_state", "scored"),
        "needs_rescore": bool(payload.get("needs_rescore")),
        "xmp_ready": bool(payload.get("xmp_ready")),
        "develop": develop,
        "retain_ratio": payload.get("retain_ratio"),
        "scoring_mode": payload.get("scoring_mode", "legacy"),
        "created_at": datetime.fromtimestamp(
            run_file.stat().st_mtime, timezone.utc
        ).isoformat(),
        "has_dry_run": (run_file.parent / "xmp-dry-run.json").is_file(),
        "transaction_count": len(
            list(run_file.parent.glob("xmp-commit-manifest*.json"))
        ),
    }


def _runs(data_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    root = runs_root(data_dir)
    paths = [
        path / "results.json"
        for path in root.glob("*")
        if path.is_dir()
        and RUN_ID_RE.fullmatch(path.name)
        and (path / "results.json").is_file()
    ]
    return [
        _run_summary(path)
        for path in sorted(paths, key=lambda item: item.parent.name, reverse=True)[
            :limit
        ]
    ]


def _normalized_project_root(value: str | None) -> str:
    if not value:
        return ""
    try:
        normalized = os.path.normpath(
            str(Path(value).expanduser().resolve(strict=False))
        )
    except (OSError, ValueError):
        normalized = os.path.normpath(str(value))
    return normalized.casefold() if os.name == "nt" else normalized


def _project_id(input_root: str | None, run_id: str | None = None) -> str:
    identity = _normalized_project_root(input_root) or f"run:{run_id or 'unknown'}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _project_registrations_root(data_dir: Path) -> Path:
    return data_dir / "projects"


def _project_registration_path(data_dir: Path, project_id: str) -> Path:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("工程编号无效。")
    return _project_registrations_root(data_dir) / f"{project_id}.json"


def _registered_projects(data_dir: Path) -> dict[str, dict[str, Any]]:
    root = _project_registrations_root(data_dir)
    if not root.is_dir():
        return {}
    projects: dict[str, dict[str, Any]] = {}
    for path in root.glob("*.json"):
        if not PROJECT_ID_RE.fullmatch(path.stem):
            continue
        try:
            payload = read_json(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("project_id") != path.stem
            or not payload.get("input_root")
            or _project_id(str(payload.get("input_root"))) != path.stem
        ):
            continue
        projects[path.stem] = payload
    return projects


def _register_project(data_dir: Path, input_root: Path) -> dict[str, Any]:
    root = input_root.resolve(strict=True)
    project_id = _project_id(str(root))
    destination = _project_registration_path(data_dir, project_id)
    if destination.is_file():
        existing = _registered_projects(data_dir).get(project_id)
        if existing and _normalized_project_root(
            existing.get("input_root")
        ) == _normalized_project_root(str(root)):
            return existing
        raise ValueError("工程记录与照片目录不一致。")
    now = _now()
    payload = {
        "schema_version": 1,
        "project_id": project_id,
        "name": root.name or str(root),
        "input_root": str(root),
        "created_at": now,
        "updated_at": now,
    }
    write_json(destination, payload)
    return payload


def _dry_run_summary(run_file: Path) -> dict[str, Any] | None:
    path = run_file.parent / "xmp-dry-run.json"
    payload = _json(path, None)
    if not isinstance(payload, dict) or payload.get("commit") is True:
        return None
    records = payload.get("records", [])
    created_at = (
        payload.get("created_at")
        or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    )
    return {
        "kind": "dry_run",
        "created_at": created_at,
        "planned_count": int(
            payload.get(
                "planned_count",
                sum(record.get("status") == "planned" for record in records),
            )
        ),
        "skipped_count": int(
            payload.get(
                "skipped_count",
                sum(
                    str(record.get("status", "")).startswith("skipped")
                    for record in records
                ),
            )
        ),
        "failed_count": int(
            payload.get(
                "failed_count",
                sum(
                    str(record.get("status", "")).startswith("failed")
                    for record in records
                ),
            )
        ),
        "min_rating": payload.get("min_rating"),
        "limit": payload.get("limit"),
    }


def _run_detail(data_dir: Path, run_id: str) -> dict[str, Any]:
    run_file = _run_file(data_dir, run_id)
    review = _review(run_file)
    payload = _apply_review(read_json(run_file), review)
    results = []
    for index, item in enumerate(payload.get("results", [])):
        raw = Path(item["path"])
        results.append(
            {
                **item,
                "index": index,
                "filename": raw.name,
                "effective_rating": int(item.get("rating", 0)),
                "preview_url": f"/api/runs/{run_id}/preview/{index}",
                "existing_xmp": raw.with_suffix(".xmp").is_file(),
            }
        )
    payload["results"] = results
    payload["review_revision"] = review["revision"]
    payload["dry_run"] = _json(run_file.parent / "xmp-dry-run.json", None)
    payload["develop"] = develop_summary(
        load_develop_plan(run_file.parent), int(review["revision"])
    )
    return payload


def _public_develop_plan(
    run_file: Path, plan: dict[str, Any], review_revision: int
) -> dict[str, Any]:
    summary = develop_summary(plan, review_revision)
    creative = copy.deepcopy(
        summary.get("creative_style") or {"status": "pending", "groups": {}}
    )
    selections = list((creative.get("groups") or {}).values())
    if isinstance(creative.get("global_selection"), dict):
        selections.append(creative["global_selection"])
    for selection in selections:
        if not isinstance(selection, dict):
            continue

        def public_preview_samples(values: Any) -> list[dict[str, Any]]:
            public_samples: list[dict[str, Any]] = []
            for sample in values or []:
                if not isinstance(sample, dict):
                    continue
                public_sample = {
                    key: value
                    for key, value in sample.items()
                    if key not in {"preview_path", "source_path"}
                }
                sample_key = str(sample.get("preview_key") or "")
                if STYLE_PREVIEW_KEY_RE.fullmatch(sample_key):
                    public_sample["preview_url"] = (
                        f"/api/runs/{run_file.parent.name}/style-preview/{sample_key}"
                    )
                public_samples.append(public_sample)
            return public_samples

        neutral_key = str(selection.get("neutral_preview_key") or "")
        if STYLE_PREVIEW_KEY_RE.fullmatch(neutral_key):
            selection["neutral_preview_url"] = (
                f"/api/runs/{run_file.parent.name}/style-preview/{neutral_key}"
            )
        selected_key = str(selection.get("selected_preview_key") or "")
        if STYLE_PREVIEW_KEY_RE.fullmatch(selected_key):
            selection["selected_preview_url"] = (
                f"/api/runs/{run_file.parent.name}/style-preview/{selected_key}"
            )
        selection["selected_preview_samples"] = public_preview_samples(
            selection.get("selected_preview_samples")
        )
        public_top3: list[dict[str, Any]] = []
        for candidate in selection.get("top3") or []:
            if not isinstance(candidate, dict):
                continue
            public_candidate = {
                key: value for key, value in candidate.items() if key != "preview_path"
            }
            preview_key = str(candidate.get("preview_key") or "")
            if STYLE_PREVIEW_KEY_RE.fullmatch(preview_key):
                public_candidate["preview_url"] = (
                    f"/api/runs/{run_file.parent.name}/style-preview/{preview_key}"
                )
            public_candidate["preview_samples"] = public_preview_samples(
                candidate.get("preview_samples")
            )
            public_top3.append(public_candidate)
        selection["top3"] = public_top3
        selection.pop("neutral_preview_path", None)
        selection.pop("selected_preview_path", None)
    summary["creative_style"] = creative
    items = []
    for item in plan.get("items", []):
        index = int(item.get("index", -1))
        public = {key: value for key, value in item.items() if key != "preview_path"}
        public["preview_url"] = (
            f"/api/runs/{run_file.parent.name}/develop/preview/{index}?revision={int(plan.get('revision', 0))}"
        )
        items.append(public)
    return {
        **summary,
        "run_id": plan.get("run_id", run_file.parent.name),
        "source_review_revision": int(plan.get("source_review_revision", -1)),
        "styles": [
            {
                "id": style_id,
                "label": value["label"],
                "description": value["description"],
            }
            for style_id, value in STYLE_PRESETS.items()
        ],
        "items": items,
    }


def _require_current_develop(run_file: Path, review_revision: int) -> dict[str, Any]:
    plan = load_develop_plan(run_file.parent)
    if not plan:
        raise HTTPException(404, "尚未生成裁剪调色方案。")
    summary = develop_summary(plan, review_revision)
    if (
        summary.get("engine_outdated")
        or int(plan.get("schema_version", 0)) != DEVELOP_SCHEMA_VERSION
    ):
        raise HTTPException(409, "处理引擎已经升级，请重新生成裁剪调色方案。")
    if int(plan.get("source_review_revision", -1)) != int(review_revision):
        raise HTTPException(409, "星级或分组已变化，请重新生成裁剪调色方案。")
    return plan


def _edit_review(
    data_dir: Path,
    run_id: str,
    indexes: list[int],
    rating: int | None,
    revision: int,
    lock: threading.RLock,
) -> dict[str, Any]:
    run_file = _run_file(data_dir, run_id)
    with lock:
        payload = read_json(run_file)
        reviewed_payload = _apply_review(payload, _review(run_file))
        if reviewed_payload.get("workflow_state") != "scored" or reviewed_payload.get(
            "needs_rescore"
        ):
            raise HTTPException(409, "请先完成当前分组评分，再调整星级。")
        for index in indexes:
            if index < 0 or index >= len(payload.get("results", [])):
                raise HTTPException(404, f"照片序号不存在：{index}")
            if reviewed_payload["results"][index].get("excluded"):
                raise HTTPException(409, "已从工程移除的照片不能调整星级，请先恢复。")
        review = _review(run_file)
        if int(review["revision"]) != revision:
            raise HTTPException(409, "审片结果已在别处更新，请刷新后重试。")
        ratings = dict(review.get("ratings", {}))
        for index in indexes:
            if rating is None:
                ratings.pop(str(index), None)
            else:
                ratings[str(index)] = rating
        review = {
            "revision": revision + 1,
            "ratings": ratings,
            "groups": dict(review.get("groups", {})),
            "excluded": dict(review.get("excluded", {})),
            "updated_at": _now(),
        }
        write_json(_review_path(run_file), review)
    return {
        "review_revision": review["revision"],
        "manual_adjusted_count": len(ratings),
        "indexes": indexes,
        "rating": rating,
    }


def _edit_groups(
    data_dir: Path,
    run_id: str,
    indexes: list[int],
    group_id: int | None,
    revision: int,
    lock: threading.RLock,
    *,
    new_group: bool = False,
    direction: Literal["previous", "next"] | None = None,
    allow_missing_target: bool = False,
) -> dict[str, Any]:
    run_file = _run_file(data_dir, run_id)
    with lock:
        payload = read_json(run_file)
        results = payload.get("results", [])
        indexes = list(dict.fromkeys(indexes))
        for index in indexes:
            if index < 0 or index >= len(results):
                raise HTTPException(404, f"照片序号不存在：{index}")
        review = _review(run_file)
        if int(review["revision"]) != revision:
            raise HTTPException(409, "分组已在别处更新，请刷新后重试。")
        current = _apply_review(payload, review)
        if any(current["results"][index].get("excluded") for index in indexes):
            raise HTTPException(409, "已移除的照片请先恢复，再调整分组。")
        active_groups = sorted(
            {
                int(item.get("group_id", 0))
                for item in current.get("results", [])
                if not item.get("excluded")
            }
        )
        target_group = group_id
        direction_targets: dict[int, int] = {}
        if direction:
            step = -1 if direction == "previous" else 1
            positions = {
                value: position for position, value in enumerate(active_groups)
            }
            for index in indexes:
                current_group = int(current["results"][index].get("group_id", 0))
                target_position = positions[current_group] + step
                direction_targets[index] = (
                    active_groups[target_position]
                    if 0 <= target_position < len(active_groups)
                    else current_group
                )
        elif new_group:
            target_group = (
                max(
                    (
                        int(item.get("group_id", 0))
                        for item in current.get("results", [])
                    ),
                    default=0,
                )
                + 1
            )
            if target_group > 100000:
                raise HTTPException(422, "分组数量已达到上限。")
        elif (
            target_group is not None
            and target_group not in active_groups
            and not allow_missing_target
        ):
            raise HTTPException(422, "目标分组不存在，请选择已有分组或新建一组。")
        groups = dict(review.get("groups", {}))
        moved_indexes: list[int] = []
        for index in indexes:
            current_group = int(current["results"][index].get("group_id", 0))
            item_target = direction_targets[index] if direction else target_group
            if item_target == current_group:
                continue
            moved_indexes.append(index)
            original_group = int(results[index].get("group_id", index + 1))
            if item_target is None or item_target == original_group:
                groups.pop(str(index), None)
            else:
                groups[str(index)] = int(item_target)
        if direction and not moved_indexes:
            applied = current
        else:
            review = {
                "revision": revision + 1,
                "ratings": dict(review.get("ratings", {})),
                "groups": groups,
                "excluded": dict(review.get("excluded", {})),
                "updated_at": _now(),
            }
            write_json(_review_path(run_file), review)
            applied = _apply_review(payload, review)
        item_states = [
            {
                "index": index,
                "group_id": int(applied["results"][index]["group_id"]),
                "ai_group_id": int(applied["results"][index]["ai_group_id"]),
                "manual_group_override": bool(
                    applied["results"][index].get("manual_group_override")
                ),
            }
            for index in indexes
        ]
    return {
        "review_revision": review["revision"],
        "indexes": indexes,
        "group_id": target_group,
        "direction": direction,
        "moved_count": len(moved_indexes),
        "manual_group_adjusted_count": applied["manual_group_adjusted_count"],
        "group_count": applied["group_count"],
        "needs_rescore": applied["needs_rescore"],
        "items": item_states,
    }


def _edit_group(
    data_dir: Path,
    run_id: str,
    index: int,
    group_id: int | None,
    revision: int,
    lock: threading.RLock,
) -> dict[str, Any]:
    result = _edit_groups(
        data_dir,
        run_id,
        [index],
        group_id,
        revision,
        lock,
        allow_missing_target=True,
    )
    item = result["items"][0]
    return {
        **result,
        "index": index,
        "group_id": item["group_id"],
        "ai_group_id": item["ai_group_id"],
        "manual_group_override": item["manual_group_override"],
    }


def _edit_excluded(
    data_dir: Path,
    run_id: str,
    indexes: list[int],
    excluded: bool,
    revision: int,
    lock: threading.RLock,
) -> dict[str, Any]:
    run_file = _run_file(data_dir, run_id)
    with lock:
        payload = read_json(run_file)
        results = payload.get("results", [])
        indexes = list(dict.fromkeys(indexes))
        for index in indexes:
            if index < 0 or index >= len(results):
                raise HTTPException(404, f"照片序号不存在：{index}")
        review = _review(run_file)
        if int(review["revision"]) != revision:
            raise HTTPException(409, "分组已在别处更新，请刷新后重试。")
        overrides = dict(review.get("excluded", {}))
        for index in indexes:
            original = bool(results[index].get("excluded", False))
            if excluded == original:
                overrides.pop(str(index), None)
            else:
                overrides[str(index)] = excluded
        updated = {
            "revision": revision + 1,
            "ratings": dict(review.get("ratings", {})),
            "groups": dict(review.get("groups", {})),
            "excluded": overrides,
            "updated_at": _now(),
        }
        applied = _apply_review(payload, updated)
        if int(applied.get("active_image_count", 0)) <= 0:
            raise HTTPException(422, "工程至少需要保留一张照片。")
        write_json(_review_path(run_file), updated)
    return {
        "review_revision": updated["revision"],
        "indexes": indexes,
        "excluded": excluded,
        "active_image_count": applied["active_image_count"],
        "excluded_count": applied["excluded_count"],
        "manual_group_adjusted_count": applied["manual_group_adjusted_count"],
        "group_count": applied["group_count"],
        "needs_rescore": applied["needs_rescore"],
    }


def _transaction_id(data_dir: Path, path: Path) -> str:
    relative = str(path.relative_to(data_dir)).replace("\\", "/")
    return hashlib.sha256(relative.encode()).hexdigest()[:20]


def _transactions(data_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    items: list[dict[str, Any]] = []
    mapping: dict[str, Path] = {}
    for path in sorted(
        runs_root(data_dir).glob("*/xmp-commit-manifest*.json"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    ):
        payload = _json(path, {})
        if not payload or payload.get("commit") is not True:
            continue
        transaction_id = _transaction_id(data_dir, path)
        mapping[transaction_id] = path
        created = [
            record
            for record in payload.get("records", [])
            if record.get("status")
            in {"created", "created_pending_validation", "creating"}
        ]
        remaining = sum(
            Path(record.get("xmp_path", "")).is_file() for record in created
        )
        source = Path(payload.get("source_results", ""))
        items.append(
            {
                "id": transaction_id,
                "kind": "commit",
                "run_id": source.parent.name if source.name else None,
                "owner_run_id": path.parent.name,
                "created_at": payload.get("created_at"),
                "created_count": len(created),
                "skipped_count": payload.get("skipped_count", 0),
                "remaining_count": remaining,
                "rollbackable": remaining > 0,
                "manifest_name": path.name,
            }
        )
    return items, mapping


def _project_collection(
    data_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    registered = _registered_projects(data_dir)
    runs = _runs(data_dir, 1_000_000)
    transactions, _ = _transactions(data_dir)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        project_id = _project_id(run.get("input_root"), run.get("run_id"))
        grouped.setdefault(project_id, []).append({**run, "project_id": project_id})

    summaries: list[dict[str, Any]] = []
    details: dict[str, dict[str, Any]] = {}
    for project_id, project_runs in grouped.items():
        project_runs.sort(key=lambda item: item["run_id"], reverse=True)
        run_ids = {item["run_id"] for item in project_runs}
        owned_transactions = [
            item for item in transactions if item.get("owner_run_id") in run_ids
        ]
        versions: list[dict[str, Any]] = []
        for run in project_runs:
            run_file = runs_root(data_dir) / run["run_id"] / "results.json"
            versions.append(
                {
                    **run,
                    "dry_run": _dry_run_summary(run_file),
                    "transactions": [
                        item
                        for item in owned_transactions
                        if item.get("run_id") == run["run_id"]
                    ],
                }
            )

        orphan_transactions = [
            item for item in owned_transactions if item.get("run_id") not in run_ids
        ]
        latest = versions[0]
        activity_times = [item.get("created_at") for item in versions]
        activity_times.extend(item.get("created_at") for item in owned_transactions)
        activity_times.extend(
            item["dry_run"].get("created_at")
            for item in versions
            if item.get("dry_run")
        )
        updated_at = max(
            (value for value in activity_times if value), default=latest["created_at"]
        )
        summary = {
            "project_id": project_id,
            "name": latest["name"],
            "input_root": latest.get("input_root"),
            "version_count": len(versions),
            "image_count": latest["image_count"],
            "candidate_count": latest["candidate_count"],
            "strong_count": latest["strong_count"],
            "xmp_count": sum(item["remaining_count"] for item in owned_transactions),
            "transaction_count": len(owned_transactions),
            "updated_at": updated_at,
            "latest_run_id": latest["run_id"],
            "latest_result": latest,
        }
        summaries.append(summary)
        details[project_id] = {
            **summary,
            "versions": versions,
            "orphan_transactions": orphan_transactions,
        }

    for project_id, registration in registered.items():
        if project_id in details:
            details[project_id]["created_at"] = registration.get("created_at")
            continue
        summary = {
            "project_id": project_id,
            "name": registration.get("name")
            or Path(str(registration["input_root"])).name,
            "input_root": registration["input_root"],
            "version_count": 0,
            "image_count": 0,
            "candidate_count": 0,
            "strong_count": 0,
            "xmp_count": 0,
            "transaction_count": 0,
            "created_at": registration.get("created_at"),
            "updated_at": registration.get("updated_at")
            or registration.get("created_at"),
            "latest_run_id": None,
            "latest_result": None,
            "workflow_state": "created",
        }
        summaries.append(summary)
        details[project_id] = {
            **summary,
            "versions": [],
            "orphan_transactions": [],
        }

    summaries.sort(key=lambda item: item["updated_at"], reverse=True)
    return summaries, details


def _projects(data_dir: Path) -> list[dict[str, Any]]:
    return _project_collection(data_dir)[0]


def _project_detail(data_dir: Path, project_id: str) -> dict[str, Any]:
    project = _project_collection(data_dir)[1].get(project_id)
    if not project:
        raise HTTPException(404, "工程不存在。")
    return project


def _trash_project(
    data_dir: Path, project_id: str, lock: threading.RLock
) -> dict[str, Any]:
    with lock:
        project = _project_detail(data_dir, project_id)
        project_runs_root = runs_root(data_dir).resolve()
        run_ids = [version["run_id"] for version in project["versions"]]
        sources: list[tuple[Path, str]] = []
        for run_id in run_ids:
            if not RUN_ID_RE.fullmatch(run_id):
                raise HTTPException(409, f"工程包含异常版本：{run_id}")
            source = (project_runs_root / run_id).resolve()
            if (
                source.parent != project_runs_root
                or not source.is_dir()
                or not (source / "results.json").is_file()
            ):
                raise HTTPException(409, f"工程版本目录异常：{run_id}")
            sources.append((source, run_id))

        latest = project_runs_root / "latest"
        latest_payload = _json(latest / "results.json", {})
        if latest.is_dir() and latest_payload.get("run_id") in run_ids:
            latest_source = latest.resolve()
            if latest_source.parent != project_runs_root:
                raise HTTPException(409, "最新结果目录异常，已拒绝操作。")
            sources.append((latest_source, "latest-snapshot"))

        registration = _project_registration_path(data_dir, project_id)
        if registration.is_file():
            registration_root = _project_registrations_root(data_dir).resolve()
            registration_source = registration.resolve()
            if registration_source.parent != registration_root:
                raise HTTPException(409, "工程登记文件异常，已拒绝操作。")
            sources.append((registration_source, "registration.json"))

        deleted_at = datetime.now(timezone.utc)
        backups_root = Path(os.environ.get("PHOTO_AI_BACKUPS_DIR") or data_dir / "trash")
        recycle_root = (backups_root / "projects").resolve()
        recycle_root.mkdir(parents=True, exist_ok=True)
        recycle_id = f"{deleted_at.strftime('%Y%m%d-%H%M%S')}-{project_id}-{uuid.uuid4().hex[:8]}"
        target = recycle_root / recycle_id
        if not _inside(target, recycle_root) or target.exists():
            raise HTTPException(409, "工程回收目录异常，已拒绝操作。")
        target.mkdir()
        marker = target / "project.json"
        write_json(
            marker,
            {
                "schema_version": 1,
                "deleted_at": deleted_at.isoformat(),
                "project_id": project_id,
                "name": project["name"],
                "input_root": project["input_root"],
                "run_ids": run_ids,
                "version_count": len(run_ids),
                "xmp_count_at_delete": project["xmp_count"],
                "raw_files_deleted": 0,
                "xmp_files_deleted": 0,
            },
        )

        moved: list[tuple[Path, Path]] = []
        try:
            for source, name in sources:
                destination = target / name
                source.rename(destination)
                moved.append((source, destination))
        except OSError as exc:
            for source, destination in reversed(moved):
                try:
                    destination.rename(source)
                except OSError:
                    pass
            try:
                marker.unlink(missing_ok=True)
                target.rmdir()
            except OSError:
                pass
            raise HTTPException(
                500, "工程未能完整移入回收目录，已尝试恢复原状。"
            ) from exc

        return {
            "project_id": project_id,
            "name": project["name"],
            "recycle_id": recycle_id,
            "version_count": len(run_ids),
            "xmp_count": project["xmp_count"],
            "raw_files_deleted": 0,
            "xmp_files_deleted": 0,
        }


def _ensure_xmp_ready(payload: dict[str, Any]) -> None:
    if payload.get("workflow_state") != "scored":
        raise HTTPException(409, "这个工程尚未评分，不能写入 XMP。")
    if payload.get("needs_rescore"):
        raise HTTPException(409, "分组已经修改，请重新评分后再写入 XMP。")


def _lightroom_plugin_dir(project_root: Path) -> Path:
    configured = os.environ.get("PHOTO_AI_LIGHTROOM_PLUGIN_TEMPLATE")
    if configured:
        return Path(configured).expanduser().resolve()
    resource_root = os.environ.get("PHOTO_AI_RESOURCE_ROOT")
    if resource_root:
        bundled = (
            Path(resource_root) / "integrations" / "photo-ai-lightroom.lrplugin"
        ).resolve()
        if bundled.is_dir():
            return bundled
    return (project_root / "integrations" / "photo-ai-lightroom.lrplugin").resolve()


def _require_e_drive_lightroom_storage(data_dir: Path, project_root: Path) -> Path:
    """Validate the configured portable bridge and immutable plug-in template.

    The historical function name is retained for API/test compatibility.  A
    desktop installation may use any validated Content Root drive; it must
    never be coupled to the developer's E: drive.
    """

    plugin_dir = _lightroom_plugin_dir(project_root)
    if not data_dir.is_dir():
        raise HTTPException(409, f"数据目录不可用：{data_dir}")
    if not plugin_dir.is_dir():
        raise HTTPException(
            409, f"安装内容不完整：缺少 Lightroom 插件模板：{plugin_dir}"
        )
    return plugin_dir


def _lightroom_status(data_dir: Path, project_root: Path) -> dict[str, Any]:
    settings = _json(data_dir / "lightroom" / "settings.json", {})
    configured_executable = str(settings.get("executable_path") or "").strip()
    installed_plugin = installed_lightroom_plugin_dir()
    plugin_dir = (
        installed_plugin
        if (installed_plugin / "Info.lua").is_file()
        else _lightroom_plugin_dir(project_root)
    )
    status = get_lightroom_plugin_status(
        data_dir,
        search_roots=[Path(configured_executable)] if configured_executable else None,
        plugin_dir=plugin_dir,
        include_batches=False,
    )
    status["settings"] = {
        "executable_path": configured_executable or None,
        "auto_detected": not bool(configured_executable),
    }
    return status


def _require_style_lightroom(status: dict[str, Any]) -> None:
    lightroom = status.get("lightroom") or {}
    executable = Path(str(lightroom.get("executable") or ""))
    if not lightroom.get("compatible") or not executable.is_file():
        raise HTTPException(
            409, "没有检测到兼容的 Lightroom Classic（需要 14.3 或更高版本）。"
        )
    plugin = status.get("plugin") or {}
    if not status.get("configured") or not plugin.get("points_to_this_bridge"):
        raise HTTPException(409, "请先在设置中配置 Lightroom 桥接。")
    heartbeat = status.get("heartbeat") or {}
    if heartbeat.get("state") == "online":
        loaded = str(heartbeat.get("plugin_version") or "")
        if loaded != PLUGIN_VERSION:
            raise HTTPException(
                409,
                f"Lightroom 当前加载的是旧插件 {loaded or '未知版本'}；"
                f"请在插件管理器中重新加载“照片选片 - Lightroom” {PLUGIN_VERSION} 后再试。",
            )


def _style_library_settings(data_dir: Path) -> dict[str, Any]:
    payload = _json(managed_style_root(data_dir) / "settings.json", {})
    if not isinstance(payload, dict):
        payload = {}
    hidden = payload.get("hidden_resource_ids", [])
    return {
        "source_mode": "lightroom_user_only",
        "include_black_white": False,
        "include_adaptive": False,
        "include_lightroom_presets": bool(
            payload.get("include_lightroom_presets", True)
        ),
        "include_user_uploads": bool(payload.get("include_user_uploads", True)),
        "include_adobe_local_copy": False,
        "hidden_resource_ids": sorted(
            {
                str(value)
                for value in hidden
                if isinstance(value, str)
                and (value.startswith("xmp-") or value.startswith("lut-"))
            }
        ),
    }


def _write_style_library_settings(
    data_dir: Path, settings: dict[str, Any]
) -> dict[str, Any]:
    normalized = {
        "source_mode": "lightroom_user_only",
        "include_black_white": False,
        "include_adaptive": False,
        "include_lightroom_presets": bool(
            settings.get("include_lightroom_presets", True)
        ),
        "include_user_uploads": bool(settings.get("include_user_uploads", True)),
        "include_adobe_local_copy": False,
        "hidden_resource_ids": sorted(
            {
                str(value)
                for value in settings.get("hidden_resource_ids", [])
                if isinstance(value, str)
                and (value.startswith("xmp-") or value.startswith("lut-"))
            }
        ),
        "updated_at": _now(),
    }
    settings_path = managed_style_root(data_dir) / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(settings_path, normalized)
    return normalized


def _lut_resource_id(lut_id: str, lut_hash: str) -> str:
    encoded = f"{lut_id}\0{lut_hash}".encode("utf-8")
    return f"lut-{hashlib.sha256(encoded).hexdigest()[:32]}"


def _style_upload_source_root(data_dir: Path) -> Path:
    return managed_style_root(data_dir) / "user-upload"


def _style_archive_root(data_dir: Path) -> Path:
    return managed_style_root(data_dir).parent / "style-library-archive"


def _ensure_style_upload_manifest(data_dir: Path) -> Path:
    source_root = _style_upload_source_root(data_dir)
    manifest = source_root / "SOURCE.json"
    if not manifest.is_file():
        write_json(
            manifest,
            {
                "id": "user-upload",
                "title": "用户批量导入",
                "license_spdx": "user-provided",
                "tier": "curated_candidate",
                "source_url": None,
            },
        )
    return source_root


def _decode_style_imports(
    files: list[StyleImportFileBody],
) -> list[tuple[str, str, bytes]]:
    decoded: list[tuple[str, str, bytes]] = []
    total_bytes = 0
    for item in files:
        name = item.name.strip()
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or Path(name).name != name
        ):
            raise HTTPException(422, f"文件名无效：{item.name}")
        suffix = Path(name).suffix.casefold()
        if suffix not in {".xmp", ".cube"}:
            raise HTTPException(422, f"不支持的风格文件：{name}")
        try:
            content = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, f"文件内容无法解码：{name}") from exc
        if not content:
            raise HTTPException(422, f"文件为空：{name}")
        total_bytes += len(content)
        if total_bytes > MAX_STYLE_IMPORT_BYTES:
            raise HTTPException(413, "单次批量导入不能超过 96 MB。")
        decoded.append((name, suffix, content))
    return decoded


def _sync_registered_styles_if_newer(data_dir: Path) -> bool:
    registration = registration_state_path(data_dir)
    index = style_index_path(data_dir)
    if not registration.is_file() or (
        index.is_file() and registration.stat().st_mtime_ns <= index.stat().st_mtime_ns
    ):
        return False
    sync_style_library(data_dir)
    return True


def _style_entry_source_group(entry: dict[str, Any]) -> str | None:
    if str(entry.get("source_id") or "").casefold() == "user-upload":
        return "user"
    if entry.get("source_kind") in {"adobe-installed", "user-installed"}:
        return "lightroom"
    return None


def _configured_style_catalog(data_dir: Path) -> dict[str, Any]:
    _sync_registered_styles_if_newer(data_dir)
    catalog = load_style_index(data_dir)
    settings = _style_library_settings(data_dir)
    profile_pool: list[str] = []
    legacy_pool: list[str] = []
    for entry in catalog.get("entries", []):
        source_group = (
            _style_entry_source_group(entry) if isinstance(entry, dict) else None
        )
        if (
            not isinstance(entry, dict)
            or source_group is None
            or (
                source_group == "lightroom"
                and not settings["include_lightroom_presets"]
            )
            or (source_group == "user" and not settings["include_user_uploads"])
            or not entry.get("preset_id")
            or entry.get("duplicate_of")
            or entry.get("hidden")
            or not entry.get("ai_eligible")
            or entry.get("registration_status")
            not in (
                {"installed", "registered", "local_copy"}
                if entry.get("look_kind") == "lightroom_profile"
                else {"installed", "registered"}
            )
            or entry.get("compatibility") != "compatible"
            or entry.get("utility")
            or entry.get("experimental")
        ):
            continue
        if entry.get("black_and_white") and not settings["include_black_white"]:
            continue
        if entry.get("adaptive") and not settings["include_adaptive"]:
            continue
        target = (
            profile_pool
            if entry.get("look_kind") == "lightroom_profile"
            else legacy_pool
        )
        target.append(str(entry["preset_id"]))
    # Creative Profiles are LUT-backed, Amount-aware, and XMP-compatible.  Old
    # develop presets remain searchable as a migration fallback but are not
    # mixed into the default creative-look AI pool when profiles are present.
    pool = profile_pool or legacy_pool
    return {
        **catalog,
        "default_pool": pool,
        "candidate_mode": "creative_profiles" if profile_pool else "legacy_presets",
    }


def _public_style_library(
    data_dir: Path,
    lut_engine: CreativeLutEngine | None = None,
) -> dict[str, Any]:
    catalog = _configured_style_catalog(data_dir)
    settings = _style_library_settings(data_dir)
    hidden_resource_ids = set(settings["hidden_resource_ids"])
    pool = set(catalog.get("default_pool") or [])
    manageable_entries = [
        item
        for item in catalog.get("entries", [])
        if _style_entry_source_group(item) is not None
        and not item.get("parser_hidden")
        and not item.get("duplicate_of")
    ]
    entries = [
        {
            "resource_id": item.get("resource_id") or style_resource_id(item),
            "preset_id": item.get("preset_id"),
            "preset_hash": item.get("file_hash"),
            "look_kind": item.get("look_kind") or "develop_preset",
            "asset_kind": item.get("asset_kind") or "develop_preset",
            "profile_name": item.get("profile_name"),
            "profile_hash": item.get("file_hash")
            if item.get("look_kind") == "lightroom_profile"
            else None,
            "xmp_compatible": bool(item.get("xmp_compatible")),
            "uuid": item.get("uuid"),
            "label": item.get("name"),
            "group": item.get("group"),
            "source": item.get("source"),
            "source_kind": item.get("source_kind"),
            "source_id": item.get("source_id"),
            "source_group": _style_entry_source_group(item),
            "source_url": item.get("source_url"),
            "license": item.get("license"),
            "tier": item.get("source_tier")
            or (
                "community_experimental"
                if item.get("experimental")
                else "installed"
                if item.get("source_kind") != "managed"
                else "curated_candidate"
            ),
            "category": item.get("category"),
            "compatibility": item.get("compatibility"),
            "compatibility_reasons": item.get("compatibility_reasons") or [],
            "supports_amount": runtime_supports_amount(item),
            "adaptive": bool(item.get("adaptive")),
            "black_and_white": bool(item.get("black_and_white")),
            "profile_dependencies": item.get("profile_dependencies") or [],
            "ai_enabled": item.get("preset_id") in pool,
            "enabled": not bool(item.get("hidden")),
            "source_enabled": not bool(item.get("source_disabled")),
            "user_hidden": bool(item.get("user_hidden")),
            "can_hide": True,
            "can_delete": bool(
                item.get("source_kind") == "managed"
                and str(item.get("source_id") or "").casefold() == "user-upload"
            ),
        }
        for item in manageable_entries
    ]
    lut_entries = (
        []
        if lut_engine is None
        else [
            {
                "resource_id": _lut_resource_id(item.lut_id, item.lut_hash),
                "preset_id": None,
                "preset_hash": None,
                "lut_id": item.lut_id,
                "lut_hash": item.lut_hash,
                "look_kind": "rendered_lut",
                "asset_kind": "cube_lut",
                "profile_name": None,
                "profile_hash": None,
                "xmp_compatible": False,
                "label": item.name,
                "source": item.source_label or "Imported .cube",
                "source_kind": "managed_lut",
                "source_id": "user-upload",
                "source_group": "user",
                "source_url": item.source_url,
                "license": item.source_license,
                "tier": "imported_lut",
                "category": item.kind,
                "compatibility": "compatible",
                "compatibility_reasons": [],
                "supports_amount": True,
                "strength_min": 0,
                "strength_max": 200,
                "adaptive": False,
                "black_and_white": False,
                "profile_dependencies": [],
                # Imported .cube files are available for exact manual preview and
                # JPEG rendering. They are not part of the calibrated AI pool;
                # Creative Profiles remain the default AI recommendation source.
                "ai_enabled": False,
                "enabled": settings["include_user_uploads"]
                and _lut_resource_id(item.lut_id, item.lut_hash)
                not in hidden_resource_ids,
                "source_enabled": settings["include_user_uploads"],
                "user_hidden": _lut_resource_id(item.lut_id, item.lut_hash)
                in hidden_resource_ids,
                "can_hide": True,
                "can_delete": True,
                "manual_preview_enabled": settings["include_user_uploads"]
                and _lut_resource_id(item.lut_id, item.lut_hash)
                not in hidden_resource_ids,
                "render_targets": ["jpeg"],
                "xmp_limitation": ORDINARY_XMP_LUT_LIMITATION,
            }
            for item in lut_engine.list_luts()
        ]
    )
    lightroom_entries = [
        item for item in entries if item["source_group"] == "lightroom"
    ]
    user_entries = [item for item in entries if item["source_group"] == "user"]
    summary = dict(catalog.get("summary") or {})
    summary["total"] = len(entries) + len(lut_entries)
    summary["ai_pool"] = len(pool)
    summary["experimental"] = sum(
        bool(item.get("experimental")) for item in manageable_entries
    )
    summary["visible"] = sum(bool(item.get("enabled")) for item in entries) + sum(
        bool(item.get("enabled")) for item in lut_entries
    )
    summary["luts"] = len(lut_entries)
    summary["user_hidden"] = sum(
        bool(item.get("user_hidden")) for item in entries
    ) + sum(bool(item.get("user_hidden")) for item in lut_entries)
    return {
        "generated_at": catalog.get("generated_at"),
        "candidate_mode": catalog.get("candidate_mode"),
        "stats": summary,
        "settings": settings,
        "sources": {
            "lightroom": {
                "count": len(lightroom_entries),
                "enabled": settings["include_lightroom_presets"],
                "ai_pool": sum(
                    bool(item.get("ai_enabled")) for item in lightroom_entries
                ),
            },
            "user": {
                "count": len(user_entries) + len(lut_entries),
                "enabled": settings["include_user_uploads"],
                "ai_pool": sum(bool(item.get("ai_enabled")) for item in user_entries),
            },
        },
        # Keep one searchable resource collection for the existing picker while
        # also exposing LUTs separately to API clients that need target capability.
        "presets": [*entries, *lut_entries],
        "luts": lut_entries,
    }


def _recommendation_groups_for_develop(
    recommendation: dict[str, Any], catalog: dict[str, Any], plan: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    by_id = {str(item.get("preset_id")): item for item in catalog.get("entries", [])}
    plan_items = [item for item in plan.get("items", []) if isinstance(item, dict)]
    output: dict[str, dict[str, Any]] = {}
    for group in recommendation.get("groups", []):
        group_id = str(group.get("group_id"))
        candidates = []
        for candidate in group.get("candidates", [])[:3]:
            entry = by_id.get(str(candidate.get("preset_id")), {})
            lut_id = candidate.get("lut_id")
            look_kind = (
                candidate.get("look_kind")
                or entry.get("look_kind")
                or ("rendered_lut" if lut_id else "develop_preset")
            )
            candidates.append(
                {
                    "preset_id": candidate.get("preset_id"),
                    "preset_hash": candidate.get("preset_hash"),
                    "lut_id": lut_id,
                    "lut_hash": candidate.get("lut_hash"),
                    "preset_uuid": entry.get("uuid"),
                    "label": candidate.get("name") or entry.get("name"),
                    "source": entry.get("source"),
                    "category": candidate.get("category") or entry.get("category"),
                    "preset_scope": entry.get("preset_scope") or "catalog",
                    "look_kind": look_kind,
                    "profile_name": candidate.get("profile_name")
                    or entry.get("profile_name"),
                    "profile_hash": candidate.get("profile_hash")
                    or (
                        entry.get("file_hash")
                        if look_kind == "lightroom_profile"
                        else None
                    ),
                    "xmp_compatible": False
                    if lut_id
                    else bool(
                        candidate.get(
                            "xmp_compatible", entry.get("xmp_compatible", True)
                        )
                    ),
                    "amount_supported": True
                    if lut_id
                    else runtime_supports_amount(entry),
                    "amount_note": (
                        None
                        if lut_id or runtime_supports_amount(entry)
                        else "此预设由插件托管，Lightroom 仅支持 100%；可换原生预设调强度"
                        if str(entry.get("preset_scope") or "catalog") == "plugin"
                        else "此预设未启用 Lightroom 强度调整"
                    ),
                    "score": None,
                    "render_status": "pending",
                }
            )
        representative_path = str(
            (group.get("probes") or {}).get("representative") or ""
        )
        representative = next(
            (
                item
                for item in plan_items
                if str(item.get("group_id")) == group_id
                and representative_path
                in {str(item.get("path")), str(item.get("preview_path"))}
            ),
            next(
                (item for item in plan_items if str(item.get("group_id")) == group_id),
                {},
            ),
        )
        output[group_id] = {
            "group_id": int(group_id),
            "status": "pending",
            "preset_id": None,
            "preset_hash": None,
            "amount": 100,
            "amount_supported": False,
            "recommended_preset_id": None,
            "top3": candidates,
            "confidence": 0.0,
            "reason": "等待 Lightroom 真实预览与质量复评",
            "missing_stages": group.get("missing_stages") or [],
            "representative_index": representative.get("index"),
            "render_tasks": group.get("render_tasks") or [],
            "manual_override": False,
        }
    return output


def _export_output_dir(value: str | None, input_root: Path) -> Path:
    if not value or not value.strip():
        return (input_root / "成片").resolve()
    requested = Path(value).expanduser()
    if not requested.is_absolute():
        raise HTTPException(422, "JPEG 输出目录必须是绝对路径。")
    try:
        output = requested.resolve()
    except OSError as exc:
        raise HTTPException(422, f"JPEG 输出目录无效：{requested}") from exc
    if not _inside(output, input_root):
        raise HTTPException(422, f"JPEG 输出目录必须位于照片目录内：{input_root}")
    if output.exists() and not output.is_dir():
        raise HTTPException(422, f"JPEG 输出位置不是文件夹：{output}")
    return output


def _enrich_frozen_style_recipe(
    recipe: dict[str, Any],
    catalog_by_id: dict[str, dict[str, Any]],
    lut_engine: CreativeLutEngine | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    creative = recipe.get("creative_style")
    if not isinstance(creative, dict) or creative.get("status") == "skipped":
        return recipe
    if creative.get("status") != "confirmed":
        raise HTTPException(409, "部分照片组的 AI 风格尚未确认。")
    lut_id = str(creative.get("lut_id") or "").strip()
    look_kind = str(creative.get("look_kind") or "").strip().casefold()
    if lut_id or look_kind in {"cube_lut", "rendered_lut"}:
        if not lut_id or lut_engine is None:
            raise HTTPException(409, "已确认的 .cube 外观缺少可验证 LUT 资源。")
        try:
            descriptor = lut_engine.get_lut(lut_id)
        except (CreativeLutError, OSError) as exc:
            raise HTTPException(
                409, f"已确认的 LUT 不在当前资源库中：{lut_id}"
            ) from exc
        expected_hash = str(creative.get("lut_hash") or "")
        if not expected_hash or expected_hash != descriptor.lut_hash:
            raise HTTPException(409, "LUT 版本已经变化，请重新确认该组风格。")
        amount_value = creative.get("strength", creative.get("amount", 100))
        if (
            isinstance(amount_value, bool)
            or not isinstance(amount_value, (int, float))
            or int(amount_value) != amount_value
            or not 0 <= int(amount_value) <= 200
        ):
            raise HTTPException(409, "LUT 强度必须是 0 到 200 的整数。")
        frozen = dict(recipe)
        frozen["creative_style"] = {
            **creative,
            "preset_id": None,
            "preset_hash": None,
            "preset_uuid": None,
            "preset_scope": None,
            "look_kind": "rendered_lut",
            "lut_id": descriptor.lut_id,
            "lut_hash": descriptor.lut_hash,
            "amount": int(amount_value),
            "strength": int(amount_value),
            "amount_supported": True,
            "xmp_compatible": False,
        }
        return frozen
    preset_id = str(creative.get("preset_id") or "")
    if not preset_id:
        raise HTTPException(409, "部分照片组的 AI 风格尚未确认。")
    entry = catalog_by_id.get(preset_id)
    if not entry:
        raise HTTPException(409, f"已确认的风格预设不在当前资源库中：{preset_id}")
    expected_hash = creative.get("preset_hash")
    current_hash = entry.get("file_hash")
    if expected_hash and current_hash and str(expected_hash) != str(current_hash):
        raise HTTPException(409, "风格预设版本已经变化，请重新确认该组风格。")
    registration_status = str(entry.get("registration_status") or "")
    look_kind = str(
        creative.get("look_kind") or entry.get("look_kind") or "develop_preset"
    )
    allowed_statuses = (
        {"installed", "registered", "local_copy"}
        if look_kind == "lightroom_profile"
        else {"installed", "registered"}
    )
    if registration_status not in allowed_statuses:
        raise HTTPException(409, f"风格预设尚未由 Lightroom 确认可用：{preset_id}")
    if look_kind == "lightroom_profile":
        if data_dir is None:
            raise HTTPException(409, "冻结 Creative Look 时缺少应用数据目录。")
        profile_name = str(
            creative.get("profile_name") or entry.get("profile_name") or ""
        ).strip()
        if not profile_name:
            raise HTTPException(
                409, f"创意外观缺少 Lightroom Profile 名称：{preset_id}"
            )
        descriptor = entry.get("look_descriptor")
        source_hash = str(entry.get("look_descriptor_hash") or "").strip().lower()
        look_uuid = str(entry.get("uuid") or "").strip()
        if not isinstance(descriptor, dict) or not source_hash or not look_uuid:
            raise HTTPException(409, f"创意外观缺少安全 Look 描述：{preset_id}")
        if (
            not STYLE_PREVIEW_KEY_RE.fullmatch(source_hash)
            or str(descriptor.get("Hash") or "").strip().lower() != source_hash
            or str(descriptor.get("UUID") or "").strip() != look_uuid
        ):
            raise HTTPException(409, f"创意外观 Look 描述校验失败：{preset_id}")
        selected_descriptor_hash = (
            str(creative.get("look_descriptor_hash") or "").strip().lower()
        )
        if selected_descriptor_hash and selected_descriptor_hash != source_hash:
            raise HTTPException(409, "Creative Look 版本已经变化，请重新确认该组风格。")
        selected_uuid = str(creative.get("look_uuid") or "").strip()
        if selected_uuid and selected_uuid != look_uuid:
            raise HTTPException(409, "Creative Look UUID 已变化，请重新确认该组风格。")
        amount_value = creative.get("amount", 100)
        if (
            isinstance(amount_value, bool)
            or not isinstance(amount_value, (int, float))
            or int(amount_value) != amount_value
            or not 0 <= int(amount_value) <= 200
        ):
            raise HTTPException(409, "Creative Look 强度必须是 0 到 200 的整数。")
        amount = int(amount_value)
        if not runtime_supports_amount(entry) and amount != 100:
            raise HTTPException(409, "此 Creative Look 不支持强度调整，请重新确认。")
        try:
            published = write_lightroom_look_descriptor(
                data_dir,
                descriptor,
                look_uuid=look_uuid,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise HTTPException(409, f"无法冻结 Creative Look：{exc}") from exc
        frozen = dict(recipe)
        frozen["creative_style"] = {
            **creative,
            "preset_id": preset_id,
            "preset_hash": current_hash or expected_hash,
            "look_kind": "lightroom_profile",
            "profile_name": profile_name,
            "profile_hash": current_hash or expected_hash,
            "xmp_compatible": True,
            "preset_uuid": None,
            "preset_scope": None,
            "look_source_hash": source_hash,
            "look_descriptor_path": published["look_descriptor_path"],
            "look_descriptor_hash": published["look_descriptor_hash"],
            "look_uuid": published["look_uuid"],
            "look_amount": amount,
            "amount": amount,
        }
        return frozen
    preset_uuid = creative.get("preset_uuid") or entry.get("runtime_preset_uuid")
    if not preset_uuid:
        raise HTTPException(409, f"风格预设缺少 Lightroom 运行时 UUID：{preset_id}")
    preset_scope = creative.get("preset_scope") or entry.get("preset_scope")
    if preset_scope not in {"catalog", "plugin"}:
        raise HTTPException(409, f"风格预设缺少 Lightroom 运行时作用域：{preset_id}")
    frozen = dict(recipe)
    frozen["creative_style"] = {
        **creative,
        "preset_id": preset_id,
        "preset_hash": current_hash or expected_hash,
        "preset_uuid": str(preset_uuid),
        "preset_scope": str(preset_scope),
    }
    return frozen


def _export_style_catalog_by_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("preset_id")): item
        for item in catalog.get("entries", [])
        if (
            isinstance(item, dict)
            and item.get("preset_id")
            and not item.get("duplicate_of")
            and item.get("registration_status")
            in (
                {"installed", "registered", "local_copy"}
                if item.get("look_kind") == "lightroom_profile"
                else {"installed", "registered"}
            )
        )
    }


def _selected_rendered_luts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    groups = (plan.get("creative_style") or {}).get("groups", {})
    if not isinstance(groups, dict):
        return []
    return [
        group
        for group in groups.values()
        if isinstance(group, dict)
        and str(group.get("status") or "") == "confirmed"
        and (
            group.get("lut_id")
            or str(group.get("look_kind") or "").casefold()
            in {"cube_lut", "rendered_lut"}
        )
    ]


def _ensure_export_style_targets(plan: dict[str, Any], *, xmp: bool) -> None:
    if xmp and _selected_rendered_luts(plan):
        raise HTTPException(
            422,
            "任意 .cube LUT 不能写入普通 Lightroom XMP；"
            "请仅导出 JPEG，或改选 Lightroom Creative Profile。",
        )


def _freeze_export_items(
    payload: dict[str, Any],
    plan: dict[str, Any],
    data_dir: Path,
    lut_engine: CreativeLutEngine | None = None,
) -> list[dict[str, Any]]:
    merged = merge_confirmed_develop(
        payload,
        plan,
        int(payload.get("review_revision", 0)),
    )
    color_mode = str(plan.get("color_mode") or "pending")
    catalog_by_id: dict[str, dict[str, Any]] = {}
    if color_mode == "style":
        try:
            catalog = _configured_style_catalog(data_dir)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(409, f"无法冻结风格预设：{exc}") from exc
        catalog_by_id = _export_style_catalog_by_id(catalog)
    frozen: list[dict[str, Any]] = []
    for index, item in enumerate(merged.get("results", [])):
        if item.get("excluded") or int(item.get("rating", 0)) < 3:
            continue
        recipe = item.get("develop")
        if not isinstance(recipe, dict) or recipe.get("confirmed") is not True:
            raise HTTPException(409, "构图方案没有完整冻结，请返回构图步骤重新确认。")
        recipe = _enrich_frozen_style_recipe(
            dict(recipe),
            catalog_by_id,
            lut_engine,
            data_dir,
        )
        source = Path(str(item.get("path") or "")).resolve()
        frozen.append(
            {
                "item_id": str(index),
                "index": index,
                "path": str(source),
                "group_id": str(item.get("group_id") or ""),
                "rating": int(item.get("rating") or 0),
                "score": float(item.get("score") or 0.0),
                "keywords": list(item.get("keywords") or []),
                "develop": recipe,
                "source_fingerprint": quick_fingerprint(source),
            }
        )
    return frozen


def _verify_export_sources(spec: dict[str, Any]) -> None:
    for item in spec.get("items", []):
        states = (item.get("targets") or {}).values()
        if not any(state.get("status") in {"pending", "failed"} for state in states):
            continue
        source = Path(str(item.get("path") or ""))
        expected = item.get("source_fingerprint")
        try:
            current = quick_fingerprint(source)
        except OSError as exc:
            raise HTTPException(409, f"导出源照片已不可用：{source}") from exc
        if not isinstance(expected, dict) or current != expected:
            raise HTTPException(
                409, f"照片在准备导出后发生了变化，请重新准备：{source.name}"
            )


def _export_execution(spec: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    execution = next(
        (
            item
            for item in spec.get("executions", [])
            if str(item.get("attempt_id")) == str(attempt_id)
        ),
        None,
    )
    if not isinstance(execution, dict):
        raise KeyError(attempt_id)
    return execution


def _export_result_error(row: dict[str, Any], fallback: str) -> str:
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    return str(
        result.get("error") or result.get("message") or row.get("message") or fallback
    )


def _fail_export_execution(
    data_dir: Path,
    spec: dict[str, Any],
    execution: dict[str, Any],
    message: str,
) -> None:
    for work in execution.get("work", []):
        record_export_result(
            data_dir,
            spec,
            item_id=str(work["item_id"]),
            target=str(work["target"]),
            succeeded=False,
            error=message,
        )
    finish_export_attempt(
        data_dir,
        spec,
        attempt_id=str(execution["attempt_id"]),
        status="failed",
        error=message,
    )


def _rendered_lut_recipe(item: dict[str, Any]) -> dict[str, Any] | None:
    recipe = item.get("recipe") if isinstance(item.get("recipe"), dict) else {}
    creative = (
        recipe.get("creative_style")
        if isinstance(recipe.get("creative_style"), dict)
        else {}
    )
    look_kind = str(creative.get("look_kind") or "").casefold()
    if creative.get("lut_id") and look_kind in {"cube_lut", "rendered_lut"}:
        return creative
    return None


def _prepare_lut_export_task(
    spec: dict[str, Any],
    execution: dict[str, Any],
    batch: dict[str, Any],
) -> tuple[Path | None, list[str], list[dict[str, Any]]]:
    task_by_path = {
        os.path.normcase(str(Path(str(row.get("photo_path") or "")).resolve())): row
        for row in batch.get("tasks", [])
        if isinstance(row, dict) and row.get("photo_path")
    }
    item_by_id = {str(item.get("item_id")): item for item in spec.get("items", [])}
    task_items: list[dict[str, Any]] = []
    expected_ids: list[str] = []
    failures: list[dict[str, Any]] = []
    for work in execution.get("work", []):
        if str(work.get("target")) != "jpeg":
            continue
        item_id = str(work.get("item_id") or "")
        item = item_by_id.get(item_id, {})
        creative = _rendered_lut_recipe(item)
        if creative is None:
            continue
        expected_ids.append(item_id)
        key = os.path.normcase(str(Path(str(item.get("path") or "")).resolve()))
        row = task_by_path.get(key)
        result = (
            row.get("result")
            if isinstance(row, dict) and isinstance(row.get("result"), dict)
            else {}
        )
        jpeg_path = Path(str(result.get("jpeg_path") or "")).expanduser().resolve()
        try:
            if str(result.get("jpeg_status") or "") != "done":
                raise ValueError(
                    _export_result_error(row or {}, "Lightroom JPEG 导出失败。")
                )
            if (
                jpeg_path.suffix.casefold() not in {".jpg", ".jpeg"}
                or not jpeg_path.is_file()
            ):
                raise ValueError("Lightroom 没有返回可处理的基础 JPEG。")
            task_items.append(
                {
                    "item_id": item_id,
                    "jpeg_path": str(jpeg_path),
                    "lut_id": str(creative.get("lut_id") or ""),
                    "lut_hash": str(creative.get("lut_hash") or ""),
                    "strength": int(
                        creative.get("strength", creative.get("amount", 100))
                    ),
                    "input_fingerprint": full_fingerprint(jpeg_path),
                }
            )
        except (OSError, TypeError, ValueError) as exc:
            failures.append({"item_id": item_id, "succeeded": False, "error": str(exc)})
    if not expected_ids:
        return None, [], []
    if not task_items:
        return None, expected_ids, failures
    task_path = (
        Path(str(execution["spec_path"])).resolve().parent / "lut-export-task.json"
    )
    write_json(
        task_path,
        {
            "protocol": LUT_EXPORT_PROTOCOL,
            "export_spec_id": str(spec.get("export_spec_id") or ""),
            "attempt_id": str(execution.get("attempt_id") or ""),
            "output_root": str(spec.get("output_dir") or ""),
            "jpeg_quality": int((spec.get("jpeg_settings") or {}).get("quality", 90)),
            "items": task_items,
        },
    )
    return task_path, expected_ids, failures


def _reconcile_lightroom_export(
    data_dir: Path,
    spec: dict[str, Any],
    execution: dict[str, Any],
    batch: dict[str, Any],
    fallback_error: str,
    lut_results: dict[str, Any] | None = None,
) -> None:
    task_by_path = {
        os.path.normcase(str(Path(str(row.get("photo_path") or "")).resolve())): row
        for row in batch.get("tasks", [])
        if isinstance(row, dict) and row.get("photo_path")
    }
    item_by_id = {str(item.get("item_id")): item for item in spec.get("items", [])}
    trusted_lut_results = (
        lut_results
        if isinstance(lut_results, dict)
        and lut_results.get("protocol") == LUT_EXPORT_PROTOCOL
        and str(lut_results.get("attempt_id") or "")
        == str(execution.get("attempt_id") or "")
        else {}
    )
    lut_by_item = {
        str(row.get("item_id")): row
        for row in trusted_lut_results.get("items", [])
        if isinstance(row, dict) and row.get("item_id") is not None
    }
    succeeded = True
    for work in execution.get("work", []):
        item_id = str(work["item_id"])
        target = str(work["target"])
        item = item_by_id.get(item_id, {})
        key = os.path.normcase(str(Path(str(item.get("path") or "")).resolve()))
        row = task_by_path.get(key)
        result = (
            row.get("result")
            if isinstance(row, dict) and isinstance(row.get("result"), dict)
            else {}
        )
        target_status = str(result.get(f"{target}_status") or "missing")
        target_succeeded = target_status == "done"
        target_error: str | None = None
        if target == "jpeg" and _rendered_lut_recipe(item) is not None:
            lut_row = lut_by_item.get(item_id, {})
            target_succeeded = bool(lut_row.get("succeeded"))
            if not target_succeeded:
                target_error = (
                    f"创意 LUT 渲染失败：{lut_row.get('error') or fallback_error}"
                )
        succeeded = succeeded and target_succeeded
        record_export_result(
            data_dir,
            spec,
            item_id=item_id,
            target=target,
            succeeded=target_succeeded,
            output=(
                str(
                    (lut_by_item.get(item_id) or {}).get("output")
                    or result.get(f"{target}_path")
                )
                if result.get(f"{target}_path")
                else None
            ),
            error=(
                None
                if target_succeeded
                else target_error or _export_result_error(row or {}, fallback_error)
            ),
        )
    finish_export_attempt(
        data_dir,
        spec,
        attempt_id=str(execution["attempt_id"]),
        status="completed" if succeeded else "failed",
        error=None if succeeded else fallback_error,
    )


def _reconcile_direct_xmp_export(
    data_dir: Path,
    spec: dict[str, Any],
    execution: dict[str, Any],
    fallback_error: str,
) -> None:
    results_path = Path(str(execution.get("results_path") or ""))
    manifests = list(results_path.parent.glob("xmp-commit-manifest-*.json"))
    manifest = (
        read_json(max(manifests, key=lambda path: path.stat().st_mtime_ns))
        if manifests
        else {}
    )
    by_path = {
        os.path.normcase(str(Path(str(record.get("raw_path") or "")).resolve())): record
        for record in manifest.get("records", [])
        if isinstance(record, dict) and record.get("raw_path")
    }
    item_by_id = {str(item.get("item_id")): item for item in spec.get("items", [])}
    succeeded = True
    for work in execution.get("work", []):
        item_id = str(work["item_id"])
        item = item_by_id.get(item_id, {})
        key = os.path.normcase(str(Path(str(item.get("path") or "")).resolve()))
        record = by_path.get(key, {})
        status = str(record.get("status") or "missing")
        target_succeeded = status == "created"
        succeeded = succeeded and target_succeeded
        error = None
        if not target_succeeded:
            error = str(record.get("error") or fallback_error)
            if status.startswith("skipped_existing"):
                error = (
                    "执行期间出现了已有 XMP；为避免覆盖，已留待 Lightroom 安全重试。"
                )
        record_export_result(
            data_dir,
            spec,
            item_id=item_id,
            target="xmp",
            succeeded=target_succeeded,
            output=(str(record.get("xmp_path")) if record.get("xmp_path") else None),
            error=error,
        )
    finish_export_attempt(
        data_dir,
        spec,
        attempt_id=str(execution["attempt_id"]),
        status="completed" if succeeded else "failed",
        error=None if succeeded else fallback_error,
    )


def _reconcile_export_spec(
    data_dir: Path,
    jobs: JobManager,
    spec: dict[str, Any],
    lut_engine: CreativeLutEngine | None = None,
) -> dict[str, Any]:
    for execution in spec.get("executions", []):
        if execution.get("status") not in {"queued", "running", "cancelling"}:
            continue
        job_id = execution.get("job_id")
        if not job_id:
            continue
        try:
            job = jobs.get(str(job_id))
        except KeyError:
            continue
        job_status = str(job.get("status") or "")
        if job_status in {"queued", "running", "cancelling"}:
            if execution.get("status") != job_status:
                execution["status"] = job_status
                execution["updated_at"] = _now()
                write_json(
                    export_spec_path(data_dir, str(spec["export_spec_id"])), spec
                )
            continue
        fallback = str(job.get("message") or "导出任务未完成。")
        if execution.get("engine") == "direct_xmp":
            _reconcile_direct_xmp_export(data_dir, spec, execution, fallback)
            continue
        batch: dict[str, Any] | None = None
        batch_id = execution.get("batch_id")
        if batch_id:
            try:
                batch = read_lightroom_batch_status(data_dir, str(batch_id))
            except (OSError, ValueError, TypeError, KeyError):
                batch = None
        if batch and batch.get("status") in {
            "complete",
            "failed",
            "cancelled",
            "incomplete",
        }:
            lut_results: dict[str, Any] | None = None
            lut_state = execution.get("lut_postprocess")
            if not isinstance(lut_state, dict):
                task_path, expected_ids, preflight_failures = _prepare_lut_export_task(
                    spec,
                    execution,
                    batch,
                )
                if expected_ids:
                    lut_state = {
                        "status": "prepared",
                        "job_id": None,
                        "task_path": str(task_path) if task_path else None,
                        "expected_item_ids": expected_ids,
                        "preflight_failures": preflight_failures,
                    }
                    if task_path is not None:
                        try:
                            job = jobs.start(
                                "lut_export",
                                [
                                    "render-export-luts",
                                    "--task",
                                    str(task_path),
                                    "--project-root",
                                    str(jobs.project_root),
                                ],
                                {
                                    "title": f"渲染创意 LUT · {len(expected_ids)} 张",
                                    "export_spec_id": spec.get("export_spec_id"),
                                    "attempt_id": execution.get("attempt_id"),
                                    "task_path": str(task_path),
                                },
                            )
                            lut_state.update(status="queued", job_id=str(job["id"]))
                        except RuntimeError as exc:
                            lut_state.update(status="failed", error=str(exc))
                    else:
                        lut_state.update(
                            status="failed", error="没有可渲染的基础 JPEG。"
                        )
                    execution["lut_postprocess"] = lut_state
                    execution["status"] = str(lut_state["status"])
                    execution["updated_at"] = _now()
                    write_json(
                        export_spec_path(data_dir, str(spec["export_spec_id"])), spec
                    )
                    if lut_state["status"] == "queued":
                        continue
            if isinstance(lut_state, dict):
                worker_rows = list(lut_state.get("preflight_failures") or [])
                lut_job_id = lut_state.get("job_id")
                if lut_job_id:
                    try:
                        lut_job = jobs.get(str(lut_job_id))
                    except KeyError:
                        lut_job = {
                            "status": "failed",
                            "message": "创意 LUT Worker 任务丢失。",
                        }
                    lut_job_status = str(lut_job.get("status") or "")
                    if lut_job_status in {"queued", "running", "cancelling"}:
                        if execution.get("status") != lut_job_status:
                            execution["status"] = lut_job_status
                            execution["updated_at"] = _now()
                            write_json(
                                export_spec_path(data_dir, str(spec["export_spec_id"])),
                                spec,
                            )
                        continue
                    result = lut_job.get("result")
                    if (
                        isinstance(result, dict)
                        and result.get("protocol") == LUT_EXPORT_PROTOCOL
                    ):
                        worker_rows.extend(
                            row
                            for row in result.get("items", [])
                            if isinstance(row, dict)
                        )
                    else:
                        known = {str(row.get("item_id")) for row in worker_rows}
                        error = str(
                            lut_job.get("message")
                            or lut_state.get("error")
                            or "创意 LUT Worker 未返回完整结果。"
                        )
                        worker_rows.extend(
                            {
                                "item_id": item_id,
                                "succeeded": False,
                                "error": error,
                            }
                            for item_id in lut_state.get("expected_item_ids", [])
                            if str(item_id) not in known
                        )
                elif lut_state.get("status") == "failed":
                    known = {str(row.get("item_id")) for row in worker_rows}
                    worker_rows.extend(
                        {
                            "item_id": item_id,
                            "succeeded": False,
                            "error": str(
                                lut_state.get("error") or "创意 LUT Worker 无法启动。"
                            ),
                        }
                        for item_id in lut_state.get("expected_item_ids", [])
                        if str(item_id) not in known
                    )
                lut_results = {
                    "protocol": LUT_EXPORT_PROTOCOL,
                    "attempt_id": execution.get("attempt_id"),
                    "items": worker_rows,
                }
            _reconcile_lightroom_export(
                data_dir,
                spec,
                execution,
                batch,
                fallback,
                lut_results,
            )
        else:
            _fail_export_execution(data_dir, spec, execution, fallback)
    return spec


def _public_export_spec(spec: dict[str, Any]) -> dict[str, Any]:
    summary = export_summary(spec)
    public_status = {
        "complete": "completed",
        "partial_failure": "partial",
    }.get(str(spec.get("status")), str(spec.get("status") or "prepared"))
    latest = spec.get("executions", [])[-1] if spec.get("executions") else {}
    errors = [
        state.get("error")
        for item in spec.get("items", [])
        for state in (item.get("targets") or {}).values()
        if state.get("status") == "failed" and state.get("error")
    ]
    return {
        **spec,
        "status": public_status,
        "results": summary.get("targets", {}),
        "job_id": latest.get("job_id"),
        "stage_label": latest.get("status"),
        "message": errors[0] if errors else None,
    }


def create_app(
    data_dir: Path | None = None,
    project_root: Path | None = None,
    pending_root: Path | None = None,
    content_root: Path | None = None,
    bootstrap_storage: bool = False,
) -> FastAPI:
    data_dir = (data_dir or runtime_data_dir()).resolve()
    project_root = (project_root or PROJECT_ROOT).resolve()
    if pending_root is not None:
        pending_root = pending_root.resolve()
    elif DEFAULT_PENDING:
        pending_root = Path(DEFAULT_PENDING).resolve()
    content_layout = (
        resolve_content_root(content_root, apply_environment=True)
        if content_root is not None
        else None
    )
    model_root = (
        content_layout.models
        if content_layout is not None
        else project_root / ".runtime"
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    lut_engine = (
        CreativeLutEngine.for_content_root(content_layout)
        if content_layout is not None
        else CreativeLutEngine.for_project(project_root)
    )
    token = secrets.token_urlsafe(32)
    jobs = JobManager(data_dir, project_root, content_layout)
    review_lock = threading.RLock()
    develop_lock = threading.RLock()
    project_lock = threading.RLock()
    export_lock = threading.RLock()
    model_resource_lock = threading.RLock()
    settings_transfer_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        jobs.shutdown()

    app = FastAPI(
        title="光影拣选台",
        version=PRODUCT_VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.data_dir = data_dir
    app.state.content_root = content_layout.root if content_layout else None
    app.state.bootstrap_storage = bool(bootstrap_storage)
    app.state.creative_lut_engine = lut_engine
    app.state.jobs = jobs
    app.state.token = token
    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")

    def mutate_token(x_photo_ai_token: str | None = Header(None)) -> None:
        if not x_photo_ai_token or not secrets.compare_digest(x_photo_ai_token, token):
            raise HTTPException(403, "页面操作令牌已失效，请刷新。")

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Response:
        if bootstrap_storage and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            response: Response = JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "content_root_required",
                        "message": "请先选择正式数据目录；模型、缓存和任务数据不会写入临时启动目录。",
                    }
                },
            )
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(TEMPLATE_ROOT / "index.html")

    def current_settings_transfer(*, include_active_profile: bool) -> dict[str, Any]:
        active_profile: str | None = None
        if include_active_profile:
            try:
                resources = (
                    ai_resources_status(content_layout)
                    if content_layout is not None
                    else model_resources_status(model_root, data_dir)
                )
                configured = str(
                    (resources.get("settings") or {}).get("active_profile") or ""
                )
                if configured in {"8gb", "16gb"}:
                    active_profile = configured
            except (OSError, ValueError, TypeError):
                active_profile = None
        return build_settings_export(
            data_dir,
            style_settings=_style_library_settings(data_dir),
            active_profile=active_profile,
        )

    def settings_environment_check() -> dict[str, bool]:
        try:
            gpu_detected = bool(_gpu().get("available"))
        except (OSError, ValueError, TypeError):
            gpu_detected = False
        try:
            lightroom = _lightroom_status(data_dir, project_root)
            lightroom_detected = bool(
                (lightroom.get("lightroom") or {}).get("compatible")
            )
            lightroom_connected = (lightroom.get("heartbeat") or {}).get(
                "state"
            ) == "online"
        except (OSError, ValueError, TypeError):
            lightroom_detected = False
            lightroom_connected = False
        return {
            "gpu_detected": gpu_detected,
            "lightroom_detected": lightroom_detected,
            "lightroom_connected": lightroom_connected,
        }

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        transactions, _ = _transactions(data_dir)
        toolbox_transactions, _ = list_raw_jpeg_transactions(data_dir)
        xmp_cleanup_transactions, _ = list_xmp_cleanup_transactions(data_dir)
        content_free = None
        try:
            content_free = round(shutil.disk_usage(data_dir.anchor).free / 1024**3, 1)
        except OSError:
            pass
        try:
            lightroom_bridge = _lightroom_status(data_dir, project_root)
        except (OSError, ValueError, TypeError) as exc:
            lightroom_bridge = {"error": str(exc)}
        try:
            preferences = current_settings_transfer(include_active_profile=False)
        except (OSError, SettingsTransferError, TypeError, ValueError):
            preferences = None
        return {
            "token": token,
            "defaults": {
                "pending": str(pending_root) if pending_root is not None else "",
                "retain_ratio": 0.30,
                "mode": "deep",
            },
            "projects": _projects(data_dir),
            "runs": _runs(data_dir),
            "jobs": jobs.list(),
            "transactions": transactions,
            "toolbox_transactions": toolbox_transactions,
            "xmp_cleanup_transactions": xmp_cleanup_transactions,
            "system": {
                "data_dir": str(data_dir),
                "content_root": (
                    str(content_layout.root) if content_layout is not None else None
                ),
                "content_root_configured": not bootstrap_storage,
                "content_free_gib": content_free,
                # Kept during the 0.9 UI transition; no drive-letter meaning.
                "e_free_gib": content_free,
                "photos_online": (
                    pending_root.is_dir() if pending_root is not None else None
                ),
                # Legacy API field retained for older frontends.  New code
                # uses the nullable, drive-neutral ``photos_online`` value.
                "x_online": bool(pending_root and pending_root.is_dir()),
                "lightroom": _lightroom_state(),
                "lightroom_bridge": lightroom_bridge,
                "exiftool": Path(os.environ.get("PHOTO_AI_EXIFTOOL", "")).is_file(),
                "gpu": _gpu(),
            },
            "preferences": preferences,
        }

    @app.get("/api/settings-transfer")
    def settings_transfer() -> dict[str, Any]:
        try:
            return current_settings_transfer(include_active_profile=True)
        except (OSError, SettingsTransferError, TypeError, ValueError) as exc:
            raise HTTPException(409, f"无法读取可迁移设置：{exc}") from exc

    @app.get("/api/settings-transfer/export")
    def export_settings_transfer() -> Response:
        try:
            payload = current_settings_transfer(include_active_profile=True)
            content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        except (OSError, SettingsTransferError, TypeError, ValueError) as exc:
            raise HTTPException(409, f"无法导出设置：{exc}") from exc
        return Response(
            content=content.encode("utf-8"),
            media_type=SETTINGS_TRANSFER_MEDIA_TYPE,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{SETTINGS_TRANSFER_FILENAME}"'
                )
            },
        )

    @app.post(
        "/api/settings-transfer/import",
        dependencies=[Depends(mutate_token)],
    )
    def import_settings_transfer(body: SettingsImportBody) -> dict[str, Any]:
        with settings_transfer_lock:
            current: dict[str, Any] | None = None
            previous_style: dict[str, Any] | None = None
            try:
                current = current_settings_transfer(include_active_profile=False)
                imported, ignored = normalize_settings_payload(
                    body.settings, current=current
                )
                previous_style = _style_library_settings(data_dir)
                write_local_preferences(data_dir, imported)
                _write_style_library_settings(data_dir, dict(imported["style_sources"]))
                sync_style_library(data_dir)
            except SettingsTransferError as exc:
                raise HTTPException(422, str(exc)) from exc
            except (CreativeLutError, OSError, TypeError, ValueError) as exc:
                try:
                    if current is not None:
                        write_local_preferences(data_dir, current)
                    if previous_style is not None:
                        _write_style_library_settings(data_dir, previous_style)
                        sync_style_library(data_dir)
                except (
                    CreativeLutError,
                    OSError,
                    SettingsTransferError,
                    TypeError,
                    ValueError,
                ):
                    pass
                raise HTTPException(409, f"设置导入未能安全完成：{exc}") from exc
        checks = settings_environment_check()
        return {
            "imported": True,
            "settings": imported,
            "ignored_fields": ignored,
            "checks": checks,
            "model_download_started": False,
            "lightroom_configuration_changed": False,
        }

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            return jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在。") from exc

    @app.delete("/api/jobs/{job_id}", dependencies=[Depends(mutate_token)])
    def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            return jobs.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在。") from exc
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise HTTPException(409, f"无法安全取消任务：{exc}") from exc

    def require_model_profile_ready() -> dict[str, Any]:
        try:
            if content_layout is not None:
                resources = ai_resources_status(content_layout)
                settings = resources.get("settings", {})
                profile_id = settings.get("active_profile")
                profile = next(
                    (
                        item
                        for item in resources.get("profiles", [])
                        if item.get("id") == profile_id
                    ),
                    None,
                )
                readiness = {
                    "ready": bool(profile and profile.get("ready")),
                    "active_profile": profile_id,
                    "message": (
                        f"{profile.get('label')} 计算环境校验通过。"
                        if profile and profile.get("ready")
                        else "请先完整安装并通过 8GB 或 16GB AI 环境自检。"
                    ),
                }
            else:
                readiness = active_model_profile_readiness(model_root, data_dir)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(
                409,
                {
                    "code": "model_profile_incomplete",
                    "message": f"无法校验 AI 模型资源：{exc}",
                },
            ) from exc
        if not readiness.get("ready"):
            raise HTTPException(
                409,
                {
                    "code": "model_profile_incomplete",
                    "message": str(
                        readiness.get("message") or "请先完整安装并启用一套 AI 模型。"
                    ),
                },
            )
        return readiness

    def start_job(
        kind: str, args: list[str], context: dict[str, Any]
    ) -> dict[str, Any]:
        if kind in {"group", "score", "develop", "style_recommend", "style_preview"}:
            require_model_profile_ready()
        try:
            return jobs.start(kind, args, context)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(
        "/api/jobs/{job_id}/retry",
        dependencies=[Depends(mutate_token)],
    )
    def retry_job(job_id: str) -> dict[str, Any]:
        try:
            previous = jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在。") from exc
        kind = str(previous.get("kind") or "")
        context = previous.get("context") or {}
        partial_style = (
            kind in {"style_recommend", "style_preview"}
            and previous.get("status") == "completed"
            and (previous.get("result") or {}).get("style_status") == "partial"
        )
        if previous.get("status") not in {"failed", "interrupted"} and not partial_style:
            raise HTTPException(409, "只有失败或意外中断的任务可以重试。")
        if kind in {"group", "score", "style_recommend", "style_preview"}:
            require_model_profile_ready()
        if kind in {
            "xmp_commit",
            "rollback",
            "raw_jpeg_execute",
            "raw_jpeg_rollback",
            "xmp_cleanup_rollback",
        } and _lightroom_state() != "closed":
            raise HTTPException(409, "重试此文件操作前请先关闭 Lightroom。")
        try:
            # Rebuild mutable workflow inputs through the normal validated
            # routes. Replaying a stale revision/snapshot just fails again (or
            # can undo a user's subsequent grouping edits).
            if kind == "score" and context.get("source_run_id"):
                run_id = str(context["source_run_id"])
                revision = int(_review(_run_file(data_dir, run_id))["revision"])
                return score_run(run_id, RunScoreBody(
                    base_revision=revision, mode=context.get("mode")
                ))
            if kind in {"style_recommend", "style_preview"}:
                run_id = str(context.get("run_id") or "")
                run_file = _run_file(data_dir, run_id)
                with review_lock, develop_lock:
                    review = _review(run_file)
                    plan = _require_current_develop(run_file, int(review["revision"]))
                    revision = int(plan.get("revision", 0))
                    scope = context.get("scope") or "group"
                    if kind == "style_preview":
                        return rerender_style_preview(run_id, StylePreviewBody(
                            base_revision=revision,
                            scope=scope,
                            group_id=context.get("group_id"),
                            preset_id=context.get("preset_id"),
                            preset_hash=context.get("preset_hash"),
                            lut_id=context.get("lut_id"),
                            lut_hash=context.get("lut_hash"),
                            amount=int(context.get("amount", 100)),
                        ))
                    if scope == "groups":
                        return recommend_styles_for_all_groups(
                            run_id, AllGroupsStyleRecommendationBody(base_revision=revision)
                        )
                    return recommend_styles(run_id, StyleRecommendationBody(
                        base_revision=revision, scope=scope, group_id=context.get("group_id")
                    ))
            return jobs.retry(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在。") from exc
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise HTTPException(409, f"无法重试任务：{exc}") from exc

    @app.get("/api/model-resources")
    def model_resource_list() -> dict[str, Any]:
        try:
            if content_layout is not None:
                return ai_resources_status(content_layout)
            return model_resources_status(model_root, data_dir)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(500, f"无法读取模型资源：{exc}") from exc

    @app.post("/api/model-resources/configure", dependencies=[Depends(mutate_token)])
    def configure_model_resources(
        body: ModelResourcesConfigureBody,
    ) -> dict[str, Any]:
        profile = PROFILE_SPECS[body.profile_id]
        job = start_job(
            "model_download",
            (
                [
                    "ai-runtime-install",
                    "--profile",
                    body.profile_id,
                    "--content-root",
                    str(content_layout.root),
                ]
                if content_layout is not None
                else [
                    "model-resources-configure",
                    "--profile",
                    body.profile_id,
                    "--runtime-root",
                    str(model_root),
                    "--data-dir",
                    str(data_dir),
                ]
            ),
            {
                "title": f"配置 {profile['label']} AI 模型",
                "profile_id": body.profile_id,
                "managed_engine": content_layout is not None,
            },
        )
        return job

    @app.post(
        "/api/model-resources/import-offline",
        dependencies=[Depends(mutate_token)],
    )
    def import_model_resources_offline(
        body: ModelResourcesOfflineImportBody,
    ) -> dict[str, Any]:
        if content_layout is None:
            raise HTTPException(409, "离线资源包只能在桌面安装版中导入。")
        package = Path(body.package_path).expanduser()
        if (
            package.suffix.casefold() != ".photoai-offline"
            or not package.is_absolute()
            or not package.is_file()
        ):
            raise HTTPException(400, "请选择有效的 .photoai-offline 文件。")
        return start_job(
            "model_download",
            [
                "ai-runtime-import",
                "--content-root",
                str(content_layout.root),
                "--package",
                str(package.resolve()),
            ],
            {
                "title": "导入 16GB 离线 AI 环境",
                "profile_id": "16gb",
                "managed_engine": True,
                "offline_import": True,
                "package_name": package.name,
            },
        )

    @app.delete(
        "/api/model-resources/profiles/{profile_id}",
        dependencies=[Depends(mutate_token)],
    )
    def remove_model_profile(profile_id: Literal["8gb", "16gb"]) -> dict[str, Any]:
        if jobs.active():
            raise HTTPException(409, "后台任务运行中，暂不能删除模型。")
        with model_resource_lock:
            try:
                _unload_local_vlm()
                deleted = delete_model_profile(profile_id, model_root, data_dir)
                return (
                    ai_resources_status(content_layout)
                    if content_layout is not None
                    else deleted
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                raise HTTPException(409, f"模型删除失败：{exc}") from exc

    @app.delete(
        "/api/model-resources/{resource_id}",
        dependencies=[Depends(mutate_token)],
    )
    def remove_model_resource(resource_id: str) -> dict[str, Any]:
        if jobs.active():
            raise HTTPException(409, "后台任务运行中，暂不能删除模型。")
        with model_resource_lock:
            try:
                _unload_local_vlm()
                deleted = delete_model_resource(resource_id, model_root, data_dir)
                return (
                    ai_resources_status(content_layout)
                    if content_layout is not None
                    else deleted
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                raise HTTPException(409, f"模型删除失败：{exc}") from exc

    @app.delete(
        "/api/ai-runtime",
        dependencies=[Depends(mutate_token)],
    )
    def remove_ai_runtime() -> dict[str, Any]:
        if content_layout is None:
            raise HTTPException(409, "开发模式不管理独立 AI 环境。")
        if jobs.active():
            raise HTTPException(409, "后台任务运行中，暂不能删除计算环境。")
        from .ai_runtime import delete_ai_runtime

        with model_resource_lock:
            try:
                delete_ai_runtime(content_layout)
                return ai_resources_status(content_layout)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                raise HTTPException(409, f"计算环境删除失败：{exc}") from exc

    @app.get("/api/lightroom/status")
    def lightroom_status() -> dict[str, Any]:
        try:
            return _lightroom_status(data_dir, project_root)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(500, f"无法读取 Lightroom 桥接状态：{exc}") from exc

    @app.post("/api/lightroom/configure", dependencies=[Depends(mutate_token)])
    def configure_lightroom(body: LightroomConfigureBody) -> dict[str, Any]:
        plugin_template = _require_e_drive_lightroom_storage(data_dir, project_root)
        try:
            requested = str(body.executable_path or "").strip()
            detected = detect_lightroom_classic_15_3(
                [Path(requested)] if requested else None
            )
            if detected is None:
                message = (
                    "指定路径中没有找到 Lightroom Classic 14.3 或更高版本，请填写 "
                    "Lightroom.exe 或其安装文件夹。"
                    if requested
                    else "未自动找到 Lightroom Classic 14.3 或更高版本，请手动填写程序路径。"
                )
                raise HTTPException(409, message)
            write_json(
                data_dir / "lightroom" / "settings.json",
                {
                    "schema_version": 1,
                    "executable_path": str(detected),
                    "updated_at": _now(),
                },
            )
            installed = install_lightroom_plugin(plugin_template)
            plugin_dir = Path(str(installed["plugin_dir"]))
            bridge = write_lightroom_plugin_config(data_dir, plugin_dir=plugin_dir)
            result = _lightroom_status(data_dir, project_root)
            result["configuration"] = {
                "plugin_installed": bool(installed.get("installed")),
                "plugin_dir": str(plugin_dir),
                "bridge_root": bridge.get("root"),
                "restart_required": True,
            }
            return result
        except HTTPException:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(409, f"Lightroom 桥接配置失败：{exc}") from exc

    @app.get("/api/style-library")
    def style_library() -> dict[str, Any]:
        try:
            return _public_style_library(data_dir, lut_engine)
        except (CreativeLutError, OSError, ValueError, TypeError) as exc:
            raise HTTPException(500, f"无法读取风格库：{exc}") from exc

    @app.post("/api/style-library/sync", dependencies=[Depends(mutate_token)])
    def sync_styles(body: StyleLibrarySyncBody) -> dict[str, Any]:
        current = _style_library_settings(data_dir)
        _write_style_library_settings(
            data_dir,
            {
                **current,
                "include_lightroom_presets": body.include_lightroom_presets,
                "include_user_uploads": body.include_user_uploads,
            },
        )
        try:
            sync_style_library(data_dir)
            return {"library": _public_style_library(data_dir, lut_engine)}
        except (CreativeLutError, OSError, ValueError, TypeError) as exc:
            raise HTTPException(409, f"风格库同步失败：{exc}") from exc

    @app.patch(
        "/api/style-library/items/{resource_id}",
        dependencies=[Depends(mutate_token)],
    )
    def update_style_library_item(
        resource_id: str, body: StyleLibraryItemVisibilityBody
    ) -> dict[str, Any]:
        library = _public_style_library(data_dir, lut_engine)
        item = next(
            (
                candidate
                for candidate in library.get("presets", [])
                if candidate.get("resource_id") == resource_id
            ),
            None,
        )
        if item is None:
            raise HTTPException(404, "风格资源不存在，请刷新后重试。")
        settings = _style_library_settings(data_dir)
        hidden = set(settings["hidden_resource_ids"])
        if body.hidden:
            hidden.add(resource_id)
        else:
            hidden.discard(resource_id)
        settings["hidden_resource_ids"] = sorted(hidden)
        _write_style_library_settings(data_dir, settings)
        try:
            sync_style_library(data_dir)
            return {"library": _public_style_library(data_dir, lut_engine)}
        except (CreativeLutError, OSError, ValueError, TypeError) as exc:
            raise HTTPException(409, f"风格库索引更新失败：{exc}") from exc

    @app.delete(
        "/api/style-library/items/{resource_id}",
        dependencies=[Depends(mutate_token)],
    )
    def delete_style_library_item(resource_id: str) -> dict[str, Any]:
        library = _public_style_library(data_dir, lut_engine)
        item = next(
            (
                candidate
                for candidate in library.get("presets", [])
                if candidate.get("resource_id") == resource_id
            ),
            None,
        )
        if item is None:
            raise HTTPException(404, "风格资源不存在，请刷新后重试。")
        if not item.get("can_delete"):
            raise HTTPException(409, "Lightroom 或内置资源不能删除，可以改为隐藏。")

        try:
            if item.get("lut_id"):
                lut_engine.delete_lut(
                    str(item["lut_id"]),
                    expected_hash=str(item.get("lut_hash") or ""),
                    archive_root=_style_archive_root(data_dir) / "user-lut",
                )
            else:
                indexed = load_style_index(data_dir)
                entry = next(
                    (
                        candidate
                        for candidate in indexed.get("entries", [])
                        if isinstance(candidate, dict)
                        and (
                            candidate.get("resource_id") or style_resource_id(candidate)
                        )
                        == resource_id
                    ),
                    None,
                )
                if entry is None:
                    raise HTTPException(404, "风格资源不存在，请刷新后重试。")
                if not (
                    entry.get("source_kind") == "managed"
                    and str(entry.get("source_id") or "").casefold() == "user-upload"
                ):
                    raise HTTPException(409, "只允许删除软件托管的用户导入资源。")
                preset_root = (
                    _style_upload_source_root(data_dir) / "presets"
                ).resolve()
                source = Path(str(entry.get("path") or "")).resolve()
                if (
                    not source.is_file()
                    or not source.is_relative_to(preset_root)
                    or source.suffix.casefold() != ".xmp"
                ):
                    raise HTTPException(409, "用户风格文件路径已变化，已拒绝删除。")
                current_hash = hashlib.sha256(source.read_bytes()).hexdigest()
                if current_hash != str(entry.get("file_hash") or ""):
                    raise HTTPException(409, "用户风格文件已变化，请刷新后重试。")
                archive = (
                    _style_archive_root(data_dir)
                    / "user-upload"
                    / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
                )
                archive.mkdir(parents=True, exist_ok=False)
                archived_file = archive / source.name
                os.replace(source, archived_file)
                write_json(
                    archive / "removed.json",
                    {
                        "removed_at": _now(),
                        "resource_id": resource_id,
                        "file_hash": current_hash,
                        "archived_file": archived_file.name,
                        "label": item.get("label"),
                    },
                )
            settings = _style_library_settings(data_dir)
            settings["hidden_resource_ids"] = [
                value
                for value in settings["hidden_resource_ids"]
                if value != resource_id
            ]
            _write_style_library_settings(data_dir, settings)
            sync_style_library(data_dir)
            return {
                "deleted": True,
                "archived": True,
                "library": _public_style_library(data_dir, lut_engine),
            }
        except HTTPException:
            raise
        except (CreativeLutError, OSError, ValueError, TypeError) as exc:
            raise HTTPException(409, f"无法安全删除风格资源：{exc}") from exc

    @app.post("/api/style-library/import", dependencies=[Depends(mutate_token)])
    def import_styles(body: StyleLibraryImportBody) -> dict[str, Any]:
        decoded = _decode_style_imports(body.files)
        source_root = _ensure_style_upload_manifest(data_dir)
        preset_root = source_root / "presets"
        incoming_root = managed_style_root(data_dir) / ".incoming"
        archive_root = _style_archive_root(data_dir) / "user-upload"
        preset_root.mkdir(parents=True, exist_ok=True)
        incoming_root.mkdir(parents=True, exist_ok=True)
        existing_luts = {item.lut_id for item in lut_engine.list_luts()}
        imported = 0
        reused = 0
        changed = False
        xmp_changed = False
        failures: list[dict[str, str]] = []
        source_metadata = {
            "source": "用户批量导入",
            "source_id": "user-upload",
            "license": "user-provided",
            "source_url": None,
            "source_tier": "curated_candidate",
            "experimental": False,
        }
        for name, suffix, content in decoded:
            temporary = incoming_root / f"{uuid.uuid4().hex}{suffix}"
            try:
                temporary.write_bytes(content)
                if suffix == ".cube":
                    descriptor = lut_engine.import_lut(
                        temporary,
                        name=Path(name).stem,
                        source_label="用户批量导入",
                        source_license="user-provided",
                    )
                    if descriptor.lut_id in existing_luts:
                        reused += 1
                    else:
                        existing_luts.add(descriptor.lut_id)
                        imported += 1
                        changed = True
                    continue

                inspected = inspect_xmp_preset(
                    temporary,
                    source_kind="managed",
                    source_metadata=source_metadata,
                    source_root=incoming_root,
                )
                if inspected.get("compatibility") == "invalid":
                    reasons = inspected.get("compatibility_reasons") or []
                    raise ValueError("；".join(map(str, reasons)) or "XMP 无法解析")
                if str(inspected.get("preset_type") or "").casefold() not in {
                    "normal",
                    "look",
                }:
                    raise ValueError("不是 Lightroom 调整预设或创意外观")
                identity = hashlib.sha256(
                    str(inspected["preset_id"]).encode("utf-8")
                ).hexdigest()[:32]
                destination = preset_root / f"{identity}.xmp"
                incoming_hash = str(inspected["file_hash"])
                if destination.is_file():
                    current_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
                    if current_hash == incoming_hash:
                        reused += 1
                        continue
                    archive_root.mkdir(parents=True, exist_ok=True)
                    archive = archive_root / f"{current_hash}.xmp"
                    if not archive.exists():
                        shutil.copy2(destination, archive)
                os.replace(temporary, destination)
                imported += 1
                changed = True
                xmp_changed = True
            except (CreativeLutError, OSError, ValueError, TypeError) as exc:
                failures.append({"name": name, "message": str(exc)})
            finally:
                temporary.unlink(missing_ok=True)

        try:
            registration_pending = False
            if changed:
                indexed = sync_style_library(data_dir)
                expected_registry_hash = str(
                    (indexed.get("managed_registry") or {}).get("registry_hash") or ""
                )
                awaiting = int(
                    (indexed.get("summary") or {}).get("awaiting_registration") or 0
                )
                heartbeat = data_dir / "lightroom-bridge" / "heartbeat.line"
                heartbeat_fresh = bool(
                    heartbeat.is_file()
                    and time.time() - heartbeat.stat().st_mtime <= 15
                )
                if (
                    xmp_changed
                    and awaiting
                    and heartbeat_fresh
                    and expected_registry_hash
                ):
                    deadline = time.monotonic() + 12
                    registration = registration_state_path(data_dir)
                    while time.monotonic() < deadline:
                        state = _json(registration, {})
                        if state.get("registry_hash") == expected_registry_hash:
                            sync_style_library(data_dir)
                            break
                        time.sleep(0.2)
                registration_pending = (
                    xmp_changed
                    and int(
                        (load_style_index(data_dir).get("summary") or {}).get(
                            "awaiting_registration"
                        )
                        or 0
                    )
                    > 0
                )
            library = _public_style_library(data_dir, lut_engine)
        except (CreativeLutError, OSError, ValueError, TypeError) as exc:
            raise HTTPException(
                409,
                f"风格文件已导入，但 AI 索引自动重建失败：{exc}",
            ) from exc
        return {
            "imported_count": imported,
            "reused_count": reused,
            "failed_count": len(failures),
            "failures": failures,
            "index_rebuilt": changed,
            "registration_pending": registration_pending,
            "library": library,
        }

    @app.post(
        "/api/runs/{run_id}/exports/prepare",
        dependencies=[Depends(mutate_token)],
    )
    def prepare_export(run_id: str, body: ExportPrepareBody) -> dict[str, Any]:
        if not body.xmp and not body.jpeg:
            raise HTTPException(422, "请至少选择保存 XMP 或导出 JPEG。")
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock, export_lock:
            review = _review(run_file)
            if int(review["revision"]) != int(body.base_revision):
                raise HTTPException(409, "审片已变化，请刷新后重新准备导出。")
            payload = _apply_review(read_json(run_file), review)
            _ensure_xmp_ready(payload)
            input_root = _scoped_dir(
                str(payload.get("input_root") or ""),
                "照片目录",
                pending_root,
            )
            plan = _require_current_develop(run_file, int(review["revision"]))
            if body.develop_revision is None or int(plan.get("revision", -1)) != int(
                body.develop_revision
            ):
                raise HTTPException(409, "构图调色方案已更新，请刷新后重新准备导出。")
            summary = develop_summary(plan, int(review["revision"]))
            if str((plan.get("crop") or {}).get("status")) not in {
                "confirmed",
                "skipped",
            }:
                raise HTTPException(409, "请先完成或跳过构图。")
            if int(summary.get("confirmed_count", 0)) != int(
                summary.get("eligible_count", 0)
            ):
                raise HTTPException(409, "请先确认全部构图。")
            color_mode = str(plan.get("color_mode") or "pending")
            basic_status = str(
                (plan.get("basic_color") or {}).get("status") or "pending"
            )
            creative_status = str(
                (plan.get("creative_style") or {}).get("status") or "pending"
            )
            if color_mode == "skip":
                if basic_status != "skipped":
                    raise HTTPException(409, "不调色步骤尚未保存，请重新选择。")
            elif color_mode == "auto":
                if basic_status != "enabled":
                    raise HTTPException(409, "Lightroom 基础调色尚未确认。")
            elif color_mode == "style":
                if basic_status != "enabled" or creative_status != "confirmed":
                    raise HTTPException(
                        409, "请先确认每个照片组的风格，或选择保持自然。"
                    )
            else:
                raise HTTPException(409, "请先完成或跳过调色。")

            candidates = [
                item
                for item in payload.get("results", [])
                if not item.get("excluded") and int(item.get("rating", 0)) >= 3
            ]
            if not candidates:
                raise HTTPException(409, "当前没有可导出的 3 星以上照片。")
            if int(summary.get("eligible_count", 0)) != len(candidates):
                raise HTTPException(
                    409, "构图方案与当前候选照片不一致，请重新生成构图。"
                )
            for item in candidates:
                source = Path(str(item.get("path") or ""))
                if (
                    source.suffix.casefold() not in PROPRIETARY_RAW_EXTENSIONS
                    or not source.is_file()
                    or not _inside(source, input_root)
                ):
                    raise HTTPException(
                        409,
                        "首版导出只接受照片目录内、使用独立 XMP 的相机 RAW："
                        f"{source.name or source}",
                    )
            _ensure_export_style_targets(plan, xmp=body.xmp)
            frozen_items = _freeze_export_items(payload, plan, data_dir, lut_engine)
            if len(frozen_items) != len(candidates):
                raise HTTPException(409, "最终方案未覆盖全部 3 星以上照片。")
            output_dir = _export_output_dir(
                body.jpeg_output_dir if body.jpeg else None,
                input_root,
            )
            snapshot_sha256 = hashlib.sha256(
                json.dumps(
                    frozen_items,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            try:
                spec = create_export_spec(
                    data_dir,
                    run_id=run_id,
                    input_root=input_root,
                    items=frozen_items,
                    xmp=body.xmp,
                    jpeg=body.jpeg,
                    review_revision=int(review["revision"]),
                    develop_revision=int(plan.get("revision", 0)),
                    develop_plan_id=str(plan.get("plan_id") or ""),
                    source_snapshot_sha256=snapshot_sha256,
                    output_dir=output_dir,
                    jpeg_options=body.jpeg_settings,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            spec["workflow"] = {
                "crop": str((plan.get("crop") or {}).get("status")),
                "color_mode": color_mode,
                "basic_color": basic_status,
                "creative_style": creative_status,
            }
            spec["source_results"] = str(run_file)
            write_json(export_spec_path(data_dir, str(spec["export_spec_id"])), spec)
            return _public_export_spec(spec)

    @app.get("/api/exports/{export_spec_id}")
    def get_export(export_spec_id: str) -> dict[str, Any]:
        with export_lock:
            try:
                spec = load_export_spec(data_dir, export_spec_id)
            except FileNotFoundError as exc:
                raise HTTPException(404, "导出任务不存在。") from exc
            except (OSError, ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc
            _reconcile_export_spec(data_dir, jobs, spec, lut_engine)
            return _public_export_spec(spec)

    @app.post(
        "/api/exports/{export_spec_id}/execute",
        dependencies=[Depends(mutate_token)],
    )
    def execute_export(
        export_spec_id: str,
        body: ExportExecuteBody,
    ) -> dict[str, Any]:
        with review_lock, develop_lock, export_lock:
            try:
                spec = load_export_spec(data_dir, export_spec_id)
            except FileNotFoundError as exc:
                raise HTTPException(404, "导出任务不存在。") from exc
            except (OSError, ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc
            _reconcile_export_spec(data_dir, jobs, spec, lut_engine)
            if spec.get("status") == "complete":
                return _public_export_spec(spec)
            active = jobs.active()
            if active:
                raise HTTPException(409, f"已有任务正在运行：{active['title']}")
            run_file = _run_file(data_dir, str(spec.get("run_id") or ""))
            review = _review(run_file)
            if int(review["revision"]) != int(spec.get("review_revision", -1)):
                raise HTTPException(409, "审片已变化，这个冻结导出已失效，请重新准备。")
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", -1)) != int(
                spec.get("develop_revision", -2)
            ) or str(plan.get("plan_id") or "") != str(
                spec.get("develop_plan_id") or ""
            ):
                raise HTTPException(409, "构图调色方案已变化，请重新准备导出。")
            _verify_export_sources(spec)
            tool_root = (
                content_layout.tools
                if content_layout is not None
                else project_root / ".runtime" / "tools"
            )
            portable_exiftool = (
                tool_root / "exiftool-13.59" / "exiftool-13.59_64" / "exiftool.exe"
            )
            exiftool_ready = (
                Path(os.environ.get("PHOTO_AI_EXIFTOOL", "")).is_file()
                or portable_exiftool.is_file()
            )
            has_creative_look = any(
                str(
                    (
                        ((item.get("recipe") or {}).get("creative_style") or {}).get(
                            "look_kind"
                        )
                    )
                    or ""
                ).casefold()
                == "lightroom_profile"
                for item in spec.get("items", [])
                if isinstance(item, dict)
            )
            # Creative Looks are nested Lightroom settings and cannot be
            # represented by the direct ExifTool-only XMP writer.
            force_lightroom = (
                has_creative_look
                or _lightroom_state() != "closed"
                or not exiftool_ready
            )
            try:
                execution = create_export_attempt(
                    data_dir,
                    spec,
                    retry_failed_only=body.retry_failed_only,
                    force_lightroom=force_lightroom,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                raise HTTPException(409, str(exc)) from exc
            if execution is None:
                return _public_export_spec(spec)
            attempt_id = str(execution["attempt_id"])
            try:
                if execution["engine"] == "lightroom":
                    try:
                        bridge_status = _lightroom_status(data_dir, project_root)
                    except (OSError, ValueError, TypeError) as exc:
                        raise HTTPException(
                            409, f"无法读取 Lightroom 桥接状态：{exc}"
                        ) from exc
                    lightroom = bridge_status.get("lightroom") or {}
                    executable = Path(str(lightroom.get("executable") or ""))
                    plugin = bridge_status.get("plugin") or {}
                    if not lightroom.get("compatible") or not executable.is_file():
                        raise HTTPException(
                            409, "没有检测到 Lightroom Classic 14.3 或更高版本。"
                        )
                    if not bridge_status.get("configured") or not plugin.get(
                        "points_to_this_bridge"
                    ):
                        raise HTTPException(409, "请先在设置中配置 Lightroom 桥接。")
                    batch_id = f"export-{uuid.uuid4().hex[:20]}"
                    job = start_job(
                        "lightroom_apply",
                        [
                            "lightroom-export",
                            "--spec",
                            str(execution["spec_path"]),
                            "--data-dir",
                            str(data_dir),
                            "--batch-id",
                            batch_id,
                            "--lightroom-exe",
                            str(executable),
                        ],
                        {
                            "title": f"Lightroom 最终导出 · {len(execution['work'])} 项",
                            "run_id": spec["run_id"],
                            "export_spec_id": spec["export_spec_id"],
                            "attempt_id": attempt_id,
                            "batch_id": batch_id,
                            "results_path": execution["spec_path"],
                        },
                    )
                else:
                    if _lightroom_state() != "closed":
                        raise HTTPException(409, "直接创建 XMP 前请先关闭 Lightroom。")
                    if not exiftool_ready:
                        raise HTTPException(409, "ExifTool 不可用，无法安全创建 XMP。")
                    batch_id = None
                    job = start_job(
                        "xmp_commit",
                        [
                            "write-xmp",
                            "--results",
                            str(execution["results_path"]),
                            "--commit",
                            "--min-rating",
                            "3",
                        ],
                        {
                            "title": f"保存最终 XMP · {len(execution['work'])} 张",
                            "run_id": spec["run_id"],
                            "export_spec_id": spec["export_spec_id"],
                            "attempt_id": attempt_id,
                            "results_path": execution["results_path"],
                            "direct": True,
                        },
                    )
            except HTTPException as exc:
                abandon_export_attempt(
                    data_dir,
                    spec,
                    attempt_id=attempt_id,
                    error=str(exc.detail),
                )
                raise
            activate_export_attempt(
                data_dir,
                spec,
                attempt_id=attempt_id,
                job_id=str(job["id"]),
                batch_id=batch_id,
            )
            return job

    @app.post(
        "/api/runs/{run_id}/style-recommendations", dependencies=[Depends(mutate_token)]
    )
    def recommend_styles(run_id: str, body: StyleRecommendationBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "调色方案已更新，请刷新后重试。")
            if str(plan.get("crop", {}).get("status")) not in {"confirmed", "skipped"}:
                raise HTTPException(409, "请先完成或跳过构图。")
            catalog = _configured_style_catalog(data_dir)
            if not catalog.get("default_pool"):
                try:
                    catalog = sync_style_library(data_dir)
                except (OSError, ValueError, TypeError) as exc:
                    raise HTTPException(409, f"风格库尚未就绪：{exc}") from exc
            group_ids = {
                int(item.get("group_id", -1)) for item in plan.get("items", [])
            }
            if body.scope == "group" and body.group_id not in group_ids:
                raise HTTPException(404, "照片组不存在。")
            try:
                bridge = _lightroom_status(data_dir, project_root)
                _require_style_lightroom(bridge)
            except (OSError, ValueError, TypeError) as exc:
                raise HTTPException(409, f"无法读取 Lightroom 桥接状态：{exc}") from exc
            batch_id = f"style-{run_id}-{uuid.uuid4().hex[:10]}"
            args = [
                "style-recommend",
                "--run-dir",
                str(run_file.parent),
                "--data-dir",
                str(data_dir),
                "--batch-id",
                batch_id,
                "--base-revision",
                str(int(plan.get("revision", 0))),
            ]
            if body.scope == "group":
                args.extend(["--group-id", str(body.group_id)])
            else:
                # CLI compatibility sentinel: the worker resolves group 0 to
                # one project-wide representative set, never to a photo group.
                args.extend(["--group-id", "0"])
            return start_job(
                "style_recommend",
                args,
                {
                    "title": "生成 Lightroom 风格预览并由 AI 复评",
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "group_id": body.group_id,
                    "scope": body.scope,
                    "base_revision": int(plan.get("revision", 0)),
                    "requires_lightroom": True,
                    "lightroom_state": str(
                        (bridge.get("heartbeat") or {}).get("state") or "unknown"
                    ),
                },
            )

    @app.post(
        "/api/runs/{run_id}/style-recommendations/groups",
        dependencies=[Depends(mutate_token)],
    )
    def recommend_styles_for_all_groups(
        run_id: str, body: AllGroupsStyleRecommendationBody
    ) -> dict[str, Any]:
        """Generate one independent recommendation per group in one cancellable job."""

        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "调色方案已更新，请刷新后重试。")
            if str(plan.get("crop", {}).get("status")) not in {
                "confirmed",
                "skipped",
            }:
                raise HTTPException(409, "请先完成或跳过构图。")
            catalog = _configured_style_catalog(data_dir)
            if not catalog.get("default_pool"):
                try:
                    catalog = sync_style_library(data_dir)
                except (OSError, ValueError, TypeError) as exc:
                    raise HTTPException(409, f"风格库尚未就绪：{exc}") from exc
            group_ids = sorted(
                {
                    int(item.get("group_id", -1))
                    for item in plan.get("items", [])
                    if isinstance(item, dict) and int(item.get("group_id", -1)) >= 1
                }
            )
            if not group_ids:
                raise HTTPException(409, "当前工程没有可生成风格推荐的照片组。")
            try:
                bridge = _lightroom_status(data_dir, project_root)
                _require_style_lightroom(bridge)
            except (OSError, ValueError, TypeError) as exc:
                raise HTTPException(409, f"无法读取 Lightroom 桥接状态：{exc}") from exc
            batch_id = f"style-groups-{run_id}-{uuid.uuid4().hex[:10]}"
            return start_job(
                "style_recommend",
                [
                    "style-recommend",
                    "--run-dir",
                    str(run_file.parent),
                    "--data-dir",
                    str(data_dir),
                    "--batch-id",
                    batch_id,
                    "--base-revision",
                    str(int(plan.get("revision", 0))),
                    "--all-groups",
                ],
                {
                    "title": f"为全部 {len(group_ids)} 组生成独立风格推荐",
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "group_id": None,
                    "group_count": len(group_ids),
                    "scope": "groups",
                    "base_revision": int(plan.get("revision", 0)),
                    "requires_lightroom": True,
                    "lightroom_state": str(
                        (bridge.get("heartbeat") or {}).get("state") or "unknown"
                    ),
                },
            )

    @app.post("/api/runs/{run_id}/style-preview", dependencies=[Depends(mutate_token)])
    def rerender_style_preview(run_id: str, body: StylePreviewBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "调色方案已更新，请刷新后重试。")
            creative = plan.get("creative_style") or {}
            group = (
                creative.get("global_selection")
                if body.scope == "global"
                else creative.get("groups", {}).get(str(body.group_id))
            )
            if not isinstance(group, dict):
                detail = (
                    "当前工程还没有可调强度的全局风格推荐。"
                    if body.scope == "global"
                    else "照片组还没有可调强度的风格推荐。"
                )
                raise HTTPException(404, detail)
            if body.scope == "global" and body.lut_id:
                raise HTTPException(
                    422,
                    "全局 .cube LUT 强度预览尚未就绪；请使用 Lightroom 外观或按组调整。",
                )
            if body.lut_id:
                try:
                    lut = lut_engine.get_lut(body.lut_id)
                except (CreativeLutError, OSError) as exc:
                    raise HTTPException(422, "该 LUT 不在当前创意外观库中。") from exc
                if lut.lut_hash != body.lut_hash:
                    raise HTTPException(409, "LUT 版本已经变化，请重新运行 AI 推荐。")
                resource_args = [
                    "--lut-id",
                    lut.lut_id,
                    "--lut-hash",
                    lut.lut_hash,
                ]
                resource_context = {"lut_id": lut.lut_id, "lut_hash": lut.lut_hash}
                requires_lightroom = False
            else:
                candidate = next(
                    (
                        item
                        for item in group.get("top3") or []
                        if str(item.get("preset_id") or "") == body.preset_id
                    ),
                    None,
                )
                catalog = _configured_style_catalog(data_dir)
                allowed = set(catalog.get("default_pool") or [])
                catalog_entry = next(
                    (
                        item
                        for item in catalog.get("entries", [])
                        if str(item.get("preset_id") or "") == body.preset_id
                        and not item.get("duplicate_of")
                    ),
                    None,
                )
                source = candidate if isinstance(candidate, dict) else catalog_entry
                if not isinstance(source, dict) or body.preset_id not in allowed:
                    raise HTTPException(
                        422, "该预设不在当前可用的 Lightroom 风格库中。"
                    )
                if (
                    str(source.get("preset_hash") or source.get("file_hash") or "")
                    != body.preset_hash
                ):
                    raise HTTPException(409, "预设版本已经变化，请重新运行 AI 推荐。")
                # The catalog entry is authoritative for runtime capability. Old
                # recommendation files may still contain the source XMP flag even
                # though Lightroom converted it to a non-adjustable plugin preset.
                supports_amount = bool(
                    catalog_entry and runtime_supports_amount(catalog_entry)
                )
                if not supports_amount and body.amount != 100:
                    detail = (
                        "此预设由插件托管，Lightroom 仅支持 100%；可换原生预设调强度。"
                        if catalog_entry
                        and str(catalog_entry.get("preset_scope") or "catalog")
                        == "plugin"
                        else "这个预设未启用 Lightroom 强度调整。"
                    )
                    raise HTTPException(422, detail)
                resource_args = [
                    "--preset-id",
                    str(body.preset_id),
                    "--preset-hash",
                    str(body.preset_hash),
                ]
                resource_context = {
                    "preset_id": body.preset_id,
                    "preset_hash": body.preset_hash,
                }
                requires_lightroom = True
            if requires_lightroom:
                try:
                    bridge = _lightroom_status(data_dir, project_root)
                    _require_style_lightroom(bridge)
                except (OSError, ValueError, TypeError) as exc:
                    raise HTTPException(
                        409, f"无法读取 Lightroom 桥接状态：{exc}"
                    ) from exc
            batch_id = f"strength-{run_id}-{uuid.uuid4().hex[:10]}"
            target_label = "全局" if body.scope == "global" else f"组 {body.group_id}"
            worker_group_id = 0 if body.scope == "global" else int(body.group_id)
            return start_job(
                "style_preview",
                [
                    "style-recommend",
                    "--run-dir",
                    str(run_file.parent),
                    "--data-dir",
                    str(data_dir),
                    "--batch-id",
                    batch_id,
                    "--base-revision",
                    str(int(plan.get("revision", 0))),
                    "--group-id",
                    str(worker_group_id),
                    *resource_args,
                    "--amount",
                    str(body.amount),
                    "--preview-only",
                ],
                {
                    "title": f"重渲{target_label}风格强度 {body.amount}%",
                    "amount": body.amount,
                    "run_id": run_id,
                    "batch_id": batch_id,
                    "group_id": body.group_id,
                    "scope": body.scope,
                    "base_revision": int(plan.get("revision", 0)),
                    "requires_lightroom": requires_lightroom,
                    "lightroom_state": (
                        str((bridge.get("heartbeat") or {}).get("state") or "unknown")
                        if requires_lightroom
                        else "not_required"
                    ),
                    **resource_context,
                },
            )

    @app.post("/api/tools/raw-jpeg/preview", dependencies=[Depends(mutate_token)])
    def preview_raw_jpeg(body: RawJpegPreviewBody) -> dict[str, Any]:
        mixed_root = (
            _scoped_toolbox_dir(body.mixed_path, "混合照片目录", pending_root)
            if body.layout == "mixed" and body.mixed_path
            else None
        )
        raw_root = (
            _scoped_toolbox_dir(body.raw_path, "RAW 文件夹", pending_root)
            if body.layout == "separate" and body.raw_path
            else None
        )
        jpeg_root = (
            _scoped_toolbox_dir(body.jpeg_path, "成片文件夹", pending_root)
            if body.layout == "separate" and body.jpeg_path
            else None
        )
        try:
            return create_raw_jpeg_plan(
                data_dir,
                layout=body.layout,
                direction=body.direction,
                mixed_root=mixed_root,
                raw_root=raw_root,
                jpeg_root=jpeg_root,
                raw_extensions=body.raw_extensions,
                jpeg_extensions=body.jpeg_extensions,
                recursive=body.recursive,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/tools/raw-jpeg/transactions")
    def raw_jpeg_transactions() -> list[dict[str, Any]]:
        return list_raw_jpeg_transactions(data_dir)[0]

    @app.post("/api/tools/raw-jpeg/execute", dependencies=[Depends(mutate_token)])
    def execute_raw_jpeg(body: RawJpegExecuteBody) -> dict[str, Any]:
        if _lightroom_state() != "closed":
            raise HTTPException(409, "Lightroom 必须明确处于关闭状态。")
        try:
            source_plan = raw_jpeg_plan_path(data_dir, body.plan_id)
            plan = load_plan(source_plan)
        except (OSError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc
        if plan.get("status") != "planned":
            raise HTTPException(409, "这个操作计划已经执行或正在执行。")
        if not plan.get("complete", True):
            raise HTTPException(409, "扫描结果不完整，请排除读取错误后重新扫描。")
        if not plan.get("candidates"):
            raise HTTPException(409, "这个计划没有需要整理的孤片。")
        if pending_root is not None and any(
            not _inside(Path(root), pending_root)
            for root in plan.get("roots", {}).values()
        ):
            raise HTTPException(409, "操作计划已超出待处理目录，请重新扫描。")
        result_path = raw_jpeg_transactions_root(data_dir) / f"{body.plan_id}.json"
        return start_job(
            "raw_jpeg_execute",
            [
                "raw-jpeg-execute",
                "--plan",
                str(source_plan),
                "--transactions-dir",
                str(raw_jpeg_transactions_root(data_dir)),
            ],
            {
                "title": f"整理 RAW / 成片 · {len(plan.get('candidates', []))} 个孤片",
                "plan_id": body.plan_id,
                "result_path": str(result_path),
            },
        )

    @app.post("/api/tools/raw-jpeg/rollback", dependencies=[Depends(mutate_token)])
    def rollback_raw_jpeg(body: RawJpegRollbackBody) -> dict[str, Any]:
        if _lightroom_state() != "closed":
            raise HTTPException(409, "Lightroom 必须明确处于关闭状态。")
        try:
            summary, manifest = load_raw_jpeg_transaction(
                data_dir, body.transaction_id, reconcile=True
            )
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(404, "RAW / 成片操作记录不存在。") from exc
        if pending_root is not None and any(
            not _inside(Path(root), pending_root)
            for root in summary.get("roots", {}).values()
        ):
            raise HTTPException(409, "操作记录已超出待处理目录，已拒绝恢复。")
        if not summary.get("rollbackable") and not summary.get("needs_attention"):
            raise HTTPException(409, "这次操作没有可恢复的文件。")
        pending_count = summary["remaining_count"] + summary.get("conflict_count", 0)
        return start_job(
            "raw_jpeg_rollback",
            ["raw-jpeg-rollback", "--manifest", str(manifest)],
            {
                "title": f"撤销 RAW / 成片整理 · {pending_count} 个文件",
                "transaction_id": body.transaction_id,
                "result_path": str(manifest),
            },
        )

    @app.post("/api/tools/xmp-cleanup/preview", dependencies=[Depends(mutate_token)])
    def preview_xmp_cleanup(body: XmpCleanupPreviewBody) -> dict[str, Any]:
        root = _scoped_toolbox_dir(body.root_path, "XMP 照片目录", pending_root)
        try:
            return create_xmp_cleanup_plan(
                data_dir,
                root=root,
                recursive=body.recursive,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/tools/xmp-cleanup/transactions")
    def xmp_cleanup_transactions() -> list[dict[str, Any]]:
        return list_xmp_cleanup_transactions(data_dir)[0]

    @app.post("/api/tools/xmp-cleanup/execute", dependencies=[Depends(mutate_token)])
    def execute_xmp_cleanup(body: XmpCleanupExecuteBody) -> dict[str, Any]:
        try:
            source_plan = xmp_cleanup_plan_path(data_dir, body.plan_id)
            plan = load_xmp_cleanup_plan(source_plan)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(404, str(exc)) from exc
        if plan.get("status") != "planned":
            raise HTTPException(409, "这个 XMP 清理计划已经执行或正在执行。")
        if not plan.get("complete", True):
            raise HTTPException(409, "扫描结果不完整，请排除读取错误后重新扫描。")
        if int(plan.get("xmp_count", 0)) <= 0:
            raise HTTPException(409, "这个计划没有需要清理的 XMP。")
        if pending_root is not None and not _inside(
            Path(str(plan.get("root_path") or "")), pending_root
        ):
            raise HTTPException(409, "XMP 清理计划已超出待处理目录，请重新扫描。")
        result_path = xmp_cleanup_transactions_root(data_dir) / f"{body.plan_id}.json"
        return start_job(
            "xmp_cleanup_execute",
            [
                "xmp-cleanup-execute",
                "--plan",
                str(source_plan),
                "--transactions-dir",
                str(xmp_cleanup_transactions_root(data_dir)),
            ],
            {
                "title": f"永久删除 XMP · {int(plan['xmp_count'])} 个文件",
                "plan_id": body.plan_id,
                "result_path": str(result_path),
            },
        )

    @app.post("/api/tools/xmp-cleanup/rollback", dependencies=[Depends(mutate_token)])
    def rollback_xmp_cleanup(body: XmpCleanupRollbackBody) -> dict[str, Any]:
        if _lightroom_state() != "closed":
            raise HTTPException(409, "Lightroom 必须明确处于关闭状态。")
        try:
            summary, manifest = load_xmp_cleanup_transaction(
                data_dir, body.transaction_id, reconcile=True
            )
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(404, "XMP 清理操作记录不存在。") from exc
        if pending_root is not None and not _inside(
            Path(str(summary.get("root_path") or "")), pending_root
        ):
            raise HTTPException(409, "XMP 清理记录已超出待处理目录，已拒绝恢复。")
        if not summary.get("rollbackable") and not summary.get("needs_attention"):
            raise HTTPException(409, "这次 XMP 清理没有可恢复的文件。")
        pending_count = int(
            summary.get("remaining_count", summary.get("remaining", 0))
        ) + int(summary.get("conflict_count", summary.get("conflict", 0)))
        return start_job(
            "xmp_cleanup_rollback",
            ["xmp-cleanup-rollback", "--manifest", str(manifest)],
            {
                "title": f"恢复文件夹 XMP · {pending_count} 个文件",
                "transaction_id": body.transaction_id,
                "result_path": str(manifest),
            },
        )

    @app.post("/api/group", dependencies=[Depends(mutate_token)])
    def group(body: GroupBody) -> dict[str, Any]:
        input_path = _scoped_dir(body.input_path, "待处理目录", pending_root)
        return start_job(
            "group",
            [
                "group",
                "--input",
                str(input_path),
                "--data-dir",
                str(data_dir),
                "--retain-ratio",
                str(body.retain_ratio),
                "--mode",
                body.mode,
            ],
            {"title": f"照片分类 · {input_path.name}", "mode": body.mode},
        )

    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        return _projects(data_dir)

    @app.post("/api/projects", dependencies=[Depends(mutate_token)])
    def create_project(body: ProjectCreateBody) -> dict[str, Any]:
        require_model_profile_ready()
        input_path = _scoped_dir(body.input_path, "照片目录", pending_root)
        with project_lock:
            registration = _register_project(data_dir, input_path)
            return _project_detail(data_dir, str(registration["project_id"]))

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str) -> dict[str, Any]:
        return _project_detail(data_dir, project_id)

    @app.post(
        "/api/projects/{project_id}/group",
        dependencies=[Depends(mutate_token)],
    )
    def group_project(project_id: str, body: ProjectGroupBody) -> dict[str, Any]:
        project = _project_detail(data_dir, project_id)
        input_path = _scoped_dir(
            str(project.get("input_root") or ""),
            "照片目录",
            pending_root,
        )
        if _project_id(str(input_path)) != project_id:
            raise HTTPException(409, "工程与照片目录不一致。")
        return start_job(
            "group",
            [
                "group",
                "--input",
                str(input_path),
                "--data-dir",
                str(data_dir),
                "--retain-ratio",
                str(body.retain_ratio),
                "--mode",
                body.mode,
            ],
            {
                "title": f"照片分类 · {input_path.name}",
                "mode": body.mode,
                "project_id": project_id,
            },
        )

    @app.delete("/api/projects/{project_id}", dependencies=[Depends(mutate_token)])
    def delete_project(project_id: str, body: DeleteProjectBody) -> dict[str, Any]:
        if body.confirmation != "删除工程":
            raise HTTPException(422, "请输入“删除工程”确认。")
        if jobs.active():
            raise HTTPException(409, "有任务正在运行，请完成或取消后再删除工程。")
        return _trash_project(data_dir, project_id, project_lock)

    @app.get("/api/runs")
    def list_runs(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
        return _runs(data_dir, limit)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        return _run_detail(data_dir, run_id)

    @app.get("/api/runs/{run_id}/preview/{index}")
    def preview(run_id: str, index: int) -> FileResponse:
        payload = read_json(_run_file(data_dir, run_id))
        results = payload.get("results", [])
        if index < 0 or index >= len(results):
            raise HTTPException(404, "预览不存在。")
        path = Path(results[index].get("preview", ""))
        root = runtime_cache_dir(data_dir) / "previews"
        if not path.is_file() or not _inside(path, root):
            raise HTTPException(404, "预览不存在。")
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/api/runs/{run_id}/develop")
    def get_develop(run_id: str) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        review = _review(run_file)
        plan = load_develop_plan(run_file.parent)
        if not plan:
            return develop_summary(None, int(review["revision"]))
        return _public_develop_plan(run_file, plan, int(review["revision"]))

    @app.get("/api/runs/{run_id}/develop/progress")
    def get_develop_progress(run_id: str) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        progress_file = run_file.parent / "develop-progress.json"
        return _json(
            progress_file,
            {
                "status": "idle",
                "overall_percent": 0.0,
                "current": 0,
                "completed": 0,
                "total": 0,
                "nodes": [],
            },
        )

    @app.post("/api/runs/{run_id}/develop", dependencies=[Depends(mutate_token)])
    def generate_develop(run_id: str, body: DevelopGenerateBody) -> dict[str, Any]:
        require_model_profile_ready()
        run_file = _run_file(data_dir, run_id)
        progress_file = run_file.parent / "develop-progress.json"
        with review_lock, develop_lock:
            review = _review(run_file)
            if int(review["revision"]) != int(body.base_revision):
                raise HTTPException(409, "审片已变化，请刷新后重新生成。")
            payload = _apply_review(read_json(run_file), review)
            _ensure_xmp_ready(payload)
            total = sum(
                not item.get("excluded") and int(item.get("rating", 0)) >= 3
                for item in payload.get("results", [])
            )
            input_path = jobs.jobs_dir / f"develop-input-{uuid.uuid4().hex}.json"
            write_json(
                input_path,
                {
                    "schema_version": 1,
                    "review_revision": int(review["revision"]),
                    "payload": payload,
                },
            )
            write_json(
                progress_file,
                {
                    "status": "running",
                    "phase": "subjects",
                    "stage_label": "准备智能构图",
                    "current": 0,
                    "completed": 0,
                    "total": total,
                    "filename": "",
                    "overall_percent": 0.0,
                    "nodes": [],
                    "updated_at": _now(),
                },
            )

        try:
            try:
                job = start_job(
                    "develop",
                    [
                        "develop-plan",
                        "--input",
                        str(input_path),
                        "--run-dir",
                        str(run_file.parent),
                        "--review-revision",
                        str(int(review["revision"])),
                    ],
                    {
                        "title": f"智能构图 · {total} 张",
                        "run_id": run_id,
                        "review_revision": int(review["revision"]),
                        "total": total,
                    },
                )
            except HTTPException as exc:
                write_json(
                    progress_file,
                    {
                        "status": "failed",
                        "stage_label": "构图分析失败",
                        "message": str(exc.detail),
                        "overall_percent": 0.0,
                        "current": 0,
                        "completed": 0,
                        "total": total,
                        "nodes": [],
                        "updated_at": _now(),
                    },
                )
                raise
            terminal = jobs.wait(str(job["id"]))
        finally:
            try:
                input_path.unlink(missing_ok=True)
            except OSError:
                pass

        if terminal.get("status") != "completed":
            cancelled = terminal.get("status") == "cancelled"
            message = (
                "智能构图已取消；已有缓存会在下次继续复用。"
                if cancelled
                else str(terminal.get("message") or "智能构图 Worker 执行失败。")
            )
            current_progress = _json(progress_file, {})
            nodes = (
                current_progress.get("nodes")
                if isinstance(current_progress, dict)
                and isinstance(current_progress.get("nodes"), list)
                else []
            )
            write_json(
                progress_file,
                {
                    **(current_progress if isinstance(current_progress, dict) else {}),
                    "status": "cancelled" if cancelled else "failed",
                    "stage_label": "智能构图已取消" if cancelled else "构图分析失败",
                    "message": message,
                    "nodes": [
                        {
                            **node,
                            "status": "failed"
                            if str(node.get("status")) == "active"
                            else node.get("status", "pending"),
                        }
                        for node in nodes
                    ],
                    "updated_at": _now(),
                },
            )
            raise HTTPException(409, message)

        with review_lock, develop_lock:
            current_review = _review(run_file)
            plan = load_develop_plan(run_file.parent)
            if not plan:
                raise HTTPException(409, "智能构图 Worker 未生成有效方案。")
            if int(current_review["revision"]) != int(review["revision"]):
                raise HTTPException(409, "审片在构图期间发生变化，请重新生成。")
            return _public_develop_plan(
                run_file, plan, int(current_review["revision"])
            )

    @app.get("/api/runs/{run_id}/develop/preview/{index}")
    def develop_preview(run_id: str, index: int) -> FileResponse:
        run_file = _run_file(data_dir, run_id)
        plan = load_develop_plan(run_file.parent)
        if not plan:
            raise HTTPException(404, "裁剪调色预览不存在。")
        item = next(
            (
                value
                for value in plan.get("items", [])
                if int(value.get("index", -1)) == index
            ),
            None,
        )
        if item is None:
            raise HTTPException(404, "裁剪调色预览不存在。")
        path = Path(str(item.get("preview_path", "")))
        root = run_file.parent / "develop-previews"
        if not path.is_file() or not _inside(path, root):
            raise HTTPException(404, "裁剪调色预览不存在。")
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/api/runs/{run_id}/style-preview/{preview_key}")
    def style_preview(run_id: str, preview_key: str) -> FileResponse:
        run_file = _run_file(data_dir, run_id)
        if not STYLE_PREVIEW_KEY_RE.fullmatch(preview_key):
            raise HTTPException(404, "风格预览不存在。")
        root = (run_file.parent / "style-previews").resolve()
        path = (root / f"{preview_key}.jpg").resolve()
        if not path.is_file() or not _inside(path, root):
            raise HTTPException(404, "风格预览不存在。")
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @app.patch(
        "/api/runs/{run_id}/develop/{index}", dependencies=[Depends(mutate_token)]
    )
    def edit_develop(run_id: str, index: int, body: DevelopEditBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "裁剪调色方案已更新，请刷新后重试。")
            results = read_json(run_file).get("results", [])
            if index < 0 or index >= len(results):
                raise HTTPException(404, "照片不存在。")
            source_preview = Path(str(results[index].get("preview", "")))
            if not source_preview.is_file() or not _inside(
                source_preview, runtime_cache_dir(data_dir) / "previews"
            ):
                raise HTTPException(409, "原始预览不存在，请重新评分。")
            item = next(
                (
                    value
                    for value in plan.get("items", [])
                    if int(value.get("index", -1)) == index
                ),
                None,
            )
            if item is None:
                raise HTTPException(404, "这张照片不在当前成片方案中。")
            item["_source_preview"] = str(source_preview)
            try:
                update_develop_item(
                    plan,
                    run_file.parent,
                    index,
                    crop_id=body.crop_id,
                    style_id=body.style_id,
                    style_strength=body.style_strength,
                    confirmed=body.confirmed,
                )
            except (KeyError, ValueError, FileNotFoundError) as exc:
                raise HTTPException(422, str(exc)) from exc
            return _public_develop_plan(run_file, plan, int(review["revision"]))

    @app.post(
        "/api/runs/{run_id}/develop/confirm-all", dependencies=[Depends(mutate_token)]
    )
    def confirm_develop(run_id: str, body: DevelopConfirmBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "裁剪调色方案已更新，请刷新后重试。")
            confirm_all(plan, run_file.parent)
            return _public_develop_plan(run_file, plan, int(review["revision"]))

    @app.post(
        "/api/runs/{run_id}/develop/skip-crop", dependencies=[Depends(mutate_token)]
    )
    def skip_develop_crop(run_id: str, body: DevelopConfirmBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "裁剪调色方案已更新，请刷新后重试。")
            results = read_json(run_file).get("results", [])
            source_previews: dict[int, Path] = {}
            for item in plan.get("items", []):
                index = int(item.get("index", -1))
                if index < 0 or index >= len(results):
                    raise HTTPException(
                        409, "裁剪调色方案包含不存在的照片，请重新生成。"
                    )
                source_preview = Path(str(results[index].get("preview", "")))
                if not source_preview.is_file() or not _inside(
                    source_preview, runtime_cache_dir(data_dir) / "previews"
                ):
                    raise HTTPException(409, "原始预览不存在，请重新评分。")
                source_previews[index] = source_preview
            try:
                skip_all_crops(plan, run_file.parent, source_previews)
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(409, str(exc)) from exc
            return _public_develop_plan(run_file, plan, int(review["revision"]))

    @app.post(
        "/api/runs/{run_id}/develop/options", dependencies=[Depends(mutate_token)]
    )
    def edit_develop_options(run_id: str, body: DevelopOptionsBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "裁剪调色方案已更新，请刷新后重试。")
            if body.mode is not None:
                update_color_mode(plan, run_file.parent, mode=body.mode)
            elif body.color_enabled is not None:
                update_develop_options(
                    plan, run_file.parent, color_enabled=body.color_enabled
                )
            else:
                raise HTTPException(422, "请选择调色方式。")
            return _public_develop_plan(run_file, plan, int(review["revision"]))

    @app.put(
        "/api/runs/{run_id}/develop/style/groups/{group_id}",
        dependencies=[Depends(mutate_token)],
    )
    def edit_style_group(
        run_id: str, group_id: int, body: StyleGroupBody
    ) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "调色方案已更新，请刷新后重试。")
            try:
                update_style_group(
                    plan,
                    run_file.parent,
                    group_id,
                    preset_id=body.preset_id,
                    preset_hash=body.preset_hash,
                    lut_id=body.lut_id,
                    lut_hash=body.lut_hash,
                    amount=body.amount,
                    status=body.status,
                )
            except KeyError as exc:
                raise HTTPException(404, "照片组不存在。") from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return _public_develop_plan(run_file, plan, int(review["revision"]))

    @app.put(
        "/api/runs/{run_id}/develop/style/global",
        dependencies=[Depends(mutate_token)],
    )
    def edit_style_global(run_id: str, body: StyleGroupBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "调色方案已更新，请刷新后重试。")
            try:
                update_style_global(
                    plan,
                    run_file.parent,
                    preset_id=body.preset_id,
                    preset_hash=body.preset_hash,
                    lut_id=body.lut_id,
                    lut_hash=body.lut_hash,
                    amount=body.amount,
                    status=body.status,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return _public_develop_plan(run_file, plan, int(review["revision"]))

    @app.post(
        "/api/runs/{run_id}/develop/style/confirm-all",
        dependencies=[Depends(mutate_token)],
    )
    def confirm_styles(run_id: str, body: DevelopConfirmBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock, develop_lock:
            review = _review(run_file)
            plan = _require_current_develop(run_file, int(review["revision"]))
            if int(plan.get("revision", 0)) != int(body.base_revision):
                raise HTTPException(409, "调色方案已更新，请刷新后重试。")
            try:
                confirm_recommended_styles(plan, run_file.parent)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return _public_develop_plan(run_file, plan, int(review["revision"]))

    @app.patch("/api/runs/{run_id}/items/{index}", dependencies=[Depends(mutate_token)])
    def rate(run_id: str, index: int, body: RatingBody) -> dict[str, Any]:
        return _edit_review(
            data_dir, run_id, [index], body.rating, body.base_revision, review_lock
        )

    @app.patch("/api/runs/{run_id}/ratings", dependencies=[Depends(mutate_token)])
    def rate_bulk(run_id: str, body: BulkRatingBody) -> dict[str, Any]:
        return _edit_review(
            data_dir,
            run_id,
            list(dict.fromkeys(body.indexes)),
            body.rating,
            body.base_revision,
            review_lock,
        )

    @app.patch("/api/runs/{run_id}/groups", dependencies=[Depends(mutate_token)])
    def move_groups(run_id: str, body: BulkGroupEditBody) -> dict[str, Any]:
        with review_lock:
            active = jobs.active()
            if (
                active
                and active.get("kind") == "score"
                and active.get("context", {}).get("source_run_id") == run_id
            ):
                raise HTTPException(409, "这个分组正在评分，请等待完成后再调整。")
            if body.new_group and body.group_id is not None:
                raise HTTPException(422, "新建分组时不能同时指定已有分组。")
            if body.direction and (body.new_group or body.group_id is not None):
                raise HTTPException(422, "快捷移动不能同时指定目标分组。")
            return _edit_groups(
                data_dir,
                run_id,
                body.indexes,
                body.group_id,
                body.base_revision,
                review_lock,
                new_group=body.new_group,
                direction=body.direction,
            )

    @app.patch(
        "/api/runs/{run_id}/groups/{index}", dependencies=[Depends(mutate_token)]
    )
    def move_group(run_id: str, index: int, body: GroupEditBody) -> dict[str, Any]:
        with review_lock:
            active = jobs.active()
            if (
                active
                and active.get("kind") == "score"
                and active.get("context", {}).get("source_run_id") == run_id
            ):
                raise HTTPException(409, "这个分组正在评分，请等待完成后再调整。")
            return _edit_group(
                data_dir, run_id, index, body.group_id, body.base_revision, review_lock
            )

    @app.patch("/api/runs/{run_id}/excluded", dependencies=[Depends(mutate_token)])
    def exclude_items(run_id: str, body: ExcludeItemsBody) -> dict[str, Any]:
        with review_lock:
            active = jobs.active()
            if (
                active
                and active.get("kind") == "score"
                and active.get("context", {}).get("source_run_id") == run_id
            ):
                raise HTTPException(409, "这个工程正在评分，请等待完成后再调整。")
            return _edit_excluded(
                data_dir,
                run_id,
                body.indexes,
                body.excluded,
                body.base_revision,
                review_lock,
            )

    @app.post("/api/runs/{run_id}/score", dependencies=[Depends(mutate_token)])
    def score_run(run_id: str, body: RunScoreBody) -> dict[str, Any]:
        run_file = _run_file(data_dir, run_id)
        with review_lock:
            review = _review(run_file)
            if int(review["revision"]) != body.base_revision:
                raise HTTPException(409, "分组已更新，请刷新后再评分。")
            payload = _apply_review(read_json(run_file), review)
            input_path = _scoped_dir(
                str(payload.get("input_root", "")), "待处理目录", pending_root
            )
            grouping_snapshot = _materialize_grouping(run_file, payload)
            if not _inside(grouping_snapshot, run_file.parent):
                raise HTTPException(409, "分组快照路径异常。")
            mode = body.mode or str(payload.get("scoring_mode") or "deep")
            retain_ratio = (
                body.retain_ratio
                if body.retain_ratio is not None
                else float(payload.get("retain_ratio") or 0.30)
            )
            title = (
                "重新评分" if payload.get("workflow_state") == "scored" else "AI 评分"
            )
            return start_job(
                "score",
                [
                    "score",
                    "--input",
                    str(input_path),
                    "--data-dir",
                    str(data_dir),
                    "--retain-ratio",
                    str(retain_ratio),
                    "--mode",
                    mode,
                    "--groups-from",
                    str(grouping_snapshot),
                    "--source-run-id",
                    run_id,
                ],
                {
                    "title": f"{title} · {input_path.name}",
                    "source_run_id": run_id,
                    "source_revision": body.base_revision,
                    "grouping_snapshot": str(grouping_snapshot),
                    "mode": mode,
                },
            )

    @app.get("/api/transactions")
    def list_transactions() -> list[dict[str, Any]]:
        return _transactions(data_dir)[0]

    @app.post("/api/transactions/rollback", dependencies=[Depends(mutate_token)])
    def rollback(body: RollbackBody) -> dict[str, Any]:
        if body.confirmation != "撤销 XMP":
            raise HTTPException(422, "请输入“撤销 XMP”确认。")
        if _lightroom_state() != "closed":
            raise HTTPException(409, "Lightroom 必须明确处于关闭状态。")
        _, mapping = _transactions(data_dir)
        manifest = mapping.get(body.transaction_id)
        if not manifest or not manifest.is_file() or not _inside(manifest, data_dir):
            raise HTTPException(404, "XMP 事务不存在。")
        return start_job(
            "rollback",
            ["rollback", "--manifest", str(manifest)],
            {"title": "回滚 XMP", "manifest_path": str(manifest)},
        )

    return app


app: FastAPI | None = (
    None if os.environ.get("PHOTO_AI_DEFER_DEFAULT_WEB_APP") == "1" else create_app()
)


def main() -> None:
    import uvicorn

    uvicorn.run(app or create_app(), host="127.0.0.1", port=8765, reload=False)


if __name__ == "__main__":
    main()
