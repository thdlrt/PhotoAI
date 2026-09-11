from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .util import read_json, write_json

EXPORT_SPEC_SCHEMA_VERSION = 1
ExportTarget = Literal["xmp", "jpeg"]
_TERMINAL = {"succeeded", "skipped"}
_EXPORT_SPEC_ID = re.compile(r"^export-[0-9a-f]{32}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def export_specs_root(data_dir: Path) -> Path:
    return Path(data_dir).resolve() / "exports"


def export_spec_path(data_dir: Path, export_spec_id: str) -> Path:
    if not _EXPORT_SPEC_ID.fullmatch(str(export_spec_id)):
        raise ValueError("导出任务编号无效。")
    return export_specs_root(data_dir) / f"{export_spec_id}.json"


def export_attempt_root(
    data_dir: Path, export_spec_id: str, attempt_id: str
) -> Path:
    return export_specs_root(data_dir) / "attempts" / export_spec_id / attempt_id


def save_export_spec(data_dir: Path, spec: dict[str, Any]) -> None:
    spec["updated_at"] = _now()
    write_json(export_spec_path(data_dir, str(spec["export_spec_id"])), spec)


def normalize_jpeg_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize the only JPEG recipe supported by the first release.

    The browser used two names for a few fields while the Lightroom bridge was
    being introduced.  Accept both spellings, but freeze one exact recipe so a
    later retry cannot silently change rendering parameters.
    """

    values = dict(settings or {})
    color_space = values.pop("color_space", "sRGB")
    size = values.pop("size", values.pop("resize", "original"))
    quality = values.pop("quality", 90)
    collision = values.pop("collision", "suffix")
    sharpening = values.pop("sharpening", None)
    output_sharpening = values.pop("output_sharpening", None)
    output_sharpening_amount = values.pop("output_sharpening_amount", None)
    if sharpening is None:
        if output_sharpening in {None, "screen"} and output_sharpening_amount in {
            None,
            "standard",
        }:
            sharpening = "screen_standard"
        else:
            sharpening = f"{output_sharpening}_{output_sharpening_amount}"
    if values:
        raise ValueError(f"不支持的 JPEG 参数：{', '.join(sorted(values))}")
    expected = {
        "color_space": (color_space, "sRGB"),
        "size": (size, "original"),
        "quality": (quality, 90),
        "sharpening": (sharpening, "screen_standard"),
        "collision": (collision, "suffix"),
    }
    for key, (actual, supported) in expected.items():
        if actual != supported:
            raise ValueError(f"首版只支持 jpeg_settings.{key}={supported!r}。")
    return {key: actual for key, (actual, _supported) in expected.items()}


def _safe_output_name(path: Path, used: set[str]) -> str:
    base = f"{path.stem}.jpg"
    candidate = base
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{path.stem}-{counter}.jpg"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def create_export_spec(
    data_dir: Path,
    *,
    run_id: str,
    input_root: Path,
    items: list[dict[str, Any]],
    xmp: bool,
    jpeg: bool,
    review_revision: int | None = None,
    develop_revision: int | None = None,
    develop_plan_id: str | None = None,
    source_snapshot_sha256: str | None = None,
    output_dir: Path | None = None,
    jpeg_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not xmp and not jpeg:
        raise ValueError("至少选择保存 XMP 或导出 JPEG。")
    input_root = Path(input_root).resolve()
    output_dir = Path(output_dir).resolve() if output_dir else input_root / "成片"
    jpeg_settings = normalize_jpeg_settings(jpeg_options)
    export_spec_id = f"export-{uuid.uuid4().hex}"
    now = _now()
    used_names: set[str] = set()
    spec_items: list[dict[str, Any]] = []
    for raw_item in items:
        source = Path(str(raw_item.get("path") or "")).resolve()
        targets: dict[str, Any] = {}
        if xmp:
            targets["xmp"] = {
                "status": "pending",
                "attempts": 0,
                "error": None,
                "output": None,
            }
        else:
            targets["xmp"] = {
                "status": "skipped",
                "attempts": 0,
                "error": None,
                "output": None,
            }
        if jpeg:
            filename = _safe_output_name(source, used_names)
            targets["jpeg"] = {
                "status": "pending",
                "attempts": 0,
                "error": None,
                "output": str(output_dir / filename),
            }
        else:
            targets["jpeg"] = {
                "status": "skipped",
                "attempts": 0,
                "error": None,
                "output": None,
            }
        spec_items.append(
            {
                "item_id": str(
                    raw_item.get("item_id") or raw_item.get("index") or len(spec_items)
                ),
                "path": str(source),
                "group_id": str(raw_item.get("group_id") or ""),
                "rating": int(raw_item.get("rating") or 0),
                "score": float(raw_item.get("score") or 0.0),
                "keywords": list(raw_item.get("keywords") or []),
                "recipe": dict(raw_item.get("develop") or raw_item.get("recipe") or {}),
                "source_fingerprint": dict(raw_item.get("source_fingerprint") or {}),
                "targets": targets,
            }
        )
    payload = {
        "schema_version": EXPORT_SPEC_SCHEMA_VERSION,
        "export_spec_id": export_spec_id,
        "run_id": run_id,
        "review_revision": review_revision,
        "develop_revision": develop_revision,
        "develop_plan_id": develop_plan_id,
        "source_snapshot_sha256": source_snapshot_sha256,
        "created_at": now,
        "updated_at": now,
        "status": "prepared",
        "input_root": str(input_root),
        "output_dir": str(output_dir),
        "targets": {"xmp": bool(xmp), "jpeg": bool(jpeg)},
        "jpeg_options": {
            "color_space": "sRGB",
            "resize": "original",
            "quality": 90,
            "output_sharpening": "screen-standard",
            "collision": "suffix",
        },
        "jpeg_settings": jpeg_settings,
        "items": spec_items,
        "executions": [],
    }
    write_json(export_spec_path(data_dir, export_spec_id), payload)
    return payload


def load_export_spec(data_dir: Path, export_spec_id: str) -> dict[str, Any]:
    path = export_spec_path(data_dir, export_spec_id)
    if not path.is_file():
        raise FileNotFoundError(f"导出任务不存在：{export_spec_id}")
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise TypeError("导出任务格式无效。")
    return payload


def pending_export_work(
    spec: dict[str, Any], *, retry_failed: bool = False
) -> list[dict[str, Any]]:
    statuses = {"failed"} if retry_failed else {"pending"}
    work: list[dict[str, Any]] = []
    for item in spec.get("items", []):
        for target in ("xmp", "jpeg"):
            state = (item.get("targets") or {}).get(target) or {}
            if state.get("status") in statuses:
                work.append(
                    {
                        "item_id": item.get("item_id"),
                        "path": item.get("path"),
                        "group_id": item.get("group_id"),
                        "recipe": item.get("recipe") or {},
                        "target": target,
                        "output": state.get("output"),
                    }
                )
    return work


def _creative_preset(recipe: dict[str, Any]) -> str | None:
    creative = recipe.get("creative_style")
    if not isinstance(creative, dict):
        return None
    if str(creative.get("status", "")).casefold() not in {"confirmed", "enabled"}:
        return None
    preset = creative.get("preset_uuid") or creative.get("preset_id")
    return str(preset) if preset not in {None, ""} else None


def create_export_attempt(
    data_dir: Path,
    spec: dict[str, Any],
    *,
    retry_failed_only: bool = True,
    force_lightroom: bool = False,
) -> dict[str, Any] | None:
    """Freeze the next coherent execution without replaying successful work.

    Lightroom's export command has one output mode per batch.  When a partial
    failure leaves different XMP and JPEG item sets, the next attempt therefore
    handles one target only.  A subsequent call handles the other set.
    """

    if any(
        execution.get("status") in {"queued", "running", "cancelling"}
        for execution in spec.get("executions", [])
    ):
        raise RuntimeError("这个导出已有任务正在运行。")
    pending = pending_export_work(spec)
    failed = pending_export_work(spec, retry_failed=True)
    work = (pending or failed) if retry_failed_only else [*pending, *failed]
    if not work:
        return None

    xmp_ids = {str(item["item_id"]) for item in work if item["target"] == "xmp"}
    jpeg_ids = {str(item["item_id"]) for item in work if item["target"] == "jpeg"}
    if xmp_ids and xmp_ids == jpeg_ids:
        mode = "both"
        selected_ids = xmp_ids
    elif xmp_ids:
        mode = "xmp"
        selected_ids = xmp_ids
    else:
        mode = "jpeg"
        selected_ids = jpeg_ids

    by_id = {str(item.get("item_id")): item for item in spec.get("items", [])}
    selected_items = [by_id[item_id] for item_id in selected_ids if item_id in by_id]
    selected_items.sort(key=lambda item: str(item.get("item_id")))
    if not selected_items:
        raise ValueError("导出规格没有可执行的照片。")

    lightroom_required = force_lightroom or mode != "xmp" or any(
        Path(str(item["path"])).with_suffix(".xmp").is_file()
        or _creative_preset(dict(item.get("recipe") or {})) is not None
        for item in selected_items
    )
    engine = "lightroom" if lightroom_required else "direct_xmp"
    attempt_id = f"attempt-{uuid.uuid4().hex}"
    root = export_attempt_root(data_dir, str(spec["export_spec_id"]), attempt_id)
    root.mkdir(parents=True, exist_ok=False)
    enabled_targets = {
        "xmp": mode in {"xmp", "both"},
        "jpeg": mode in {"jpeg", "both"},
    }
    frozen_items = [
        {
            "item_id": str(item["item_id"]),
            "path": str(item["path"]),
            "group_id": str(item.get("group_id") or ""),
            "rating": int(item.get("rating") or 0),
            "score": float(item.get("score") or 0.0),
            "keywords": list(item.get("keywords") or []),
            "excluded": False,
            "develop": dict(item.get("recipe") or {}),
        }
        for item in selected_items
    ]
    attempt_spec = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "export_spec_id": spec["export_spec_id"],
        "run_id": spec["run_id"],
        "created_at": _now(),
        "engine": engine,
        "output_mode": mode,
        "targets": enabled_targets,
        "output_dir": spec["output_dir"],
        "jpeg_settings": dict(spec.get("jpeg_settings") or normalize_jpeg_settings()),
        "items": frozen_items,
    }
    attempt_spec_path = root / "spec.json"
    write_json(attempt_spec_path, attempt_spec)
    results_path = root / "results.json"
    if engine == "direct_xmp":
        write_json(
            results_path,
            {
                "run_id": spec["run_id"],
                "input_root": spec["input_root"],
                "review_revision": spec.get("review_revision"),
                "develop_revision": spec.get("develop_revision"),
                "results": frozen_items,
            },
        )
    work_rows = [
        {"item_id": str(item["item_id"]), "target": target}
        for item in selected_items
        for target in ("xmp", "jpeg")
        if enabled_targets[target]
    ]
    execution = {
        "attempt_id": attempt_id,
        "created_at": attempt_spec["created_at"],
        "updated_at": attempt_spec["created_at"],
        "status": "prepared",
        "engine": engine,
        "output_mode": mode,
        "spec_path": str(attempt_spec_path),
        "results_path": str(results_path) if engine == "direct_xmp" else None,
        "job_id": None,
        "batch_id": None,
        "work": work_rows,
    }
    spec.setdefault("executions", []).append(execution)
    save_export_spec(data_dir, spec)
    return execution


def activate_export_attempt(
    data_dir: Path,
    spec: dict[str, Any],
    *,
    attempt_id: str,
    job_id: str,
    batch_id: str | None = None,
) -> dict[str, Any]:
    execution = next(
        item for item in spec.get("executions", []) if item.get("attempt_id") == attempt_id
    )
    execution.update(
        status="queued",
        job_id=job_id,
        batch_id=batch_id,
        updated_at=_now(),
    )
    by_id = {str(item.get("item_id")): item for item in spec.get("items", [])}
    for work in execution.get("work", []):
        state = by_id[str(work["item_id"])]["targets"][str(work["target"])]
        state["status"] = "running"
        state["attempts"] = int(state.get("attempts") or 0) + 1
        state["error"] = None
        state["updated_at"] = _now()
    _refresh_status(spec)
    save_export_spec(data_dir, spec)
    return execution


def abandon_export_attempt(
    data_dir: Path,
    spec: dict[str, Any],
    *,
    attempt_id: str,
    error: str,
) -> None:
    execution = next(
        item for item in spec.get("executions", []) if item.get("attempt_id") == attempt_id
    )
    execution.update(status="abandoned", error=str(error), updated_at=_now())
    _refresh_status(spec)
    save_export_spec(data_dir, spec)


def finish_export_attempt(
    data_dir: Path,
    spec: dict[str, Any],
    *,
    attempt_id: str,
    status: str,
    error: str | None = None,
) -> None:
    execution = next(
        item for item in spec.get("executions", []) if item.get("attempt_id") == attempt_id
    )
    execution.update(status=status, error=error, updated_at=_now())
    _refresh_status(spec)
    save_export_spec(data_dir, spec)


def _refresh_status(spec: dict[str, Any]) -> None:
    states = [
        target.get("status")
        for item in spec.get("items", [])
        for target in (item.get("targets") or {}).values()
    ]
    active = [state for state in states if state != "skipped"]
    if not active or all(state in _TERMINAL for state in active):
        spec["status"] = "complete"
    elif any(state == "running" for state in active):
        spec["status"] = "running"
    elif any(state == "failed" for state in active):
        spec["status"] = "partial_failure"
    else:
        spec["status"] = "prepared"


def record_export_result(
    data_dir: Path,
    spec: dict[str, Any],
    *,
    item_id: str,
    target: ExportTarget,
    succeeded: bool,
    output: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    item = next(
        (
            value
            for value in spec.get("items", [])
            if str(value.get("item_id")) == str(item_id)
        ),
        None,
    )
    if item is None:
        raise KeyError(f"导出照片不存在：{item_id}")
    state = (item.get("targets") or {}).get(target)
    if not isinstance(state, dict) or state.get("status") == "skipped":
        raise ValueError(f"该照片未启用 {target.upper()} 输出。")
    if state.get("status") != "running":
        state["attempts"] = int(state.get("attempts") or 0) + 1
    state["status"] = "succeeded" if succeeded else "failed"
    state["error"] = None if succeeded else (str(error or "未知错误"))
    if output is not None:
        state["output"] = output
    state["updated_at"] = _now()
    _refresh_status(spec)
    save_export_spec(data_dir, spec)
    return state


def export_summary(spec: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, dict[str, int]] = {
        "xmp": {"pending": 0, "running": 0, "succeeded": 0, "failed": 0, "skipped": 0},
        "jpeg": {"pending": 0, "running": 0, "succeeded": 0, "failed": 0, "skipped": 0},
    }
    for item in spec.get("items", []):
        for target in ("xmp", "jpeg"):
            status = str(
                ((item.get("targets") or {}).get(target) or {}).get("status")
                or "pending"
            )
            summary[target][status] = summary[target].get(status, 0) + 1
    return {
        "status": spec.get("status"),
        "targets": summary,
        "items": len(spec.get("items", [])),
    }
