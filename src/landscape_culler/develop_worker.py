from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from .progress import emit_progress
from .util import read_json, write_json

DEVELOP_INPUT_SCHEMA = 1
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_managed_paths(input_path: Path, run_dir: Path) -> None:
    """Keep installed Worker input/output inside the selected ContentRoot."""

    content_root_value = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    if not content_root_value:
        return
    content_root = Path(content_root_value).expanduser().resolve()
    jobs_root = content_root / "state" / "web" / "jobs"
    projects_root = Path(
        os.environ.get("PHOTO_AI_PROJECTS_DIR") or content_root / "projects"
    ).expanduser().resolve()
    if not _inside(input_path, jobs_root):
        raise ValueError("智能构图输入必须位于受管数据目录的 state/web/jobs 中。")
    if not _inside(run_dir, projects_root):
        raise ValueError("智能构图结果必须位于受管数据目录的 projects 中。")


def run_develop_plan_job(
    input_path: Path,
    run_dir: Path,
    review_revision: int,
) -> dict[str, Any]:
    """Create one develop plan inside the isolated managed AI process.

    The Web service writes an immutable, review-applied payload to the jobs
    directory.  This entrypoint is imported only after CoreWorker has accepted
    the versioned job spec, so smart-crop and its native/AI dependencies never
    enter the lightweight Service process.
    """

    input_path = input_path.expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()
    _validate_managed_paths(input_path, run_dir)
    if not input_path.is_file():
        raise FileNotFoundError(f"智能构图输入不存在：{input_path}")
    if not run_dir.is_dir():
        raise FileNotFoundError(f"工程目录不存在：{run_dir}")
    envelope = read_json(input_path)
    if (
        not isinstance(envelope, dict)
        or int(envelope.get("schema_version", 0)) != DEVELOP_INPUT_SCHEMA
        or not isinstance(envelope.get("payload"), dict)
    ):
        raise ValueError("智能构图输入格式无效。")
    if int(envelope.get("review_revision", -1)) != int(review_revision):
        raise ValueError("智能构图输入的审片版本不一致。")

    # Deliberately delayed until the accepted worker job is executing.
    from .develop import DEVELOP_PROGRESS_PHASES, create_develop_plan

    progress_path = run_dir / "develop-progress.json"
    latest_progress: dict[str, Any] = {}
    progress_warning_logged = False
    retry_progress_at = 0.0

    def persist_progress(event: dict[str, Any]) -> None:
        nonlocal latest_progress, progress_warning_logged, retry_progress_at
        latest_progress = {**event, "updated_at": _now()}
        if event.get("status") == "running" and monotonic() < retry_progress_at:
            return
        try:
            write_json(progress_path, latest_progress)
        except OSError as exc:
            # After bounded atomic retries fail, avoid waiting on the same
            # persistent lock at every AI phase. Terminal events still try.
            retry_progress_at = monotonic() + 5.0
            # Progress is advisory; create_develop_plan still saves its result
            # strictly. A locked progress file must not discard completed work.
            if not progress_warning_logged:
                logger.warning("进度文件暂时无法更新，构图分析继续：%s", exc)
                progress_warning_logged = True

    def save_progress(event: dict[str, Any]) -> None:
        persist_progress(event)
        overall = max(0.0, min(100.0, float(event.get("overall_percent") or 0.0)))
        emit_progress(
            "develop",
            str(event.get("stage_label") or "智能构图"),
            round(overall * 10),
            1000,
            unit="‰",
        )

    try:
        plan = create_develop_plan(
            dict(envelope["payload"]),
            run_dir,
            int(review_revision),
            progress=save_progress,
        )
    except Exception as exc:
        previous = latest_progress
        if not previous:
            try:
                candidate = read_json(progress_path)
                if isinstance(candidate, dict):
                    previous = candidate
            except (OSError, ValueError):
                pass
        nodes = previous.get("nodes") if isinstance(previous.get("nodes"), list) else []
        if not nodes:
            nodes = [
                {
                    "key": key,
                    "label": label,
                    "status": "failed" if index == 0 else "pending",
                }
                for index, (key, label) in enumerate(DEVELOP_PROGRESS_PHASES)
            ]
        else:
            nodes = [
                {
                    **node,
                    "status": "failed"
                    if str(node.get("status")) == "active"
                    else node.get("status", "pending"),
                }
                for node in nodes
            ]
        persist_progress(
            {
                **previous,
                "status": "failed",
                "stage_label": "构图分析失败",
                "message": str(exc),
                "nodes": nodes,
                "updated_at": _now(),
            },
        )
        raise

    total = len(plan.get("items", []))
    persist_progress(
        {
            "status": "completed",
            "phase": "preview",
            "stage_label": "智能构图完成",
            "current": total,
            "completed": total,
            "analyzed": total,
            "total": total,
            "filename": "",
            "overall_percent": 100.0,
            "nodes": [
                {"key": key, "label": label, "status": "completed"}
                for key, label in DEVELOP_PROGRESS_PHASES
            ],
            "updated_at": _now(),
        },
    )
    emit_progress("develop", "智能构图完成", 1000, 1000, unit="‰")
    return {
        "run_id": str(plan.get("run_id") or run_dir.name),
        "plan_id": str(plan.get("plan_id") or ""),
        "review_revision": int(review_revision),
        "eligible_count": total,
    }
