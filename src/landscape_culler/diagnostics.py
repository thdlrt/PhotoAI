"""Read-only, allow-listed support information for local installations."""
from __future__ import annotations

import hashlib
import json
import platform
import re
from datetime import datetime
from pathlib import Path

from . import ai_runtime
from .constants import AI_WORKER_CODE_VERSION, SCORING_PIPELINE_VERSION
from .util import read_json
from .version import PRODUCT_VERSION


def build_identity() -> dict:
    path = ai_runtime.release_resources_root() / "release-manifest.json"
    if not path.is_file():
        return {"status": "unavailable", "id": None, "built_at": None}
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("oversized manifest")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        expected = path.with_suffix(".json.sha256").read_text(encoding="utf-8-sig").split()[0]
        manifest = json.loads(raw.decode("utf-8-sig"))
        if digest != expected.lower() or manifest.get("product") != "PhotoAI" or manifest.get("schema_version") != 1:
            raise ValueError("invalid manifest")
        if manifest.get("version") != PRODUCT_VERSION or manifest.get("worker_code_version", AI_WORKER_CODE_VERSION) != AI_WORKER_CODE_VERSION:
            return {"status": "mismatch", "id": digest[:12], "built_at": None}
        built_at = datetime.fromisoformat(manifest["built_at"]).isoformat()
        return {"status": "identified", "id": digest[:12], "built_at": built_at}
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        return {"status": "invalid", "id": None, "built_at": None}


def diagnostic_report(data_dir: Path, layout, *, configured: bool, job_counts: dict) -> dict:
    build = build_identity()
    installed = None
    profile = None
    engine_status = "unconfigured"
    if layout is not None:
        try:
            engine = ai_runtime.active_engine(layout)
            if engine is not None:
                manifest = read_json(engine / ai_runtime.ENGINE_MANIFEST)
                revision = str(manifest.get("worker_code_version", ""))
                if re.fullmatch(r"worker-v\d+-[a-z0-9-]{1,80}", revision):
                    installed = revision
                profile = manifest.get("profile_id") if manifest.get("profile_id") in {"8gb", "16gb"} else None
                engine_status = "current" if installed == AI_WORKER_CODE_VERSION else "update_required"
        except (OSError, ValueError, TypeError, AttributeError):
            engine_status = "unavailable"
    try:
        from .vision_provider import load_config
        mode = "cloud" if load_config(data_dir).get("mode") == "cloud" else "local"
    except (OSError, ValueError, TypeError, AttributeError):
        mode = "unavailable"
    # Only enum counts enter the report, never job titles, paths, log messages,
    # cloud URLs, configuration dictionaries, tokens or credential metadata.
    counts = {status: max(0, int(job_counts.get(status, 0))) for status in (
        "queued", "running", "cancelling", "failed", "interrupted")}
    report = dict(version=PRODUCT_VERSION, build=build, system=platform.system(),
        architecture=platform.machine(), python=platform.python_version(),
        data_configured=bool(configured), vision_mode=mode, profile=profile,
        worker_expected=AI_WORKER_CODE_VERSION, worker_installed=installed,
        worker_status=engine_status, scoring_pipeline=SCORING_PIPELINE_VERSION, jobs=counts)
    labels = {"identified": "已识别", "unavailable": "未提供", "invalid": "清单无效", "mismatch": "组件不匹配"}
    workers = {"unconfigured": "未配置或环境不可用", "current": "版本一致", "update_required": "工作组件需要更新", "unavailable": "无法读取"}
    lines = ["照片选片诊断摘要", f"版本：{PRODUCT_VERSION}",
        f"构建：{build['id'] or '—'}（{labels[build['status']]}）",
        f"构建时间：{build['built_at'] or '—'}",
        f"系统：{report['system']} / {report['architecture']}", f"后台 Python：{report['python']}",
        f"数据目录：{'已连接' if configured else '未连接'}",
        f"视觉模式：{mode}", f"模型档位：{profile or '未配置'}",
        f"工作组件：{workers[engine_status]}", f"程序要求：{AI_WORKER_CODE_VERSION}",
        f"已安装：{installed or '未确认'}", f"评分管线：{SCORING_PIPELINE_VERSION}",
        "任务计数：" + " / ".join(f"{key}={value}" for key, value in counts.items())]
    return {**report, "copy_text": "\n".join(lines)}
