from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image

from .constants import READABLE_RAW_EXTENSIONS
from .creative_lut import ORDINARY_XMP_LUT_LIMITATION, CreativeLutEngine
from .features import technical_features
from .general_aesthetic import GeneralAestheticScorer
from .lightroom_apply import (
    LightroomApplyError,
    resolve_lightroom_executable,
    start_lightroom,
    wait_for_lightroom_batch,
    wait_for_lightroom_online,
)
from .lightroom_bridge import (
    PLUGIN_VERSION,
    LightroomTask,
    bridge_paths,
    create_lightroom_batch,
    create_lightroom_preset_enumeration,
    read_lightroom_batch_status,
    read_lightroom_bridge_status,
    read_lightroom_preset_listing,
    write_lightroom_look_descriptor,
    write_lightroom_plugin_config,
)
from .progress import emit_progress as emit_cli_progress
from .style_ai import run_style_ai_cascade
from .style_library import (
    load_style_index,
    managed_style_root,
    registration_state_path,
    runtime_supports_amount,
    style_index_path,
    sync_style_library,
)
from .style_recommendation import (
    RECOMMENDATION_WEIGHTS,
    build_render_tasks,
    canonical_pool_entries,
    create_recommendation_plan,
    load_recommendation_plan,
    preview_cache_key,
    rank_rendered_candidates,
    scene_preset_affinity,
)
from .util import cache_key as source_cache_key
from .util import read_json, write_json

STYLE_PREVIEW_DIRECTORY = "style-previews"
STYLE_RECOMMENDATION_PATH = "style-recommendations.json"
STYLE_REQUEST_CACHE_DIRECTORY = "style-request-cache"
RUNTIME_PRESET_SNAPSHOT = "lightroom-runtime-presets.json"
REQUIRED_PREVIEW_PLUGIN_VERSION = "0.3.8"
RUNTIME_PRESET_CACHE_SECONDS = 300.0
RUNTIME_PRESET_ENUMERATION_TIMEOUT = 90.0
NEUTRAL_PRESET_ID = "__lightroom_natural__"
NEUTRAL_PRESET_HASH = "lightroom-auto-v1"
RENDER_CANDIDATE_LIMIT = 3
LOOK_RENDERER_VERSION = "creative-look-v2"
_CACHE_KEY = re.compile(r"^[0-9a-f]{64}$")
_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class StyleWorkerError(ValueError):
    """A user-actionable failure in the exact-preview recommendation worker."""


class AestheticScorer(Protocol):
    def score(self, paths: Sequence[Path]) -> list[dict[str, Any]]: ...

    def release(self) -> None: ...


ProgressCallback = Callable[[dict[str, Any]], None]
BridgeStatusReader = Callable[[Path | str], dict[str, Any]]
BatchCreator = Callable[..., dict[str, Any]]
BatchStatusReader = Callable[[Path | str, str], dict[str, Any]]
BatchWaiter = Callable[..., dict[str, Any]]
ScorerFactory = Callable[[Path], AestheticScorer]
CascadeRunner = Callable[..., dict[str, Any]]
LutEngineFactory = Callable[[Path], CreativeLutEngine]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _notify(
    progress: ProgressCallback | None,
    phase: str,
    label: str,
    completed: int,
    total: int,
) -> None:
    if progress is None:
        emit_cli_progress(phase, label, completed, total, unit="项")
        return
    progress(
        {
            "status": "running",
            "phase": phase,
            "stage_label": label,
            "completed": completed,
            "current": completed,
            "total": total,
            "overall_percent": round(100.0 * completed / max(1, total), 1),
            "updated_at": _now(),
        }
    )


def style_preview_path(run_dir: Path | str, cache_key: str) -> Path:
    """Resolve one immutable preview cache target below the run directory."""

    key = str(cache_key).casefold()
    if not _CACHE_KEY.fullmatch(key):
        raise ValueError("风格预览缓存键无效。")
    root = Path(run_dir).expanduser().resolve() / STYLE_PREVIEW_DIRECTORY
    return root / f"{key}.jpg"


def resolve_style_preview(run_dir: Path | str, cache_key: str) -> Path:
    """Return a verified cached JPEG suitable for a guarded FileResponse."""

    target = style_preview_path(run_dir, cache_key)
    root = target.parent.resolve()
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError(f"风格预览不存在：{cache_key}") from exc
    if not _inside(resolved, root) or not _valid_jpeg(resolved):
        raise FileNotFoundError(f"风格预览无效：{cache_key}")
    return resolved


def require_lightroom_preview_plugin(
    data_dir: Path | str,
    *,
    status_reader: BridgeStatusReader = read_lightroom_bridge_status,
) -> dict[str, Any]:
    """Require an online bridge that advertises the exact preview protocol."""

    if PLUGIN_VERSION != REQUIRED_PREVIEW_PLUGIN_VERSION:
        raise StyleWorkerError(
            "Python 桥接版本与风格预览协议不一致，请重新安装当前版本。"
        )
    status = status_reader(data_dir)
    heartbeat = status.get("heartbeat") if isinstance(status, Mapping) else None
    if not isinstance(heartbeat, Mapping) or heartbeat.get("state") != "online":
        raise StyleWorkerError(
            "Lightroom 桥接未在线，请先打开 Lightroom Classic 并启用照片选片插件。"
        )
    active_version = str(heartbeat.get("plugin_version") or "")
    if active_version != REQUIRED_PREVIEW_PLUGIN_VERSION:
        shown = active_version or "未知"
        raise StyleWorkerError(
            "Lightroom 当前加载的插件版本为 "
            f"{shown}，真实风格预览要求 {REQUIRED_PREVIEW_PLUGIN_VERSION}；"
            "请在插件管理器中重新加载插件。"
        )
    return dict(status)


def ensure_lightroom_preview_plugin(
    data_dir: Path | str,
    *,
    lightroom_exe: Path | str | None = None,
    startup_timeout: float = 90.0,
    status_reader: BridgeStatusReader = read_lightroom_bridge_status,
) -> dict[str, Any]:
    """Configure/start Lightroom when needed, then enforce the required bridge protocol.

    An injected status reader is treated as a test/orchestrator boundary: the
    worker never launches a desktop process behind that injection.
    """

    status = status_reader(data_dir)
    heartbeat = status.get("heartbeat") if isinstance(status, Mapping) else None
    if isinstance(heartbeat, Mapping) and heartbeat.get("state") == "online":
        return require_lightroom_preview_plugin(data_dir, status_reader=status_reader)
    if status_reader is not read_lightroom_bridge_status:
        return require_lightroom_preview_plugin(data_dir, status_reader=status_reader)
    write_lightroom_plugin_config(data_dir)
    executable = resolve_lightroom_executable(lightroom_exe)
    start_lightroom(executable)
    wait_for_lightroom_online(
        data_dir,
        startup_timeout,
        status_reader=status_reader,
    )
    return require_lightroom_preview_plugin(data_dir, status_reader=status_reader)


def _valid_jpeg(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 128:
        return False
    try:
        with Image.open(path) as image:
            image.verify()
            return image.format == "JPEG"
    except (OSError, ValueError):
        return False


def _read_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except (OSError, TypeError, ValueError) as exc:
        raise StyleWorkerError(f"无法读取{label}：{path}") from exc
    if not isinstance(payload, dict):
        raise StyleWorkerError(f"{label}格式无效：{path}")
    return payload


def _load_run_state(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    develop = _read_mapping(run_dir / "develop.json", "裁剪调色方案")
    results = _read_mapping(run_dir / "results.json", "选片结果")
    return develop, results


def _registration_state_needs_refresh(
    data_dir: Path,
    catalog: Mapping[str, Any],
) -> bool:
    """Detect a Lightroom registration result not yet reflected in index.json."""

    state_path = registration_state_path(data_dir)
    if not state_path.is_file():
        return False
    try:
        state = read_json(state_path)
    except (OSError, TypeError, ValueError):
        # Never replace a valid index from a partially written/external file.
        return False
    if not isinstance(state, Mapping):
        return False
    rows = state.get("entries") or state.get("presets") or []
    if not isinstance(rows, list):
        return False
    registered = {
        (str(row.get("preset_id") or ""), str(row.get("file_hash") or "")): row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("status") == "registered"
        and row.get("preset_id")
        and row.get("file_hash")
        and _BATCH_ID.fullmatch(str(row.get("plugin_uuid") or ""))
    }
    managed = {
        (str(entry.get("preset_id") or ""), str(entry.get("file_hash") or "")): entry
        for entry in catalog.get("entries", [])
        if isinstance(entry, Mapping)
        and entry.get("source_kind") == "managed"
        and entry.get("preset_id")
        and entry.get("file_hash")
        and str(entry.get("source_tier") or "").casefold() != "local_only_proprietary"
    }
    for key, entry in managed.items():
        state_entry = registered.get(key)
        if state_entry is None:
            if entry.get("registration_status") == "registered":
                return True
            continue
        if (
            entry.get("registration_status") != "registered"
            or str(entry.get("runtime_preset_uuid") or "")
            != str(state_entry.get("plugin_uuid") or "")
            or str(entry.get("preset_scope") or "")
            != str(state_entry.get("scope") or "plugin")
        ):
            return True
    try:
        return (
            bool(registered)
            and state_path.stat().st_mtime_ns
            > style_index_path(data_dir).stat().st_mtime_ns
        )
    except OSError:
        return bool(registered)


def _load_runtime_style_catalog(data_dir: Path) -> dict[str, Any]:
    """Load the index, incorporating newly confirmed Lightroom registrations."""

    catalog = load_style_index(data_dir)
    if not _registration_state_needs_refresh(data_dir, catalog):
        return _prefer_creative_profiles(catalog)
    try:
        return _prefer_creative_profiles(sync_style_library(data_dir))
    except (OSError, TypeError, ValueError) as exc:
        raise StyleWorkerError(f"无法刷新 Lightroom 预设注册状态：{exc}") from exc


def _prefer_creative_profiles(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Use Lightroom Creative Profiles as the default look pool when present."""

    result = dict(catalog)
    entries = [
        dict(item) for item in catalog.get("entries", []) if isinstance(item, Mapping)
    ]
    profiles = [
        str(item.get("preset_id"))
        for item in entries
        if item.get("preset_id")
        and item.get("look_kind") == "lightroom_profile"
        and item.get("ai_eligible")
        and item.get("compatibility") == "compatible"
        and item.get("xmp_compatible")
        and not item.get("duplicate_of")
        and not item.get("hidden")
    ]
    result["entries"] = entries
    if profiles:
        result["default_pool"] = profiles
        result["candidate_mode"] = "creative_profiles"
    else:
        result["candidate_mode"] = "legacy_presets"
    return result


def _runtime_preset_snapshot_path(data_dir: Path) -> Path:
    return managed_style_root(data_dir) / RUNTIME_PRESET_SNAPSHOT


def _runtime_path_key(value: Any) -> str:
    return str(value or "").strip().replace("/", "\\").casefold()


def _runtime_path_suffix(value: Any) -> tuple[str, ...]:
    parts = [part for part in _runtime_path_key(value).split("\\") if part]
    return tuple(parts[-3:]) if len(parts) >= 3 else ()


def _apply_runtime_preset_listing(
    catalog: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Gate the style catalog by presets the active Lightroom SDK enumerated.

    Filesystem discovery only proves that a preset file exists.  Lightroom can
    still hide bundled/profile-specific presets from ``developPresetByUuid``.
    The bridge listing is therefore authoritative for the runtime UUID/scope
    used by preview and export jobs.
    """

    clean_records = [
        dict(item)
        for item in records
        if isinstance(item, Mapping)
        and str(item.get("scope") or "") in {"catalog", "plugin"}
        and str(item.get("uuid") or "")
    ]
    exact: dict[tuple[str, str], dict[str, Any]] = {}
    by_file: dict[str, list[dict[str, Any]]] = {}
    by_suffix: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    by_catalog_name: dict[str, list[dict[str, Any]]] = {}
    by_plugin_name: dict[str, list[dict[str, Any]]] = {}
    for record in clean_records:
        scope = str(record["scope"])
        runtime_uuid = str(record["uuid"])
        exact[(scope, runtime_uuid.casefold())] = record
        file_key = _runtime_path_key(record.get("file"))
        if scope == "catalog" and file_key:
            by_file.setdefault(file_key, []).append(record)
            suffix = _runtime_path_suffix(record.get("file"))
            if suffix:
                by_suffix.setdefault(suffix, []).append(record)
        name_key = str(record.get("name") or "").strip().casefold()
        if name_key:
            if scope == "catalog":
                by_catalog_name.setdefault(name_key, []).append(record)
            else:
                by_plugin_name.setdefault(name_key, []).append(record)

    entries: list[dict[str, Any]] = []
    for raw in catalog.get("entries", []):
        if not isinstance(raw, Mapping):
            continue
        entry = dict(raw)
        status = str(entry.get("registration_status") or "")
        if entry.get("look_kind") == "lightroom_profile":
            # Creative Profiles are applied as CameraProfile/ProfileAmount,
            # not resolved through developPresetByUuid.
            entry["runtime_resolvable"] = bool(
                status in {"installed", "local_copy"}
                and entry.get("profile_name")
                and entry.get("xmp_compatible")
                and entry.get("compatibility") == "compatible"
            )
            if not entry["runtime_resolvable"]:
                entry["ai_eligible"] = False
            entries.append(entry)
            continue
        scope = str(entry.get("preset_scope") or "catalog")
        runtime_uuid = str(entry.get("runtime_preset_uuid") or "")
        match = exact.get((scope, runtime_uuid.casefold())) if runtime_uuid else None
        if match is None and scope == "catalog":
            path_matches = by_file.get(_runtime_path_key(entry.get("path")), [])
            if len(path_matches) == 1:
                match = path_matches[0]
        if match is None and scope == "catalog":
            suffix_matches = by_suffix.get(_runtime_path_suffix(entry.get("path")), [])
            if len(suffix_matches) == 1:
                match = suffix_matches[0]
        if match is None and scope == "catalog":
            # Lightroom may expose a built-in preset from CameraRaw's runtime
            # mirror using a generated SDK UUID rather than the UUID stored in
            # Adobe's installation copy.  A globally unique name is a safe
            # final mapping when neither UUID nor path suffix is shared.
            name_key = str(entry.get("name") or "").strip().casefold()
            name_matches = by_catalog_name.get(name_key, [])
            if len(name_matches) == 1:
                match = name_matches[0]
        if match is None and scope == "plugin":
            plugin_name = str(entry.get("plugin_name") or "").strip().casefold()
            name_matches = by_plugin_name.get(plugin_name, [])
            if len(name_matches) == 1:
                match = name_matches[0]

        is_profile = str(entry.get("look_kind") or "") == "lightroom_profile"
        eligible_runtime_status = status in {"installed", "registered"}
        # Creative Profiles are resolved by CameraProfile name through
        # applyDevelopSettings(), not by developPresetByUuid().  Lightroom's
        # preset-enumeration SDK intentionally does not list them.
        resolvable = bool(
            eligible_runtime_status
            and (
                bool(str(entry.get("profile_name") or "").strip())
                if is_profile
                else match is not None
            )
        )
        entry["runtime_resolvable"] = resolvable
        if resolvable and not is_profile:
            entry["runtime_preset_uuid"] = str(match["uuid"])
            entry["preset_scope"] = str(match["scope"])
            entry["runtime_preset_name"] = str(match.get("name") or "")
            entry["runtime_preset_folder"] = str(match.get("folder") or "")
            entry["runtime_preset_file"] = str(match.get("file") or "")
        elif not resolvable:
            # Do not rewrite registration_status: "installed" remains useful
            # provenance.  It simply is not evidence of SDK resolvability.
            entry["ai_eligible"] = False
        entries.append(entry)

    valid_ids = {
        str(entry.get("preset_id") or "")
        for entry in entries
        if entry.get("runtime_resolvable")
        and entry.get("ai_eligible")
        and not entry.get("duplicate_of")
        and not entry.get("hidden")
    }
    result = dict(catalog)
    result["entries"] = entries
    result["default_pool"] = [
        str(preset_id)
        for preset_id in catalog.get("default_pool", [])
        if str(preset_id) in valid_ids
    ]
    result["runtime_presets"] = {
        "verified_at": _now(),
        "enumerated": len(clean_records),
        "resolvable_entries": sum(
            bool(entry.get("runtime_resolvable")) for entry in entries
        ),
        "ai_pool": len(result["default_pool"]),
    }
    return _prefer_creative_profiles(result)


def _read_runtime_preset_snapshot(
    data_dir: Path,
    *,
    max_age_seconds: float = RUNTIME_PRESET_CACHE_SECONDS,
) -> list[dict[str, Any]] | None:
    path = _runtime_preset_snapshot_path(data_dir)
    try:
        if time.time() - path.stat().st_mtime > max_age_seconds:
            return None
        payload = read_json(path)
    except (OSError, TypeError, ValueError):
        return None
    if (
        not isinstance(payload, Mapping)
        or payload.get("plugin_version") != REQUIRED_PREVIEW_PLUGIN_VERSION
        or not isinstance(payload.get("entries"), list)
    ):
        return None
    return [dict(item) for item in payload["entries"] if isinstance(item, Mapping)]


def _enumerate_runtime_presets(
    data_dir: Path,
    *,
    batch_creator: Callable[..., dict[str, Any]] = create_lightroom_preset_enumeration,
    batch_waiter: Callable[..., dict[str, Any]] = wait_for_lightroom_batch,
    listing_reader: Callable[
        [Path | str], list[dict[str, Any]]
    ] = read_lightroom_preset_listing,
) -> list[dict[str, Any]]:
    """Ask the active plug-in for the exact presets its resolver can address."""

    batch_id = f"style-presets-{uuid.uuid4().hex[:16]}"
    batch_creator(data_dir, batch_id=batch_id, task_id="presets")
    try:
        status = batch_waiter(
            data_dir,
            batch_id,
            RUNTIME_PRESET_ENUMERATION_TIMEOUT,
            progress_phase="presets",
            progress_label="核对 Lightroom 可用预设",
            progress_unit="项",
            progress_total=1,
        )
    except Exception as exc:
        raise StyleWorkerError(f"Lightroom 可用预设核对失败：{exc}") from exc
    rows = [item for item in status.get("tasks", []) if isinstance(item, Mapping)]
    row = rows[0] if len(rows) == 1 else None
    result = row.get("result") if isinstance(row, Mapping) else None
    listing_value = (
        result.get("preset_list_path") if isinstance(result, Mapping) else None
    )
    if (
        status.get("status") != "complete"
        or not isinstance(row, Mapping)
        or row.get("status") != "done"
        or not isinstance(result, Mapping)
        or result.get("preset_status") != "done"
        or not listing_value
    ):
        raise StyleWorkerError("Lightroom 没有返回完整的可用预设清单。")
    try:
        listing_path = Path(str(listing_value)).resolve(strict=True)
        preset_root = bridge_paths(data_dir).presets.resolve(strict=True)
    except OSError as exc:
        raise StyleWorkerError("Lightroom 返回的预设清单不存在。") from exc
    if not _inside(listing_path, preset_root):
        raise StyleWorkerError("Lightroom 返回的预设清单超出桥接缓存目录。")
    try:
        records = listing_reader(listing_path)
    except (OSError, TypeError, ValueError) as exc:
        raise StyleWorkerError("Lightroom 返回的预设清单无法解析。") from exc
    snapshot = {
        "schema_version": 1,
        "generated_at": _now(),
        "plugin_version": REQUIRED_PREVIEW_PLUGIN_VERSION,
        "batch_id": batch_id,
        "entries": records,
    }
    write_json(_runtime_preset_snapshot_path(data_dir), snapshot)
    return records


def _selected_crop(item: Mapping[str, Any]) -> dict[str, float]:
    crop_id = str(item.get("crop_id") or "")
    selected = next(
        (
            value
            for value in item.get("crop_candidates", [])
            if isinstance(value, Mapping) and str(value.get("id") or "") == crop_id
        ),
        None,
    )
    bounds = selected.get("bounds") if isinstance(selected, Mapping) else None
    if not isinstance(bounds, Mapping):
        bounds = {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
    crop = {
        "left": float(bounds.get("left", 0.0)),
        "top": float(bounds.get("top", 0.0)),
        "right": float(bounds.get("right", 1.0)),
        "bottom": float(bounds.get("bottom", 1.0)),
        "angle": float(item.get("angle", 0.0)),
    }
    if not (
        0.0 <= crop["left"] < crop["right"] <= 1.0
        and 0.0 <= crop["top"] < crop["bottom"] <= 1.0
        and -45.0 <= crop["angle"] <= 45.0
    ):
        raise StyleWorkerError(f"{item.get('filename', '照片')} 的构图范围无效。")
    return crop


def _base_style(item: Mapping[str, Any]) -> dict[str, Any]:
    base = item.get("base") if isinstance(item.get("base"), Mapping) else {}
    handled = {"AutoTone", "WhiteBalance", "AutoLateralCA", "LensProfileEnable"}
    return {
        str(key): value
        for key, value in base.items()
        if key not in handled and isinstance(value, (bool, int, float, str))
    }


def _stable_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def _base_hash(item: Mapping[str, Any]) -> str:
    payload = {
        "crop": _selected_crop(item),
        "base": dict(item.get("base") or {}),
    }
    return _stable_hash(payload)


def _render_revisions(
    develop: Mapping[str, Any], item: Mapping[str, Any]
) -> tuple[str, str]:
    """Return content revisions for only the stages that affect LR pixels.

    The overall develop revision also changes when a recommendation is saved.
    Using it directly would invalidate an otherwise exact preview on every
    repeated request.  These two hashes change only with crop or basic color.
    """

    crop_revision = _stable_hash(
        {
            "workflow": dict(develop.get("crop") or {}),
            "selected": _selected_crop(item),
        }
    )
    basic_color_revision = _stable_hash(
        {
            "enabled": str((develop.get("basic_color") or {}).get("status"))
            != "skipped",
            "settings": dict(item.get("base") or {}),
        }
    )
    return crop_revision, basic_color_revision


def _runtime_identity(
    bridge_status: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, str]:
    heartbeat = (
        bridge_status.get("heartbeat")
        if isinstance(bridge_status.get("heartbeat"), Mapping)
        else {}
    )
    entries = [
        {
            "preset_id": str(entry.get("preset_id") or ""),
            "file_hash": str(entry.get("file_hash") or ""),
            "look_descriptor_hash": str(entry.get("look_descriptor_hash") or ""),
            "runtime_uuid": str(entry.get("runtime_preset_uuid") or ""),
            "scope": str(entry.get("preset_scope") or "catalog"),
        }
        for entry in canonical_pool_entries(dict(catalog))
    ]
    entries.sort(key=lambda value: (value["preset_id"], value["file_hash"]))
    return {
        "plugin_version": str(
            heartbeat.get("plugin_version") or REQUIRED_PREVIEW_PLUGIN_VERSION
        ),
        "lightroom_version": str(heartbeat.get("lightroom_version") or "unknown"),
        "catalog_version": _stable_hash(
            {
                "catalog_path": str(
                    heartbeat.get("catalog_path") or "unknown"
                ).casefold(),
                "style_schema": catalog.get("schema_version"),
                "entries": entries,
            }
        ),
        "look_renderer_version": LOOK_RENDERER_VERSION,
    }


def _recommendation_request_key(
    develop: Mapping[str, Any],
    groups: Mapping[str, Sequence[Mapping[str, Any]]],
    catalog: Mapping[str, Any],
    *,
    scope: str,
    group_id: int | None,
    amount: int,
    runtime: Mapping[str, str],
) -> str:
    source_rows: list[dict[str, Any]] = []
    items_by_path: dict[str, Mapping[str, Any]] = {}
    for item in develop.get("items", []):
        if not isinstance(item, Mapping) or not item.get("path"):
            continue
        items_by_path[
            str(Path(str(item["path"])).expanduser().resolve()).casefold()
        ] = item
    for request_group_id, items in groups.items():
        for item in items:
            path = Path(str(item.get("path") or "")).resolve(strict=True)
            develop_item = items_by_path[str(path).casefold()]
            crop_revision, basic_revision = _render_revisions(develop, develop_item)
            source_rows.append(
                {
                    "group_id": str(request_group_id) if scope == "group" else None,
                    "path": str(path).casefold(),
                    "source_fingerprint": source_cache_key(path),
                    "crop_revision": crop_revision,
                    "basic_color_revision": basic_revision,
                }
            )
    source_rows.sort(key=lambda value: value["path"])
    candidates = sorted(
        (
            str(entry.get("preset_id") or ""),
            str(entry.get("file_hash") or ""),
            str(entry.get("look_descriptor_hash") or ""),
        )
        for entry in canonical_pool_entries(dict(catalog))
    )
    return _stable_hash(
        {
            "scope": scope,
            "group_id": group_id,
            "amount": int(amount),
            "sources": source_rows,
            "candidates": candidates,
            "runtime": dict(runtime),
            "pipeline": "style-request-v6",
        }
    )


def _group_inputs(
    develop: Mapping[str, Any],
    results: Mapping[str, Any],
    *,
    group_id: int | None,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[tuple[str, str], dict[str, Any]],
]:
    result_items = (
        results.get("results") if isinstance(results.get("results"), list) else []
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    scenes: dict[str, dict[str, Any]] = {}
    develop_items: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_item in develop.get("items", []):
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        current_group = int(item.get("group_id", -1))
        if current_group < 0 or (group_id is not None and current_group != group_id):
            continue
        index = int(item.get("index", -1))
        result = (
            result_items[index]
            if 0 <= index < len(result_items) and isinstance(result_items[index], dict)
            else {}
        )
        raw_path = Path(str(item.get("path") or result.get("path") or "")).expanduser()
        if not raw_path.is_absolute():
            raise StyleWorkerError(f"照片不是绝对路径：{raw_path}")
        try:
            raw_path = raw_path.resolve(strict=True)
        except OSError as exc:
            raise StyleWorkerError(f"原始照片不存在：{raw_path}") from exc
        if raw_path.suffix.casefold() not in READABLE_RAW_EXTENSIONS:
            raise StyleWorkerError(f"风格预览只接受原始 RAW：{raw_path.name}")
        group_key = str(current_group)
        enriched = {
            **result,
            **item,
            "path": str(raw_path),
            "technical": dict(result.get("technical") or {}),
        }
        groups.setdefault(group_key, []).append(enriched)
        develop_items[(group_key, str(raw_path).casefold())] = item
        smart = item.get("smart_crop")
        if isinstance(smart, Mapping):
            scene = scenes.setdefault(
                group_key,
                {
                    "scene_type": smart.get("scene_type"),
                    "composition": smart.get("composition"),
                    "preferred_aspect": smart.get("preferred_aspect"),
                    "summaries": [],
                    "engines": smart.get("engines") or {},
                },
            )
            summary = str(smart.get("summary") or "").strip()
            if summary and summary not in scene["summaries"]:
                scene["summaries"].append(summary)
    if not groups:
        raise StyleWorkerError("当前没有可生成风格预览的照片组。")
    return groups, scenes, develop_items


def _runtime_entry(
    catalog: Mapping[str, Any],
    preset_id: str,
    preset_hash: str | None,
) -> dict[str, Any]:
    if not preset_hash:
        raise StyleWorkerError("选择的预设必须包含精确文件哈希。")
    matches = [
        dict(item)
        for item in catalog.get("entries", [])
        if isinstance(item, dict)
        and str(item.get("preset_id") or "") == str(preset_id)
        and str(item.get("file_hash") or "") == preset_hash
        and not item.get("duplicate_of")
        and not item.get("hidden")
        and item.get("compatibility", "compatible") == "compatible"
        and (
            item.get("registration_status", "installed") in {"installed", "registered"}
            or (
                item.get("look_kind") == "lightroom_profile"
                and item.get("registration_status") == "local_copy"
            )
        )
        and item.get("runtime_resolvable") is not False
    ]
    matches.sort(
        key=lambda item: (
            not bool(item.get("ai_eligible")),
            not bool(item.get("runtime_preset_uuid")),
            str(item.get("path") or "").casefold(),
        )
    )
    if not matches:
        raise StyleWorkerError("选择的预设当前不能由 Lightroom 解析。")
    entry = matches[0]
    look_kind = str(entry.get("look_kind") or "lightroom_preset")
    if look_kind == "lightroom_profile":
        descriptor = entry.get("look_descriptor")
        descriptor_hash = str(entry.get("look_descriptor_hash") or "")
        look_uuid = str(entry.get("uuid") or "")
        if (
            not isinstance(descriptor, Mapping)
            or not descriptor_hash
            or str(descriptor.get("Hash") or "") != descriptor_hash
            or not look_uuid
        ):
            raise StyleWorkerError("选择的 Creative Look 缺少完整的安全描述符。")
        return entry
    runtime_uuid = entry.get("runtime_preset_uuid")
    if not runtime_uuid:
        raise StyleWorkerError("选择的预设缺少 Lightroom 运行时 UUID。")
    if str(entry.get("file_hash") or "") != preset_hash:
        raise StyleWorkerError("预设版本已经变化，请重新选择。")
    return entry


def _candidate_payload(
    entry: Mapping[str, Any], scene: Mapping[str, Any]
) -> dict[str, Any]:
    is_creative_look = (
        str(entry.get("look_kind") or "lightroom_preset") == "lightroom_profile"
    )
    look_fields = (
        {
            "look_descriptor": dict(entry["look_descriptor"])
            if isinstance(entry.get("look_descriptor"), Mapping)
            else None,
            "look_descriptor_hash": entry.get("look_descriptor_hash"),
            "look_uuid": entry.get("uuid"),
        }
        if is_creative_look
        else {}
    )
    return {
        "preset_id": entry["preset_id"],
        "preset_hash": entry["file_hash"],
        "name": entry.get("name"),
        "category": entry.get("category"),
        "source": entry.get("source"),
        "preset_uuid": None
        if is_creative_look
        else entry.get("runtime_preset_uuid") or entry.get("uuid"),
        "preset_scope": entry.get("preset_scope") or "catalog",
        "look_kind": entry.get("look_kind") or "lightroom_preset",
        **look_fields,
        "profile_name": entry.get("profile_name"),
        "profile_hash": entry.get("profile_hash") or entry.get("file_hash"),
        "xmp_compatible": bool(entry.get("xmp_compatible")),
        "amount_supported": runtime_supports_amount(entry),
        "amount_note": (
            None
            if runtime_supports_amount(entry)
            else "此预设由插件托管，Lightroom 仅支持 100%；可换原生预设调强度"
            if str(entry.get("preset_scope") or "catalog") == "plugin"
            else "此预设未启用 Lightroom 强度调整"
        ),
        "scene_affinity": scene_preset_affinity(dict(entry), dict(scene)),
        "clip_score": None,
    }


def _merge_single_group_plan(
    previous: dict[str, Any] | None,
    fresh: dict[str, Any],
    target_group_ids: set[str],
) -> dict[str, Any]:
    if not previous:
        return fresh
    prior_groups = {
        str(item.get("group_id")): dict(item)
        for item in previous.get("groups", [])
        if isinstance(item, dict) and str(item.get("group_id")) not in target_group_ids
    }
    for item in fresh.get("groups", []):
        prior_groups[str(item.get("group_id"))] = item
    fresh["groups"] = [prior_groups[key] for key in sorted(prior_groups)]
    fresh["created_at"] = previous.get("created_at") or fresh.get("created_at")
    prior_pipeline = previous.get("ai_pipeline")
    fresh_pipeline = fresh.get("ai_pipeline")
    if (
        isinstance(prior_pipeline, dict)
        and isinstance(prior_pipeline.get("stages"), dict)
        and prior_pipeline.get("stages")
        and (not isinstance(fresh_pipeline, dict) or not fresh_pipeline.get("stages"))
    ):
        fresh["ai_pipeline"] = dict(prior_pipeline)
    return fresh


def _prepare_plan(
    run_dir: Path,
    data_dir: Path,
    develop: dict[str, Any],
    results: dict[str, Any],
    catalog: dict[str, Any],
    *,
    scope: str,
    group_id: int | None,
    preset_id: str | None,
    preset_hash: str | None,
    amount: int,
    preview_only: bool,
    runtime: Mapping[str, str],
    cascade_runner: CascadeRunner | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    groups, scenes, develop_items = _group_inputs(develop, results, group_id=group_id)
    apply_group_ids = sorted(groups, key=lambda value: int(value))
    if scope == "global":
        project_items = [item for values in groups.values() for item in values]
        project_scene = {
            "scene_type": "project",
            "summaries": [
                summary
                for scene in scenes.values()
                for summary in scene.get("summaries", [])
                if isinstance(summary, str)
            ],
        }
        project_develop_items: dict[tuple[str, str], dict[str, Any]] = {}
        for group_key, values in groups.items():
            for item in values:
                path_key = str(item.get("path") or "").casefold()
                project_develop_items[("global", path_key)] = develop_items[
                    (group_key, path_key)
                ]
        groups = {"global": project_items}
        scenes = {"global": project_scene}
        develop_items = project_develop_items
    probe_selections: dict[str, dict[str, Any]] = {}
    clip_scores: dict[str, dict[str, float]] = {}
    ai_stages: dict[str, dict[str, dict[str, Any]]] = {}
    pipeline_stages: dict[str, dict[str, Any]] = {}
    if cascade_runner is not None and preset_id is None:
        cascade = cascade_runner(
            groups,
            canonical_pool_entries(catalog),
            data_dir,
            seed_scenes=scenes,
            progress=lambda phase, label, current, total: _notify(
                progress, phase, label, current, total
            ),
        )
        cascade_groups = (
            cascade.get("groups") if isinstance(cascade.get("groups"), dict) else {}
        )
        for group_key in groups:
            result = (
                cascade_groups.get(group_key)
                if isinstance(cascade_groups.get(group_key), dict)
                else {}
            )
            if isinstance(result.get("probes"), dict):
                probe_selections[group_key] = dict(result["probes"])
            if isinstance(result.get("scene"), dict):
                scenes[group_key] = dict(result["scene"])
            if isinstance(result.get("clip_scores"), dict):
                clip_scores[group_key] = {
                    str(key): float(value)
                    for key, value in result["clip_scores"].items()
                }
            if isinstance(result.get("stages"), dict):
                ai_stages[group_key] = {
                    str(key): dict(value)
                    for key, value in result["stages"].items()
                    if isinstance(value, dict)
                }
        if isinstance(cascade.get("stages"), dict):
            pipeline_stages = {
                str(key): dict(value)
                for key, value in cascade["stages"].items()
                if isinstance(value, dict)
            }
    previous = load_recommendation_plan(run_dir)
    fresh = create_recommendation_plan(
        str(develop.get("run_id") or results.get("run_id") or run_dir.name),
        groups,
        catalog,
        run_dir,
        scene_analyses=scenes,
        clip_scores=clip_scores,
        probe_selections=probe_selections,
        ai_stages=ai_stages,
        pipeline_stages=pipeline_stages,
        lightroom_available=True,
    )
    target_group_ids = set(groups)
    if scope == "group" and group_id is not None:
        fresh = _merge_single_group_plan(previous, fresh, target_group_ids)
    fresh["scope"] = scope
    fresh["apply_group_ids"] = apply_group_ids
    fresh["runtime_identity"] = dict(runtime)
    if preset_id is not None:
        if len(target_group_ids) != 1 or (scope == "group" and group_id is None):
            raise StyleWorkerError("指定预设预览必须对应一个明确的应用范围。")
        entry = _runtime_entry(catalog, preset_id, preset_hash)
        if int(amount) != 100 and not runtime_supports_amount(entry):
            raise StyleWorkerError("这个预设不支持强度调整，只能使用 100%。")
        group_key = next(iter(target_group_ids))
        target = next(
            item for item in fresh["groups"] if str(item.get("group_id")) == group_key
        )
        candidate = _candidate_payload(entry, target.get("scene") or {})
        if preview_only and previous:
            previous_group = next(
                (
                    dict(item)
                    for item in previous.get("groups", [])
                    if str(item.get("group_id")) == group_key
                ),
                None,
            )
            if previous_group:
                evidence = {
                    key: previous_group[key]
                    for key in (
                        "recommended_kind",
                        "recommended_preset_id",
                        "recommended_preset_hash",
                        "recommended_amount",
                        "recommended_look_kind",
                        "recommended_look_descriptor_hash",
                        "recommended_look_uuid",
                        "recommended_profile_name",
                        "recommended_profile_hash",
                        "recommended_xmp_compatible",
                        "top3",
                        "confidence",
                        "reason",
                        "neutral_score",
                        "neutral_preview_key",
                        "neutral_preview_path",
                        "ai_stages",
                        "recall_basis",
                        "missing_stages",
                    )
                    if key in previous_group
                }
                target.update(evidence)
        target["candidates"] = [candidate]
        # A normal group needs one exact Lightroom preview.  A project-wide
        # choice additionally renders the same look on the representative and
        # brightness extremes.  This is deliberately limited to three stable
        # probes: it makes the global result inspectable without multiplying
        # every Top 3 candidate by every photo in the project.
        probe_roles = ["representative"]
        if scope == "global" and preview_only:
            probe_roles.extend(["brightest", "darkest"])
        render_tasks: list[dict[str, Any]] = []
        seen_sources: set[str] = set()
        runtime_entry = {
            **entry,
            "runtime_preset_uuid": candidate["preset_uuid"],
            "preset_scope": candidate["preset_scope"],
        }
        for role in probe_roles:
            source = str(target.get("probes", {}).get(role) or "")
            source_key = source.casefold()
            if not source or source_key in seen_sources:
                continue
            source_item = develop_items.get((group_key, source_key))
            if not isinstance(source_item, Mapping):
                raise StyleWorkerError("全局抽查照片已经变化，请重新运行 AI 推荐。")
            crop_revision, basic_color_revision = _render_revisions(
                develop, source_item
            )
            tasks = build_render_tasks(
                group_key,
                {"representative": source},
                [runtime_entry],
                amount=amount,
                base_hash=_base_hash(source_item),
                source_fingerprint=source_cache_key(
                    Path(source).resolve(strict=True)
                ),
                crop_revision=crop_revision,
                basic_color_revision=basic_color_revision,
                **runtime,
                limit=1,
            )
            for task in tasks:
                task["probe_role"] = role
                task["source_index"] = int(source_item.get("index", -1))
            render_tasks.extend(tasks)
            seen_sources.add(source_key)
        target["render_tasks"] = render_tasks
        target["preview_probe_count"] = len(render_tasks)
        target["forced"] = True
        target["preview_only"] = bool(preview_only)
    else:
        if preview_only:
            raise StyleWorkerError("preview_only 必须指定预设。")
        for target in fresh.get("groups", []):
            target_key = str(target.get("group_id"))
            if target_key not in target_group_ids:
                continue
            representative = str(target.get("probes", {}).get("representative") or "")
            source_item = develop_items[(target_key, representative.casefold())]
            entries_by_id = {
                str(item.get("preset_id")): item
                for item in canonical_pool_entries(catalog)
            }
            selected_entries = [
                entries_by_id[str(candidate.get("preset_id"))]
                for candidate in target.get("candidates", [])[:RENDER_CANDIDATE_LIMIT]
                if str(candidate.get("preset_id")) in entries_by_id
            ]
            target["render_tasks"] = build_render_tasks(
                target_key,
                target["probes"],
                selected_entries,
                amount=amount,
                base_hash=_base_hash(source_item),
                source_fingerprint=source_cache_key(Path(representative)),
                crop_revision=_render_revisions(develop, source_item)[0],
                basic_color_revision=_render_revisions(develop, source_item)[1],
                **runtime,
                limit=RENDER_CANDIDATE_LIMIT,
            )
            target["candidates"] = target.get("candidates", [])[:RENDER_CANDIDATE_LIMIT]
    fresh["status"] = "rendering"
    fresh["updated_at"] = _now()
    write_json(run_dir / STYLE_RECOMMENDATION_PATH, fresh)
    return fresh, develop_items


def _neutral_task(
    group: Mapping[str, Any],
    source_item: Mapping[str, Any],
    develop: Mapping[str, Any],
    runtime: Mapping[str, str],
) -> dict[str, Any]:
    source_path = str(group.get("probes", {}).get("representative") or "")
    base_hash = _base_hash(source_item)
    crop_revision, basic_color_revision = _render_revisions(develop, source_item)
    return {
        "group_id": str(group.get("group_id")),
        "kind": "neutral",
        "source_path": source_path,
        "preset_id": None,
        "preset_hash": None,
        "preset_uuid": None,
        "preset_scope": "catalog",
        "amount": 0,
        "cache_key": preview_cache_key(
            source_path=source_path,
            preset_id=NEUTRAL_PRESET_ID,
            preset_hash=NEUTRAL_PRESET_HASH,
            amount=0,
            base_hash=base_hash,
            source_fingerprint=source_cache_key(Path(source_path)),
            crop_revision=crop_revision,
            basic_color_revision=basic_color_revision,
            **runtime,
        ),
        "status": "pending",
    }


def _task_id(group_id: str, kind: str, cache_key: str) -> str:
    safe_group = re.sub(r"[^A-Za-z0-9._-]+", "-", group_id).strip("-") or "group"
    prefix = "n" if kind == "neutral" else "p"
    return f"g{safe_group}-{prefix}-{cache_key[:24]}"[:96]


def _materialize_look_descriptor(
    data_dir: Path,
    task: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish one compact Creative Look below the configured bridge root."""

    descriptor = task.get("look_descriptor")
    source_hash = str(task.get("look_descriptor_hash") or "").lower()
    look_uuid = str(task.get("look_uuid") or "")
    if (
        not isinstance(descriptor, Mapping)
        or not source_hash
        or str(descriptor.get("Hash") or "").lower() != source_hash
        or not look_uuid
    ):
        raise StyleWorkerError("Creative Look 预览缺少完整或一致的安全描述符。")
    try:
        materialized = write_lightroom_look_descriptor(
            data_dir,
            descriptor,
            look_uuid=look_uuid,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise StyleWorkerError(f"Creative Look 描述符无效：{exc}") from exc
    return {
        **materialized,
        "look_amount": int(task.get("amount", 100)),
    }


def _lightroom_task(
    task: dict[str, Any],
    source_item: Mapping[str, Any],
    incoming_dir: Path,
    data_dir: Path,
) -> LightroomTask:
    source = Path(str(task["source_path"])).resolve(strict=True)
    style = _base_style(source_item)
    is_creative_look = (
        str(task.get("look_kind") or "lightroom_preset") == "lightroom_profile"
    )
    look_fields = (
        _materialize_look_descriptor(data_dir, task) if is_creative_look else {}
    )
    return LightroomTask(
        photo_path=source,
        task_type="preview",
        task_id=str(task["task_id"]),
        auto_tone=True,
        auto_white_balance=True,
        lens_profile=True,
        remove_chromatic_aberration=True,
        crop=_selected_crop(source_item),
        style=style,
        preset_uuid=None if is_creative_look else task.get("preset_uuid"),
        preset_scope=str(task.get("preset_scope") or "catalog"),
        preset_amount=int(task.get("amount", 100)),
        **look_fields,
        jpeg_output_dir=incoming_dir,
    )


def _ready_request_cache(
    plan: Mapping[str, Any] | None,
    run_dir: Path,
    request_key: str,
) -> int | None:
    if (
        not isinstance(plan, Mapping)
        or plan.get("status") != "complete"
        or plan.get("request_cache_key") != request_key
    ):
        return None
    worker = plan.get("worker") if isinstance(plan.get("worker"), Mapping) else {}
    target_ids = {str(value) for value in worker.get("group_ids", [])}
    if not target_ids:
        return None
    ready_count = 0
    for group in plan.get("groups", []):
        if (
            not isinstance(group, Mapping)
            or str(group.get("group_id")) not in target_ids
        ):
            continue
        tasks = [group.get("neutral_render_task"), *group.get("render_tasks", [])]
        if not tasks or any(not isinstance(task, Mapping) for task in tasks):
            return None
        for task in tasks:
            preview_key = str(task.get("preview_key") or task.get("cache_key") or "")
            try:
                resolve_style_preview(run_dir, preview_key)
            except (FileNotFoundError, ValueError):
                return None
            if task.get("render_status") != "ready":
                return None
            ready_count += 1
    return ready_count or None


def _request_cache_path(run_dir: Path, request_key: str) -> Path:
    if not _CACHE_KEY.fullmatch(str(request_key)):
        raise StyleWorkerError("创意外观请求缓存键无效。")
    return run_dir / STYLE_REQUEST_CACHE_DIRECTORY / f"{request_key}.json"


def _load_request_cache(run_dir: Path, request_key: str) -> dict[str, Any] | None:
    target = _request_cache_path(run_dir, request_key)
    if not target.is_file():
        return None
    try:
        plan = _read_mapping(target, "创意外观请求缓存")
    except (OSError, TypeError, ValueError, StyleWorkerError):
        return None
    return (
        plan if _ready_request_cache(plan, run_dir, request_key) is not None else None
    )


def _save_request_cache(run_dir: Path, plan: Mapping[str, Any]) -> None:
    request_key = str(plan.get("request_cache_key") or "")
    target = _request_cache_path(run_dir, request_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, dict(plan))


def _cached_start_result(
    run_dir: Path,
    plan: dict[str, Any],
    *,
    request_key: str,
    batch_id: str,
    base_revision: int,
    scope: str,
    runtime: Mapping[str, str],
    preserve_confirmed: bool,
    progress: ProgressCallback | None,
) -> dict[str, Any]:
    cached_count = _ready_request_cache(plan, run_dir, request_key)
    if cached_count is None:
        raise StyleWorkerError("创意外观完整请求缓存已经失效。")
    worker = dict(plan.get("worker")) if isinstance(plan.get("worker"), Mapping) else {}
    worker.update(
        status="rendered",
        batch_id=None,
        requested_batch_id=batch_id,
        source_develop_revision=int(base_revision),
        task_count=0,
        cached_count=cached_count,
        recommendation_cached=True,
        preserve_confirmed=bool(preserve_confirmed),
        scope=scope,
        updated_at=_now(),
    )
    plan["worker"] = worker
    plan["runtime_identity"] = dict(runtime)
    plan["updated_at"] = _now()
    write_json(run_dir / STYLE_RECOMMENDATION_PATH, plan)
    _notify(
        progress,
        "process",
        "复用完整 AI 与 Lightroom 推荐缓存",
        cached_count,
        cached_count,
    )
    return {
        "plan": plan,
        "batch": None,
        "batch_id": None,
        "published_count": 0,
        "cached_count": cached_count,
        "recommendation_cached": True,
    }


def start_style_preview_batch(
    run_dir: Path | str,
    data_dir: Path | str,
    batch_id: str,
    base_revision: int,
    *,
    scope: str | None = None,
    group_id: int | None = None,
    preset_id: str | None = None,
    preset_hash: str | None = None,
    amount: int = 100,
    preview_only: bool = False,
    preserve_confirmed: bool = False,
    catalog: dict[str, Any] | None = None,
    lightroom_exe: Path | str | None = None,
    startup_timeout: float = 90.0,
    status_reader: BridgeStatusReader = read_lightroom_bridge_status,
    batch_creator: BatchCreator = create_lightroom_batch,
    cascade_runner: CascadeRunner | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Create/reuse cached previews and publish missing Lightroom tasks."""

    run_root = Path(run_dir).expanduser().resolve()
    data_root = Path(data_dir).expanduser().resolve()
    if not _BATCH_ID.fullmatch(str(batch_id)):
        raise StyleWorkerError("Lightroom 风格预览批次标识无效。")
    resolved_scope = str(scope or ("global" if group_id == 0 else "group"))
    if resolved_scope not in {"global", "group"}:
        raise StyleWorkerError("创意外观范围必须是 global 或 group。")
    if resolved_scope == "global":
        if group_id not in {None, 0}:
            raise StyleWorkerError("全局统一推荐不能指定照片组。")
        group_id = None
    elif group_id == 0:
        raise StyleWorkerError("按组推荐必须指定有效照片组。")
    if not 0 <= int(amount) <= 200:
        raise StyleWorkerError("预设强度必须是 0 到 200。")
    if bool(preset_id) != bool(preset_hash):
        raise StyleWorkerError("预设 ID 与精确文件哈希必须同时提供。")
    if preview_only and not preset_id:
        raise StyleWorkerError("preview_only 必须指定预设 ID 与精确文件哈希。")
    if int(amount) != 100 and not preset_id:
        raise StyleWorkerError("自定义强度只适用于指定预设的真实预览。")
    develop, results = _load_run_state(run_root)
    if int(develop.get("revision", -1)) != int(base_revision):
        raise StyleWorkerError("调色方案已更新，请刷新后重试。")
    catalog_was_injected = catalog is not None
    current_catalog = (
        catalog if catalog is not None else _load_runtime_style_catalog(data_root)
    )
    # Reject an invalid manual choice before launching or waiting for desktop
    # software.  Production performs the same check again after SDK gating.
    if preset_id is not None:
        selected_entry = _runtime_entry(current_catalog, preset_id, preset_hash)
        if int(amount) != 100 and not runtime_supports_amount(selected_entry):
            raise StyleWorkerError("这个预设不支持强度调整，只能使用 100%。")
    if preset_id is None and not preview_only:
        early_status = status_reader(data_root)
        early_heartbeat = (
            early_status.get("heartbeat")
            if isinstance(early_status, Mapping)
            and isinstance(early_status.get("heartbeat"), Mapping)
            else {}
        )
        if (
            early_heartbeat.get("state") == "online"
            and str(early_heartbeat.get("plugin_version") or "")
            == REQUIRED_PREVIEW_PLUGIN_VERSION
        ):
            early_groups, _early_scenes, _early_items = _group_inputs(
                develop, results, group_id=group_id
            )
            early_runtime = _runtime_identity(early_status, current_catalog)
            early_key = _recommendation_request_key(
                develop,
                early_groups,
                current_catalog,
                scope=resolved_scope,
                group_id=group_id,
                amount=int(amount),
                runtime=early_runtime,
            )
            early_cached = _load_request_cache(run_root, early_key)
            if early_cached is not None:
                return _cached_start_result(
                    run_root,
                    early_cached,
                    request_key=early_key,
                    batch_id=batch_id,
                    base_revision=base_revision,
                    scope=resolved_scope,
                    runtime=early_runtime,
                    preserve_confirmed=preserve_confirmed,
                    progress=progress,
                )
    bridge_status = ensure_lightroom_preview_plugin(
        data_root,
        lightroom_exe=lightroom_exe,
        startup_timeout=startup_timeout,
        status_reader=status_reader,
    )
    if catalog is None:
        # The plug-in may have registered managed presets while coming online;
        # refresh that writeback before applying its authoritative SDK listing.
        current_catalog = _load_runtime_style_catalog(data_root)
        # A recent SDK-authored snapshot is authoritative for both forced and
        # recommendation requests.  Re-enumerating the same catalog on every
        # AI click costs a Lightroom round-trip before preview caching can do
        # any useful work.
        records = _read_runtime_preset_snapshot(data_root)
        if records is None:
            _notify(progress, "presets", "核对 Lightroom 可用预设", 0, 1)
            records = _enumerate_runtime_presets(data_root)
            _notify(progress, "presets", "核对 Lightroom 可用预设", 1, 1)
        current_catalog = _apply_runtime_preset_listing(current_catalog, records)
    if not current_catalog.get("default_pool") and preset_id is None:
        raise StyleWorkerError("风格库没有可由 Lightroom 使用的 AI 预设。")
    if preset_id is not None:
        selected_entry = _runtime_entry(current_catalog, preset_id, preset_hash)
        if int(amount) != 100 and not runtime_supports_amount(selected_entry):
            raise StyleWorkerError("这个预设不支持强度调整，只能使用 100%。")
    request_groups, _request_scenes, _request_items = _group_inputs(
        develop, results, group_id=group_id
    )
    runtime = _runtime_identity(bridge_status, current_catalog)
    request_key = _recommendation_request_key(
        develop,
        request_groups,
        current_catalog,
        scope=resolved_scope,
        group_id=group_id,
        amount=int(amount),
        runtime=runtime,
    )
    if preset_id is None and not preview_only:
        cached_plan = _load_request_cache(run_root, request_key)
        if cached_plan is not None:
            return _cached_start_result(
                run_root,
                cached_plan,
                request_key=request_key,
                batch_id=batch_id,
                base_revision=base_revision,
                scope=resolved_scope,
                runtime=runtime,
                preserve_confirmed=preserve_confirmed,
                progress=progress,
            )
    effective_cascade_runner = (
        cascade_runner
        if cascade_runner is not None
        else run_style_ai_cascade
        if not catalog_was_injected and preset_id is None
        else None
    )
    plan, source_items = _prepare_plan(
        run_root,
        data_root,
        develop,
        results,
        current_catalog,
        scope=resolved_scope,
        group_id=group_id,
        preset_id=preset_id,
        preset_hash=preset_hash,
        amount=int(amount),
        preview_only=preview_only,
        runtime=runtime,
        cascade_runner=effective_cascade_runner,
        progress=progress,
    )
    if effective_cascade_runner is None:
        _notify(progress, "recall", "读取可用创意外观", 1, 1)
    target_group_ids = {
        str(group_id) if group_id is not None else str(item.get("group_id"))
        for item in plan.get("groups", [])
        if group_id is None or str(item.get("group_id")) == str(group_id)
    }
    preview_root = run_root / STYLE_PREVIEW_DIRECTORY
    incoming_dir = preview_root / "_incoming" / batch_id
    published: list[LightroomTask] = []
    task_map: dict[str, dict[str, Any]] = {}
    total = sum(
        1 + len(group.get("render_tasks", []))
        for group in plan.get("groups", [])
        if str(group.get("group_id")) in target_group_ids
    )
    completed = 0
    for group in plan.get("groups", []):
        group_key = str(group.get("group_id"))
        if group_key not in target_group_ids:
            continue
        representative = str(group.get("probes", {}).get("representative") or "")
        source_item = source_items[(group_key, representative.casefold())]
        neutral = _neutral_task(group, source_item, develop, runtime)
        tasks = [neutral, *group.get("render_tasks", [])]
        group["neutral_render_task"] = neutral
        for task in tasks:
            task["kind"] = str(task.get("kind") or "preset")
            cache_key = str(task["cache_key"])
            cache_path = style_preview_path(run_root, cache_key)
            task["preview_key"] = cache_key
            task["preview_path"] = str(cache_path)
            if _valid_jpeg(cache_path):
                task["status"] = "ready"
                task["render_status"] = "ready"
                task["cached"] = True
                completed += 1
                _notify(progress, "process", "复用 Lightroom 风格预览", completed, total)
                continue
            task_id = _task_id(group_key, task["kind"], cache_key)
            task["task_id"] = task_id
            task["status"] = "pending"
            task["render_status"] = "pending"
            task["cached"] = False
            task_map[task_id] = task
            task_source = str(task.get("source_path") or representative)
            task_source_item = source_items.get(
                (group_key, task_source.casefold())
            )
            if not isinstance(task_source_item, Mapping):
                raise StyleWorkerError("Lightroom 抽查照片已经变化，请重新生成预览。")
            published.append(
                _lightroom_task(task, task_source_item, incoming_dir, data_root)
            )
    plan["worker"] = {
        "status": "rendering" if published else "rendered",
        "batch_id": batch_id if published else None,
        "requested_batch_id": batch_id,
        "source_develop_revision": int(base_revision),
        "group_ids": sorted(target_group_ids),
        "task_count": len(published),
        "cached_count": completed,
        "incoming_dir": str(incoming_dir),
        "preview_only": bool(preview_only),
        "scope": resolved_scope,
        "request_cache_key": request_key,
        "recommendation_cached": False,
        "preserve_confirmed": bool(preserve_confirmed),
        "forced_preset_id": preset_id,
        "amount": int(amount),
        "updated_at": _now(),
    }
    plan["request_cache_key"] = request_key
    plan["scope"] = resolved_scope
    write_json(run_root / STYLE_RECOMMENDATION_PATH, plan)
    batch: dict[str, Any] | None = None
    if published:
        incoming_dir.mkdir(parents=True, exist_ok=True)
        batch = batch_creator(data_root, published, batch_id=batch_id)
    return {
        "plan": plan,
        "batch": batch,
        "batch_id": batch_id if published else None,
        "published_count": len(published),
        "cached_count": completed,
        "recommendation_cached": False,
    }


def _external_01(value: Any) -> float:
    number = float(value)
    if 0.0 <= number <= 1.05:
        return _bounded(number)
    if number <= 5.5:
        return _bounded((number - 1.0) / 4.0)
    if number <= 10.5:
        return _bounded(number / 10.0)
    return _bounded(number / 100.0)


def _technical_score(path: Path) -> tuple[float, np.ndarray]:
    with Image.open(path) as source:
        features = technical_features(source.convert("RGB"))
    brightness = float(features[1])
    contrast = _bounded(float(features[2]) / 1.2)
    clipping = _bounded(1.0 - 5.0 * float(features[3]) - 8.0 * float(features[4]))
    entropy = _bounded(float(features[6]))
    exposure = _bounded(1.0 - abs(brightness - 0.48) / 0.48)
    score = 0.24 * contrast + 0.26 * clipping + 0.25 * entropy + 0.25 * exposure
    return _bounded(score), features


def _appearance_consistency(neutral: np.ndarray, candidate: np.ndarray) -> float:
    brightness_delta = abs(float(candidate[1]) - float(neutral[1]))
    contrast_delta = abs(float(candidate[2]) - float(neutral[2]))
    saturation_delta = abs(float(candidate[5]) - float(neutral[5]))
    clipping_growth = max(
        0.0,
        float(candidate[3] + candidate[4]) - float(neutral[3] + neutral[4]),
    )
    penalty = (
        0.85 * brightness_delta
        + 0.35 * contrast_delta
        + 0.65 * saturation_delta
        + 3.0 * clipping_growth
    )
    return _bounded(1.0 - penalty)


def _score_group(
    group: dict[str, Any],
    scorer: AestheticScorer,
) -> None:
    neutral_task = group.get("neutral_render_task")
    if (
        not isinstance(neutral_task, dict)
        or neutral_task.get("render_status") != "ready"
    ):
        raise StyleWorkerError(f"照片组 {group.get('group_id')} 缺少自然版本预览。")
    candidates_by_id = {
        str(item.get("preset_id")): item
        for item in group.get("candidates", [])
        if isinstance(item, dict)
    }
    rendered: list[dict[str, Any]] = []
    for task in group.get("render_tasks", []):
        if task.get("render_status") != "ready":
            continue
        candidate = dict(candidates_by_id.get(str(task.get("preset_id")), {}))
        candidate.update(
            {
                "preset_id": task.get("preset_id"),
                "preset_hash": task.get("preset_hash"),
                "preset_uuid": task.get("preset_uuid"),
                "preset_scope": task.get("preset_scope") or "catalog",
                "look_kind": task.get("look_kind") or "lightroom_preset",
                "profile_name": task.get("profile_name"),
                "profile_hash": task.get("profile_hash"),
                "xmp_compatible": bool(task.get("xmp_compatible")),
                "amount": int(task.get("amount", 100)),
                "render_status": "ready",
                "preview_key": task.get("preview_key"),
                "preview_path": task.get("preview_path"),
            }
        )
        if task.get("look_kind") == "lightroom_profile":
            candidate.update(
                look_descriptor=task.get("look_descriptor"),
                look_descriptor_hash=task.get("look_descriptor_hash"),
                look_uuid=task.get("look_uuid"),
            )
        rendered.append(candidate)
    if not rendered:
        raise StyleWorkerError(f"照片组 {group.get('group_id')} 没有完成的候选预览。")
    paths = [
        Path(str(neutral_task["preview_path"])),
        *(Path(str(item["preview_path"])) for item in rendered),
    ]
    scores = scorer.score(paths)
    if len(scores) != len(paths):
        raise StyleWorkerError("Q-ReAlign 没有返回完整的预览评分。")
    aesthetic = [
        0.55 * _external_01(item["aesthetic"]) + 0.45 * _external_01(item["quality"])
        for item in scores
    ]
    neutral_technical, neutral_features = _technical_score(paths[0])
    neutral_metrics = {
        "style_match": 0.50,
        "aesthetic_improvement": 0.50,
        "technical_quality": neutral_technical,
        "group_consistency": 0.80,
    }
    neutral_score = sum(
        RECOMMENDATION_WEIGHTS[key] * value for key, value in neutral_metrics.items()
    )
    for index, candidate in enumerate(rendered, start=1):
        technical, features = _technical_score(paths[index])
        affinity = _bounded(candidate.get("scene_affinity", 0.0))
        clip_score = candidate.get("clip_score")
        semantic_match = (
            _bounded(0.78 * _bounded(clip_score) + 0.22 * affinity)
            if clip_score is not None
            else _bounded(0.48 + 0.50 * affinity)
        )
        candidate["metrics"] = {
            "style_match": semantic_match,
            "aesthetic_improvement": _bounded(
                0.50 + 2.0 * (aesthetic[index] - aesthetic[0])
            ),
            "technical_quality": technical,
            "group_consistency": _appearance_consistency(neutral_features, features),
        }
        candidate["qrealign"] = {
            "aesthetic": round(_external_01(scores[index]["aesthetic"]), 6),
            "quality": round(_external_01(scores[index]["quality"]), 6),
        }
    ranking = rank_rendered_candidates(
        rendered,
        neutral_score=neutral_score,
        confidence_gap=0.02,
        minimum_improvement=0.015,
    )
    group.update(ranking)
    group["recommendation_status"] = ranking["status"]
    group["neutral_score"] = round(neutral_score, 6)
    group["neutral_metrics"] = neutral_metrics
    group["neutral_preview_key"] = neutral_task["preview_key"]
    group["neutral_preview_path"] = neutral_task["preview_path"]
    selected = group.get("selected") if isinstance(group.get("selected"), dict) else {}
    selected_preview = (
        neutral_task
        if selected.get("kind") != "preset"
        else next(
            (
                item
                for item in ranking.get("top3", [])
                if item.get("preset_id") == selected.get("preset_id")
            ),
            None,
        )
    )
    if isinstance(selected_preview, Mapping):
        selected["preview_key"] = selected_preview.get("preview_key")
        selected["preview_path"] = selected_preview.get("preview_path")
    group["selected"] = selected
    group["recommended_kind"] = selected.get("kind") or "neutral"
    group["recommended_preset_id"] = selected.get("preset_id")
    group["recommended_preset_hash"] = selected.get("preset_hash")
    group["recommended_amount"] = int(selected.get("amount", 0))
    group["recommended_look_kind"] = selected.get("look_kind")
    group["recommended_look_descriptor_hash"] = selected.get("look_descriptor_hash")
    group["recommended_look_uuid"] = selected.get("look_uuid")
    group["recommended_profile_name"] = selected.get("profile_name")
    group["recommended_profile_hash"] = selected.get("profile_hash")
    group["recommended_xmp_compatible"] = bool(selected.get("xmp_compatible"))
    group["missing_stages"] = [
        str(value)
        for value in group.get("missing_stages", [])
        if str(value) not in {"lightroom_exact_preview", "qrealign_rerank"}
    ]
    group["status"] = "complete"


def _validate_look_result(
    result: Mapping[str, Any],
    task: Mapping[str, Any],
) -> None:
    expected_uuid = str(task.get("look_uuid") or "")
    status = result.get("look_status")
    if not expected_uuid:
        if status not in {None, "not_requested"}:
            raise StyleWorkerError(
                "Lightroom 对未请求的 Creative Look 返回了异常状态。"
            )
        if result.get("look_uuid") not in {None, ""}:
            raise StyleWorkerError("Lightroom 对未请求的 Creative Look 返回了 UUID。")
        return
    if status != "done":
        raise StyleWorkerError("Lightroom 未确认 Creative Look 已完整应用。")
    if str(result.get("look_uuid") or "") != expected_uuid:
        raise StyleWorkerError("Lightroom 返回的 Creative Look UUID 不匹配。")
    actual_amount = result.get("look_amount")
    expected_amount = int(task.get("amount", 100))
    # Lightroom's Lua number type is serialized as a finite bridge float even
    # when the requested Amount is an integer (for example ``f|135``).  Treat
    # an exact integral numeric value as the same protocol value while still
    # rejecting booleans, fractions and coercible strings.
    if (
        isinstance(actual_amount, bool)
        or not isinstance(actual_amount, (int, float))
        or not math.isfinite(float(actual_amount))
        or not float(actual_amount).is_integer()
        or int(actual_amount) != expected_amount
    ):
        raise StyleWorkerError("Lightroom 返回的 Creative Look 强度不匹配。")


def _secure_collect_jpeg(
    result: Mapping[str, Any],
    task: Mapping[str, Any],
    *,
    incoming_root: Path,
    cache_target: Path,
    batch_id: str,
    task_id: str,
) -> None:
    if str(result.get("batch_id") or "") != batch_id:
        raise StyleWorkerError("Lightroom 预览结果批次标识不匹配。")
    if str(result.get("task_id") or "") != task_id:
        raise StyleWorkerError("Lightroom 预览结果任务标识不匹配。")
    if result.get("restore_status") != "done":
        raise StyleWorkerError("Lightroom 未确认恢复照片的原始调整状态。")
    if result.get("isolation_kind") != "virtual_copy":
        raise StyleWorkerError("Lightroom 未确认预览使用隔离虚拟副本。")
    if result.get("isolation_status") != "removed":
        raise StyleWorkerError("Lightroom 未确认隔离虚拟副本已经移除。")
    _validate_look_result(result, task)
    jpeg_path = Path(str(result.get("jpeg_path") or ""))
    try:
        source = jpeg_path.resolve(strict=True)
    except OSError as exc:
        raise StyleWorkerError("Lightroom 没有返回可读取的 JPEG 预览。") from exc
    safe_root = incoming_root.resolve()
    if not _inside(source, safe_root) or not _valid_jpeg(source):
        raise StyleWorkerError("Lightroom 返回的 JPEG 超出本次预览缓存范围或文件无效。")
    cache_target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, cache_target)
    if not _valid_jpeg(cache_target):
        raise StyleWorkerError("风格预览缓存写入后校验失败。")


def collect_style_preview_batch(
    run_dir: Path | str,
    data_dir: Path | str,
    batch_id: str,
    *,
    batch_status: dict[str, Any] | None = None,
    status_reader: BatchStatusReader = read_lightroom_batch_status,
    allow_partial: bool = False,
    allow_cancelled: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Safely adopt bridge JPEGs into the immutable run-scoped cache.

    A cancelled multi-group batch may contain a useful prefix of completed
    tasks.  Callers must opt in explicitly; every such task still passes the
    exact same path, Look, restore, and virtual-copy isolation checks as a
    normally completed batch.  Pending, failed, or cancelled tasks remain
    absent from the cache and are therefore naturally republished on retry.
    """

    run_root = Path(run_dir).expanduser().resolve()
    data_root = Path(data_dir).expanduser().resolve()
    if not _BATCH_ID.fullmatch(str(batch_id)):
        raise StyleWorkerError("Lightroom 风格预览批次标识无效。")
    plan = _read_mapping(run_root / STYLE_RECOMMENDATION_PATH, "风格推荐计划")
    worker = plan.get("worker") if isinstance(plan.get("worker"), dict) else {}
    if str(worker.get("batch_id") or "") != str(batch_id):
        raise StyleWorkerError("风格推荐计划与 Lightroom 批次不匹配。")
    status = batch_status or status_reader(data_root, batch_id)
    terminal_status = str(status.get("status") or "")
    explicitly_cancelled = (
        terminal_status in {"cancelling", "cancelled"}
        and isinstance(status.get("cancellation"), Mapping)
    )
    if terminal_status != "complete" and not (
        allow_partial and terminal_status == "failed"
    ) and not (
        allow_cancelled and explicitly_cancelled
    ):
        raise StyleWorkerError(
            f"Lightroom 风格预览尚未成功完成：{status.get('status', 'unknown')}"
        )
    returned_batch_id = str(status.get("batch_id") or "")
    if returned_batch_id and returned_batch_id != str(batch_id):
        raise StyleWorkerError("Lightroom 返回了另一个预览批次的状态。")
    task_results = {
        str(item.get("task_id")): item
        for item in status.get("tasks", [])
        if isinstance(item, dict)
    }
    incoming_root = Path(str(worker.get("incoming_dir") or "")).resolve()
    expected_incoming_root = (
        run_root / STYLE_PREVIEW_DIRECTORY / "_incoming" / batch_id
    ).resolve()
    if incoming_root != expected_incoming_root:
        raise StyleWorkerError("风格推荐计划中的 Lightroom 临时目录无效。")
    completed = int(worker.get("cached_count", 0))
    total = completed + int(worker.get("task_count", 0))
    target_group_ids = {str(value) for value in worker.get("group_ids", [])}
    failed_group_ids: list[str] = []
    successful_group_ids: list[str] = []
    for group in plan.get("groups", []):
        group_key = str(group.get("group_id"))
        if group_key not in target_group_ids:
            continue
        tasks = [group.get("neutral_render_task"), *group.get("render_tasks", [])]
        group_errors: list[str] = []
        for task in tasks:
            if not isinstance(task, dict) or task.get("render_status") == "ready":
                continue
            task_id = str(task.get("task_id") or "")
            cache_target = style_preview_path(run_root, str(task["cache_key"]))
            if _valid_jpeg(cache_target):
                # A process may be terminated after the atomic move but before
                # the plan write. Never replace that immutable verified cache;
                # simply repair the task state around it.
                task["status"] = "ready"
                task["render_status"] = "ready"
                task["preview_key"] = task["cache_key"]
                task["preview_path"] = str(cache_target)
                task["cached"] = True
                completed += 1
                _notify(progress, "collect", "复用已校验 Lightroom 预览", completed, total)
                continue
            row = task_results.get(task_id)
            result = row.get("result") if isinstance(row, Mapping) else None
            if not isinstance(row, Mapping) or row.get("status") != "done":
                message = (
                    result.get("message") or result.get("error")
                    if isinstance(result, Mapping)
                    else None
                )
                row_status = row.get("status") if isinstance(row, Mapping) else "missing"
                group_errors.append(str(message or row_status))
                continue
            if not isinstance(result, Mapping):
                group_errors.append("Lightroom 未返回任务结果")
                continue
            try:
                _secure_collect_jpeg(
                    result,
                    task,
                    incoming_root=incoming_root,
                    cache_target=cache_target,
                    batch_id=batch_id,
                    task_id=task_id,
                )
            except StyleWorkerError as exc:
                if not (allow_partial or explicitly_cancelled):
                    raise
                group_errors.append(str(exc))
                continue
            task["status"] = "ready"
            task["render_status"] = "ready"
            task["preview_key"] = task["cache_key"]
            task["preview_path"] = str(cache_target)
            completed += 1
            _notify(progress, "collect", "校验 Lightroom 预览", completed, total)
        if group_errors:
            group["recommendation_status"] = "failed"
            group["error"] = "；".join(dict.fromkeys(group_errors))[:2000]
            failed_group_ids.append(group_key)
        else:
            successful_group_ids.append(group_key)
    worker["status"] = (
        "cancelled"
        if explicitly_cancelled
        else "partial"
        if failed_group_ids
        else "rendered"
    )
    worker["cancelled"] = explicitly_cancelled
    if explicitly_cancelled:
        worker["cancellation"] = dict(status.get("cancellation") or {})
        plan["status"] = "cancelled"
    else:
        worker.pop("cancellation", None)
    worker["completed_count"] = completed
    worker["failed_group_ids"] = failed_group_ids
    worker["successful_group_ids"] = successful_group_ids
    worker["updated_at"] = _now()
    plan["updated_at"] = _now()
    write_json(run_root / STYLE_RECOMMENDATION_PATH, plan)
    return plan


def _recover_cancelled_multi_group_previews(
    run_dir: Path,
    data_dir: Path,
    *,
    base_revision: int,
    status_reader: BatchStatusReader,
    progress: ProgressCallback | None,
) -> dict[str, Any] | None:
    """Adopt the safe prefix left by a terminated all-groups worker.

    The Web job manager deliberately terminates its Python subprocess after it
    writes the Lightroom cancellation marker. The plug-in still drains its
    queue and records rollback/isolation evidence, so the next attempt must
    inspect that prior batch before replacing its plan. Legacy cancellation
    handling could advance ``develop.json`` by one failure-bookkeeping
    revision, so recovery accepts that exact transition as well. Adopted JPEGs
    remain bound to the old immutable cache keys; the fresh plan reuses them
    only when its photo/crop/base/group identity produces the same keys.
    """

    recommendation_path = run_dir / STYLE_RECOMMENDATION_PATH
    if not recommendation_path.is_file():
        return None
    try:
        plan = _read_mapping(recommendation_path, "风格推荐计划")
        worker = plan.get("worker") if isinstance(plan.get("worker"), dict) else {}
        group_ids = {str(value) for value in worker.get("group_ids", [])}
        previous_batch_id = str(worker.get("batch_id") or "")
        source_revision = int(worker.get("source_develop_revision", -1))
        if (
            str(worker.get("scope") or plan.get("scope") or "") != "group"
            or len(group_ids) <= 1
            or int(base_revision) not in {source_revision, source_revision + 1}
            or not _BATCH_ID.fullmatch(previous_batch_id)
        ):
            return None
        status = status_reader(data_dir, previous_batch_id)
        if (
            str(status.get("status") or "") not in {"cancelling", "cancelled"}
            or not isinstance(status.get("cancellation"), Mapping)
        ):
            return None
        return collect_style_preview_batch(
            run_dir,
            data_dir,
            previous_batch_id,
            batch_status=status,
            status_reader=status_reader,
            allow_cancelled=True,
            progress=progress,
        )
    except (OSError, TypeError, ValueError, StyleWorkerError):
        # Recovery never trusts an old malformed plan/result and must not stop
        # a clean new batch. Any already-adopted content-addressed JPEG remains
        # safe and reusable; an unverified incoming file remains unreferenced.
        return None


def _creative_lut_project_root(data_dir: Path) -> Path:
    """Resolve the configured storage owner without guessing another cache."""

    data_root = data_dir.resolve()
    content_root_value = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    configured_data_value = os.environ.get("PHOTO_AI_DATA_DIR")
    if content_root_value and configured_data_value:
        content_root = Path(content_root_value).expanduser().resolve()
        configured_data = Path(configured_data_value).expanduser().resolve()
        try:
            data_root.relative_to(content_root)
            configured_data.relative_to(content_root)
        except ValueError as exc:
            raise StyleWorkerError(
                "创意 LUT 数据目录超出 PHOTO_AI_CONTENT_ROOT。"
            ) from exc
        if data_root != configured_data:
            raise StyleWorkerError("创意 LUT 数据目录与当前配置不一致。")
        return content_root
    if (
        data_root.name.casefold() != "data"
        or data_root.parent.name.casefold() != ".runtime"
    ):
        raise StyleWorkerError(
            "创意 LUT 数据目录必须位于工程的 .runtime/data，已拒绝使用其他磁盘缓存。"
        )
    return data_root.parent.parent


def _default_lut_engine_factory(storage_owner: Path) -> CreativeLutEngine:
    root = Path(storage_owner).resolve()
    content_root_value = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    if content_root_value and root == Path(content_root_value).expanduser().resolve():
        styles = Path(
            os.environ.get("PHOTO_AI_STYLES_DIR", str(root / "styles"))
        ).expanduser().resolve()
        cache = Path(
            os.environ.get("PHOTO_AI_CACHE_DIR", str(root / "cache"))
        ).expanduser().resolve()
        try:
            styles.relative_to(root)
            cache.relative_to(root)
        except ValueError as exc:
            raise StyleWorkerError(
                "创意 LUT 资源或缓存超出 PHOTO_AI_CONTENT_ROOT。"
            ) from exc
        return CreativeLutEngine(
            root,
            enforce_e_drive=False,
            library_root=styles / "creative-luts",
            cache_root=cache / "creative-luts",
        )
    return CreativeLutEngine.for_project(root)


def _current_neutral_lut_input(
    run_dir: Path,
    *,
    develop: Mapping[str, Any],
    results: Mapping[str, Any],
    recommendation: dict[str, Any],
    group_id: int,
) -> tuple[dict[str, Any], Path, str]:
    """Return the current Lightroom-rendered base JPEG for one photo group.

    A RAW/develop thumbnail is deliberately never used here.  The declared
    neutral task must match the current crop/base hash and its immutable JPEG
    must still be inside this run's guarded style-preview directory.
    """

    group_key = str(group_id)
    group = next(
        (
            item
            for item in recommendation.get("groups", [])
            if isinstance(item, dict) and str(item.get("group_id")) == group_key
        ),
        None,
    )
    if not isinstance(group, dict):
        raise StyleWorkerError(f"照片组 {group_id} 还没有 Lightroom 基础预览。")
    neutral_task = group.get("neutral_render_task")
    if (
        not isinstance(neutral_task, Mapping)
        or neutral_task.get("render_status") != "ready"
        or str(neutral_task.get("kind") or "neutral") != "neutral"
    ):
        raise StyleWorkerError(
            f"照片组 {group_id} 缺少 Lightroom 已完成的自然/基础调色 sRGB 预览，"
            "请先生成 AI 风格预览。"
        )

    _groups, _scenes, develop_items = _group_inputs(
        develop,
        results,
        group_id=group_id,
    )
    representative = str(group.get("probes", {}).get("representative") or "")
    source_item = develop_items.get((group_key, representative.casefold()))
    if not isinstance(source_item, Mapping):
        raise StyleWorkerError(
            f"照片组 {group_id} 的代表图已经变化，请先重新生成 AI 风格预览。"
        )
    runtime = (
        recommendation.get("runtime_identity")
        if isinstance(recommendation.get("runtime_identity"), Mapping)
        else {}
    )
    crop_revision, basic_color_revision = _render_revisions(develop, source_item)
    expected_key = preview_cache_key(
        source_path=representative,
        preset_id=NEUTRAL_PRESET_ID,
        preset_hash=NEUTRAL_PRESET_HASH,
        amount=0,
        base_hash=_base_hash(source_item),
        source_fingerprint=source_cache_key(Path(representative)),
        crop_revision=crop_revision,
        basic_color_revision=basic_color_revision,
        lightroom_version=str(runtime.get("lightroom_version") or ""),
        catalog_version=str(runtime.get("catalog_version") or ""),
        plugin_version=str(runtime.get("plugin_version") or ""),
        look_renderer_version=str(runtime.get("look_renderer_version") or "lr-look-v1"),
    )
    task_key = str(
        neutral_task.get("preview_key") or neutral_task.get("cache_key") or ""
    ).casefold()
    group_key_value = str(group.get("neutral_preview_key") or "").casefold()
    if expected_key not in {task_key, group_key_value}:
        raise StyleWorkerError(
            f"照片组 {group_id} 的 Lightroom 基础预览已过期，请重新生成。"
        )
    try:
        preview = resolve_style_preview(run_dir, expected_key)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise StyleWorkerError(
            f"照片组 {group_id} 缺少可用的 Lightroom 自然/基础调色 sRGB 预览，"
            "请先生成 AI 风格预览。"
        ) from exc
    return group, preview, expected_key


def _lut_candidate_payload(
    descriptor: Any,
    *,
    amount: int,
    preview_key: str,
    preview_path: Path,
) -> dict[str, Any]:
    return {
        "kind": "lut",
        "preset_id": None,
        "preset_hash": None,
        "lut_id": str(descriptor.lut_id),
        "lut_hash": str(descriptor.lut_hash),
        "name": str(descriptor.name),
        "label": str(descriptor.name),
        "source": descriptor.source_label or "Imported .cube",
        "category": str(descriptor.kind),
        "asset_kind": "cube_lut",
        "look_kind": "rendered_lut",
        "xmp_compatible": False,
        "amount": int(amount),
        "strength": int(amount),
        "amount_supported": True,
        "amount_note": ORDINARY_XMP_LUT_LIMITATION,
        "render_targets": ["jpeg"],
        "render_status": "ready",
        "preview_key": preview_key,
        "preview_path": str(preview_path),
        "score": None,
    }


def _merge_lut_preview_develop(
    run_dir: Path,
    recommendation: dict[str, Any],
    candidate: Mapping[str, Any],
    *,
    group_id: int,
    base_revision: int,
) -> dict[str, Any]:
    """Persist a rendered-LUT choice without claiming ordinary-XMP support."""

    develop_path = run_dir / "develop.json"
    develop = _read_mapping(develop_path, "裁剪调色方案")
    if int(develop.get("revision", -1)) != int(base_revision):
        raise StyleWorkerError("预览期间调色方案发生变化，已拒绝覆盖，请重新运行。")
    creative = (
        dict(develop.get("creative_style"))
        if isinstance(develop.get("creative_style"), dict)
        else {"status": "pending", "groups": {}}
    )
    groups = (
        dict(creative.get("groups")) if isinstance(creative.get("groups"), dict) else {}
    )
    group_key = str(group_id)
    previous = dict(groups.get(group_key) or {})
    recommendation_group = next(
        (
            item
            for item in recommendation.get("groups", [])
            if isinstance(item, Mapping) and str(item.get("group_id")) == group_key
        ),
        {},
    )
    public_candidate = _public_candidate(candidate)
    visible = [
        dict(item)
        for item in previous.get("top3", [])
        if isinstance(item, Mapping)
        and str(item.get("lut_id") or "") != str(candidate.get("lut_id") or "")
    ]
    visible.insert(0, public_candidate)
    groups[group_key] = {
        **previous,
        "group_id": group_id,
        "status": "confirmed",
        "recommendation_status": "complete",
        "preset_id": None,
        "preset_hash": None,
        "preset_uuid": None,
        "preset_scope": None,
        "profile_name": None,
        "profile_hash": None,
        "lut_id": candidate.get("lut_id"),
        "lut_hash": candidate.get("lut_hash"),
        "look_kind": "rendered_lut",
        "xmp_compatible": False,
        "amount": int(candidate.get("amount", 100)),
        "strength": int(candidate.get("strength", candidate.get("amount", 100))),
        "amount_supported": True,
        "amount_note": candidate.get("amount_note"),
        "neutral_preview_key": recommendation_group.get("neutral_preview_key"),
        "neutral_preview_path": recommendation_group.get("neutral_preview_path"),
        "selected_preview_key": candidate.get("preview_key"),
        "selected_preview_path": candidate.get("preview_path"),
        "top3": visible[:4],
        "manual_override": True,
        "updated_at": _now(),
    }
    groups[group_key].pop("recommendation_error", None)
    all_group_ids = {
        str(int(item.get("group_id", -1)))
        for item in develop.get("items", [])
        if isinstance(item, Mapping)
    }
    creative["groups"] = groups
    creative["status"] = (
        "confirmed"
        if all_group_ids
        and all(
            str(groups.get(key, {}).get("status", "pending"))
            in {"confirmed", "skipped"}
            for key in all_group_ids
        )
        else "pending"
    )
    creative["updated_at"] = _now()
    develop["creative_style"] = creative
    develop["basic_color"] = {"status": "enabled"}
    develop["color_enabled"] = True
    develop["color_mode"] = "style"
    develop["revision"] = int(base_revision) + 1
    develop["updated_at"] = _now()
    write_json(develop_path, develop)
    return develop


def _run_lut_preview(
    run_dir: Path,
    data_dir: Path,
    batch_id: str,
    base_revision: int,
    *,
    group_id: int,
    lut_id: str,
    lut_hash: str,
    amount: int,
    engine_factory: LutEngineFactory,
    progress: ProgressCallback | None,
) -> dict[str, Any]:
    """Render one .cube over a previously exported Lightroom base preview."""

    if not _BATCH_ID.fullmatch(str(batch_id)):
        raise StyleWorkerError("创意 LUT 预览批次标识无效。")
    develop, results = _load_run_state(run_dir)
    if int(develop.get("revision", -1)) != int(base_revision):
        raise StyleWorkerError("调色方案已更新，请刷新后重试。")
    recommendation_path = run_dir / STYLE_RECOMMENDATION_PATH
    if not recommendation_path.is_file():
        raise StyleWorkerError(
            f"照片组 {group_id} 缺少 Lightroom 自然/基础调色 sRGB 预览，"
            "请先生成 AI 风格预览。"
        )
    recommendation = _read_mapping(recommendation_path, "风格推荐计划")
    group, neutral_preview, neutral_key = _current_neutral_lut_input(
        run_dir,
        develop=develop,
        results=results,
        recommendation=recommendation,
        group_id=group_id,
    )
    project_root = _creative_lut_project_root(data_dir)
    engine = engine_factory(project_root)
    descriptor = engine.get_lut(lut_id)
    if str(descriptor.lut_hash) != str(lut_hash):
        raise StyleWorkerError("LUT 版本已经变化，请重新选择。")

    preview_key = engine.cache_key(
        neutral_preview,
        lut_hash=descriptor.lut_hash,
        strength=int(amount),
    )
    preview_path = style_preview_path(run_dir, preview_key)
    cached = _valid_jpeg(preview_path)
    _notify(progress, "lut", "渲染创意 LUT 真实预览", int(cached), 1)
    if not cached:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = engine.render_jpeg(
            neutral_preview,
            lut_id=descriptor.lut_id,
            strength=int(amount),
            output_path=preview_path,
            overwrite=True,
        )
        if (
            str(rendered.lut_id) != str(descriptor.lut_id)
            or str(rendered.lut_hash) != str(descriptor.lut_hash)
            or bool(rendered.xmp_compatible)
        ):
            raise StyleWorkerError("创意 LUT 渲染结果身份或导出能力不一致。")
        if not _valid_jpeg(preview_path):
            raise StyleWorkerError("创意 LUT 没有生成有效的 JPEG 预览。")
        _notify(progress, "lut", "渲染创意 LUT 真实预览", 1, 1)

    candidate = _lut_candidate_payload(
        descriptor,
        amount=int(amount),
        preview_key=preview_key,
        preview_path=preview_path,
    )
    prior_candidates = [
        dict(item)
        for item in group.get("candidates", [])
        if isinstance(item, Mapping)
        and str(item.get("lut_id") or "") != str(descriptor.lut_id)
    ]
    prior_top3 = [
        dict(item)
        for item in group.get("top3", [])
        if isinstance(item, Mapping)
        and str(item.get("lut_id") or "") != str(descriptor.lut_id)
    ]
    group["candidates"] = [candidate, *prior_candidates]
    group["top3"] = [candidate, *prior_top3][:4]
    group["selected"] = dict(candidate)
    group["selected"]["kind"] = "lut"
    group["manual_override"] = True
    group["status"] = "complete"
    group["recommendation_status"] = "complete"
    group["neutral_preview_key"] = neutral_key
    group["neutral_preview_path"] = str(neutral_preview)
    group["lut_render_task"] = {
        "kind": "lut",
        "lut_id": descriptor.lut_id,
        "lut_hash": descriptor.lut_hash,
        "strength": int(amount),
        "status": "ready",
        "render_status": "ready",
        "preview_key": preview_key,
        "preview_path": str(preview_path),
        "source_preview_key": neutral_key,
    }
    group["missing_stages"] = [
        str(value)
        for value in group.get("missing_stages", [])
        if str(value) not in {"lightroom_exact_preview", "creative_lut_render"}
    ]
    group.pop("error", None)
    worker = {
        "status": "complete",
        "batch_id": batch_id,
        "group_ids": [str(group_id)],
        "preview_only": True,
        "look_kind": "rendered_lut",
        "task_count": 0,
        "published_count": 0,
        "cached_count": int(cached),
        "updated_at": _now(),
    }
    recommendation["status"] = "complete"
    recommendation["worker"] = worker
    recommendation["updated_at"] = _now()
    write_json(run_dir / STYLE_RECOMMENDATION_PATH, recommendation)
    merged = _merge_lut_preview_develop(
        run_dir,
        recommendation,
        candidate,
        group_id=group_id,
        base_revision=base_revision,
    )
    _notify(progress, "finalize", "保存创意 LUT 预览", 1, 1)
    return {
        "status": "complete",
        "batch_id": batch_id,
        "published_count": 0,
        "cached_count": int(cached),
        "recommendation": recommendation,
        "develop": merged,
    }


def _public_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "preset_id",
            "preset_hash",
            "lut_id",
            "lut_hash",
            "preset_uuid",
            "preset_scope",
            "look_kind",
            "look_descriptor",
            "look_descriptor_hash",
            "look_uuid",
            "asset_kind",
            "profile_name",
            "profile_hash",
            "xmp_compatible",
            "name",
            "label",
            "source",
            "category",
            "amount",
            "strength",
            "amount_supported",
            "amount_note",
            "render_targets",
            "score",
            "render_status",
            "preview_key",
            "preview_path",
            "metrics",
            "qrealign",
            "scene_affinity",
            "clip_score",
            "preview_samples",
        )
        if key in candidate
    }


def _candidate_identity(candidate: Mapping[str, Any]) -> tuple[str, str, str] | None:
    lut_id = str(candidate.get("lut_id") or "")
    if lut_id:
        return ("lut", lut_id, str(candidate.get("lut_hash") or ""))
    preset_id = str(candidate.get("preset_id") or "")
    if preset_id:
        return ("preset", preset_id, str(candidate.get("preset_hash") or ""))
    return None


def _top3_with_preserved_selection(
    previous: Mapping[str, Any], refreshed: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    selected_identity = _candidate_identity(previous)
    rows = [dict(item) for item in refreshed if isinstance(item, Mapping)]
    if selected_identity is None:
        return rows[:3]
    selected = next(
        (
            dict(item)
            for item in previous.get("top3", [])
            if isinstance(item, Mapping)
            and _candidate_identity(item) == selected_identity
        ),
        None,
    )
    if selected is None:
        selected = _public_candidate(
            {
                **dict(previous),
                "preview_key": previous.get("selected_preview_key"),
                "preview_path": previous.get("selected_preview_path"),
                "preview_samples": previous.get("selected_preview_samples", []),
                "render_status": "ready",
            }
        )
    selected.update(
        amount=int(previous.get("amount", selected.get("amount", 100)) or 0),
        strength=int(previous.get("strength", previous.get("amount", 100)) or 0),
        preview_key=previous.get("selected_preview_key")
        or selected.get("preview_key"),
        preview_path=previous.get("selected_preview_path")
        or selected.get("preview_path"),
    )
    merged = [selected]
    merged.extend(
        item for item in rows if _candidate_identity(item) != selected_identity
    )
    return merged[:3]


def _ready_preview_samples(group: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return one atomic, path-free public sample set for a forced preview."""

    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in group.get("render_tasks", []):
        if not isinstance(task, Mapping) or task.get("render_status") != "ready":
            continue
        source_path = str(task.get("source_path") or "")
        source_key = source_path.casefold()
        preview_key = str(task.get("preview_key") or "")
        if not source_path or source_key in seen or not preview_key:
            continue
        samples.append(
            {
                "role": str(task.get("probe_role") or "representative"),
                "index": int(task.get("source_index", -1)),
                "filename": Path(source_path).name,
                "preview_key": preview_key,
                "preview_path": task.get("preview_path"),
            }
        )
        seen.add(source_key)
    return samples[:3]


def _preview_probe_indices(
    develop: Mapping[str, Any], group: Mapping[str, Any]
) -> list[dict[str, Any]]:
    by_path = {
        str(item.get("path") or "").casefold(): item
        for item in develop.get("items", [])
        if isinstance(item, Mapping) and item.get("path")
    }
    probes = group.get("probes") if isinstance(group.get("probes"), Mapping) else {}
    values: list[dict[str, Any]] = []
    seen: set[int] = set()
    for role in ("representative", "brightest", "darkest"):
        source = str(probes.get(role) or "")
        item = by_path.get(source.casefold())
        if not isinstance(item, Mapping):
            continue
        index = int(item.get("index", -1))
        if index < 0 or index in seen:
            continue
        values.append({"role": role, "index": index, "filename": Path(source).name})
        seen.add(index)
    return values[:3]


def _representative_index(
    develop: Mapping[str, Any], group: Mapping[str, Any]
) -> int | None:
    representative = str(group.get("probes", {}).get("representative") or "").casefold()
    return next(
        (
            int(item.get("index", -1))
            for item in develop.get("items", [])
            if isinstance(item, Mapping)
            and str(item.get("path") or "").casefold() == representative
        ),
        None,
    )


def _merge_develop(
    run_dir: Path,
    recommendation: dict[str, Any],
    *,
    base_revision: int,
    preview_only: bool,
) -> dict[str, Any]:
    develop_path = run_dir / "develop.json"
    develop = _read_mapping(develop_path, "裁剪调色方案")
    if int(develop.get("revision", -1)) != int(base_revision):
        raise StyleWorkerError("预览期间调色方案发生变化，已拒绝覆盖，请重新运行。")
    creative = (
        dict(develop.get("creative_style"))
        if isinstance(develop.get("creative_style"), dict)
        else {"status": "pending", "groups": {}}
    )
    groups = (
        dict(creative.get("groups")) if isinstance(creative.get("groups"), dict) else {}
    )
    scope = str(recommendation.get("scope") or "group")
    worker_state = (
        recommendation.get("worker")
        if isinstance(recommendation.get("worker"), Mapping)
        else {}
    )
    preserve_confirmed = bool(worker_state.get("preserve_confirmed"))
    original_groups = dict(groups)
    worker_group_ids = {
        str(value)
        for value in (recommendation.get("worker") or {}).get("group_ids", [])
    }
    result_rows = [
        result
        for result in recommendation.get("groups", [])
        if isinstance(result, dict)
    ]
    synthetic_global_group_id: str | None = None
    if scope == "global":
        apply_group_ids = [
            str(value) for value in recommendation.get("apply_group_ids", [])
        ]
        global_result = next(
            (
                result
                for result in result_rows
                if str(result.get("group_id")) in worker_group_ids
            ),
            None,
        )
        if not apply_group_ids or global_result is None:
            raise StyleWorkerError("全局统一推荐缺少目标照片组或真实预览结果。")
        synthetic_global_group_id = apply_group_ids[0]
        result_rows = [{**global_result, "group_id": synthetic_global_group_id}]
        worker_group_ids = {synthetic_global_group_id}
    for result in result_rows:
        group_key = str(result.get("group_id"))
        if group_key not in worker_group_ids:
            continue
        previous = dict(groups.get(group_key) or {})
        if str(result.get("recommendation_status")) == "failed":
            failure_message = str(
                result.get("error") or "Lightroom 风格预览失败"
            )[:2000]
            if preserve_confirmed and str(previous.get("status")) in {
                "confirmed",
                "skipped",
            }:
                groups[group_key] = {
                    **previous,
                    "group_id": int(group_key),
                    "last_attempt_status": "failed",
                    "last_attempt_error": failure_message,
                    "updated_at": _now(),
                }
            else:
                groups[group_key] = {
                    **previous,
                    "group_id": int(group_key),
                    "status": str(previous.get("status") or "pending"),
                    "recommendation_status": "failed",
                    "recommendation_error": failure_message,
                    "last_attempt_status": "failed",
                    "last_attempt_error": failure_message,
                    "updated_at": _now(),
                }
            continue
        selected = (
            result.get("selected") if isinstance(result.get("selected"), dict) else {}
        )
        selected_kind = str(selected.get("kind") or "neutral")
        top3 = [_public_candidate(item) for item in result.get("top3", [])[:3]]
        common = {
            "group_id": int(group_key),
            "recommendation_status": "complete",
            "recommended_kind": result.get("recommended_kind") or selected_kind,
            "recommended_preset_id": result.get("recommended_preset_id"),
            "recommended_preset_hash": result.get("recommended_preset_hash"),
            "recommended_amount": int(result.get("recommended_amount", 0)),
            "recommended_look_kind": result.get("recommended_look_kind"),
            "recommended_look_descriptor_hash": result.get(
                "recommended_look_descriptor_hash"
            ),
            "recommended_look_uuid": result.get("recommended_look_uuid"),
            "recommended_profile_name": result.get("recommended_profile_name"),
            "recommended_profile_hash": result.get("recommended_profile_hash"),
            "recommended_xmp_compatible": bool(
                result.get("recommended_xmp_compatible")
            ),
            "top3": top3,
            "neutral_score": result.get("neutral_score"),
            "neutral_preview_key": result.get("neutral_preview_key"),
            "neutral_preview_path": result.get("neutral_preview_path"),
            "confidence": result.get("confidence", 0.0),
            "reason": result.get("reason"),
            "missing_stages": result.get("missing_stages") or [],
            "ai_stages": result.get("ai_stages") or {},
            "recall_basis": result.get("recall_basis"),
            "representative_index": _representative_index(develop, result),
            "preview_probe_indices": _preview_probe_indices(develop, result),
            "updated_at": _now(),
        }
        if preview_only:
            forced_task = next(
                (
                    item
                    for item in result.get("render_tasks", [])
                    if item.get("render_status") == "ready"
                ),
                None,
            )
            if not isinstance(forced_task, dict):
                raise StyleWorkerError("用户选择的预设没有生成真实预览。")
            forced_candidate = next(
                (
                    dict(item)
                    for item in result.get("candidates", [])
                    if item.get("preset_id") == forced_task.get("preset_id")
                ),
                {},
            )
            forced_candidate.update(
                {
                    "preset_id": forced_task.get("preset_id"),
                    "preset_hash": forced_task.get("preset_hash"),
                    "preset_uuid": forced_task.get("preset_uuid"),
                    "preset_scope": forced_task.get("preset_scope"),
                    "look_kind": forced_task.get("look_kind") or "lightroom_preset",
                    "look_descriptor": forced_task.get("look_descriptor"),
                    "look_descriptor_hash": forced_task.get("look_descriptor_hash"),
                    "look_uuid": forced_task.get("look_uuid"),
                    "profile_name": forced_task.get("profile_name"),
                    "profile_hash": forced_task.get("profile_hash"),
                    "xmp_compatible": bool(forced_task.get("xmp_compatible")),
                    "amount": int(forced_task.get("amount", 100)),
                    "render_status": "ready",
                    "preview_key": forced_task.get("preview_key"),
                    "preview_path": forced_task.get("preview_path"),
                    "preview_samples": _ready_preview_samples(result),
                }
            )
            visible = [
                item
                for item in previous.get("top3", [])
                if isinstance(item, dict)
                and item.get("preset_id") != forced_candidate.get("preset_id")
            ]
            visible.insert(0, _public_candidate(forced_candidate))
            groups[group_key] = {
                **previous,
                **{
                    key: value
                    for key, value in common.items()
                    if key
                    not in {
                        "recommended_kind",
                        "recommended_preset_id",
                        "recommended_preset_hash",
                        "recommended_amount",
                        "recommended_look_kind",
                        "recommended_look_descriptor_hash",
                        "recommended_look_uuid",
                        "recommended_profile_name",
                        "recommended_profile_hash",
                        "recommended_xmp_compatible",
                        "confidence",
                        "reason",
                        "neutral_score",
                    }
                    or key not in previous
                },
                "status": "confirmed",
                "preset_id": forced_candidate.get("preset_id"),
                "preset_hash": forced_candidate.get("preset_hash"),
                "preset_uuid": forced_candidate.get("preset_uuid"),
                "preset_scope": forced_candidate.get("preset_scope"),
                "look_kind": forced_candidate.get("look_kind") or "lightroom_preset",
                "look_descriptor_hash": forced_candidate.get("look_descriptor_hash"),
                "look_uuid": forced_candidate.get("look_uuid"),
                "profile_name": forced_candidate.get("profile_name"),
                "profile_hash": forced_candidate.get("profile_hash"),
                "xmp_compatible": bool(forced_candidate.get("xmp_compatible")),
                "amount": int(forced_candidate.get("amount", 100)),
                "amount_supported": forced_candidate.get("amount_supported", False),
                "selected_preview_key": forced_candidate.get("preview_key"),
                "selected_preview_path": forced_candidate.get("preview_path"),
                "selected_preview_samples": forced_candidate.get("preview_samples", []),
                "top3": visible[:4],
                "manual_override": True,
            }
        else:
            chosen = (
                next(
                    (
                        item
                        for item in result.get("top3", [])
                        if item.get("preset_id") == selected.get("preset_id")
                    ),
                    None,
                )
                if selected_kind == "preset"
                else {
                    "preview_key": result.get("neutral_preview_key"),
                    "preview_path": result.get("neutral_preview_path"),
                }
            )
            if preserve_confirmed and str(previous.get("status")) in {
                "confirmed",
                "skipped",
            }:
                # Batch recommendation refreshes only the AI evidence. A frozen
                # human choice remains authoritative until the user changes it.
                preserved_common = dict(common)
                if str(previous.get("status")) == "confirmed":
                    preserved_common["top3"] = _top3_with_preserved_selection(
                        previous, common["top3"]
                    )
                groups[group_key] = {**previous, **preserved_common}
            else:
                groups[group_key] = {
                    **previous,
                    **common,
                    "status": "pending",
                    "preset_id": None,
                    "preset_hash": None,
                    "preset_uuid": None,
                    "preset_scope": None,
                    "amount": int(selected.get("amount", 0)),
                    "amount_supported": False,
                    "selected_preview_key": chosen.get("preview_key")
                    if isinstance(chosen, Mapping)
                    else None,
                    "selected_preview_path": chosen.get("preview_path")
                    if isinstance(chosen, Mapping)
                    else None,
                    "manual_override": False,
                }
        # A successful rerun supersedes any failure message retained from an
        # older attempt.  Keeping it would make a ready preview look failed in
        # clients that surface the diagnostic field.
        groups[group_key].pop("recommendation_error", None)
        groups[group_key].pop("last_attempt_status", None)
        groups[group_key].pop("last_attempt_error", None)
    all_group_ids = {
        str(int(item.get("group_id", -1)))
        for item in develop.get("items", [])
        if isinstance(item, Mapping)
    }
    if scope == "global":
        assert synthetic_global_group_id is not None
        global_selection = dict(groups[synthetic_global_group_id])
        global_selection.pop("group_id", None)
        global_selection["scope"] = "global"
        creative["groups"] = original_groups
        creative["global_selection"] = global_selection
        creative["scope"] = "global"
        # A preview-only global request is an explicit user choice (candidate
        # or fixed strength tier), not passive recommendation generation. The
        # exact Lightroom samples therefore complete the global style step in
        # the same action instead of leaving Next disabled behind a redundant
        # second confirmation click.
        creative["status"] = (
            "confirmed"
            if preview_only
            and str(global_selection.get("status")) in {"confirmed", "skipped"}
            else "pending"
        )
    else:
        creative["groups"] = groups
        creative["scope"] = "group"
        creative["status"] = (
            "confirmed"
            if all_group_ids
            and all(
                str(groups.get(key, {}).get("status", "pending"))
                in {"confirmed", "skipped"}
                for key in all_group_ids
            )
            else "pending"
        )
    creative["updated_at"] = _now()
    develop["creative_style"] = creative
    develop["basic_color"] = {"status": "enabled"}
    develop["color_enabled"] = True
    develop["color_mode"] = "style"
    develop["revision"] = int(base_revision) + 1
    develop["updated_at"] = _now()
    write_json(develop_path, develop)
    return develop


def _record_failure(
    run_dir: Path,
    error: Exception,
    *,
    base_revision: int,
    group_id: int | None,
) -> None:
    message = str(error)[:2000]
    recommendation_path = run_dir / STYLE_RECOMMENDATION_PATH
    if recommendation_path.is_file():
        try:
            plan = _read_mapping(recommendation_path, "风格推荐计划")
            plan["status"] = "failed"
            worker = plan.get("worker") if isinstance(plan.get("worker"), dict) else {}
            worker.update(status="failed", error=message, updated_at=_now())
            plan["worker"] = worker
            for group in plan.get("groups", []):
                if group_id is None or str(group.get("group_id")) == str(group_id):
                    group["recommendation_status"] = "failed"
                    group["error"] = message
            plan["updated_at"] = _now()
            write_json(recommendation_path, plan)
        except (OSError, TypeError, ValueError, StyleWorkerError):
            pass
    develop_path = run_dir / "develop.json"
    if not develop_path.is_file():
        return
    try:
        develop = _read_mapping(develop_path, "裁剪调色方案")
        if int(develop.get("revision", -1)) != int(base_revision):
            return
        creative = (
            dict(develop.get("creative_style"))
            if isinstance(develop.get("creative_style"), dict)
            else {"status": "pending", "groups": {}}
        )
        groups = dict(creative.get("groups") or {})
        target_ids = (
            {str(group_id)}
            if group_id is not None
            else {
                str(int(item.get("group_id", -1)))
                for item in develop.get("items", [])
                if isinstance(item, Mapping)
            }
        )
        for key in target_ids:
            groups[key] = {
                **dict(groups.get(key) or {}),
                "group_id": int(key),
                "recommendation_status": "failed",
                "recommendation_error": message,
                "updated_at": _now(),
            }
        creative["groups"] = groups
        creative["status"] = "pending"
        creative["updated_at"] = _now()
        develop["creative_style"] = creative
        develop["revision"] = int(base_revision) + 1
        develop["updated_at"] = _now()
        write_json(develop_path, develop)
    except (OSError, TypeError, ValueError, StyleWorkerError):
        return


def run_style_worker(
    run_dir: Path | str,
    data_dir: Path | str,
    batch_id: str,
    base_revision: int,
    *,
    scope: str | None = None,
    group_id: int | None = None,
    preset_id: str | None = None,
    preset_hash: str | None = None,
    lut_id: str | None = None,
    lut_hash: str | None = None,
    amount: int = 100,
    preview_only: bool = False,
    preserve_confirmed: bool = False,
    lightroom_exe: Path | str | None = None,
    startup_timeout: float = 90.0,
    batch_timeout: float = 1800.0,
    catalog: dict[str, Any] | None = None,
    bridge_status_reader: BridgeStatusReader = read_lightroom_bridge_status,
    batch_creator: BatchCreator = create_lightroom_batch,
    batch_waiter: BatchWaiter = wait_for_lightroom_batch,
    batch_status_reader: BatchStatusReader = read_lightroom_batch_status,
    scorer_factory: ScorerFactory = GeneralAestheticScorer,
    cascade_runner: CascadeRunner | None = None,
    lut_engine_factory: LutEngineFactory = _default_lut_engine_factory,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Render, verify, score, and persist one exact style recommendation run.

    The positional parameters intentionally match the ``style-recommend`` CLI
    contract.  Tests and local orchestration may inject bridge/scorer callables;
    production callers normally use only the CLI-shaped arguments.
    """

    run_root = Path(run_dir).expanduser().resolve()
    data_root = Path(data_dir).expanduser().resolve()
    try:
        resolved_scope = str(scope or ("global" if group_id == 0 else "group"))
        if resolved_scope == "global" and group_id == 0:
            group_id = None
        preset_pair = bool(preset_id) or bool(preset_hash)
        lut_pair = bool(lut_id) or bool(lut_hash)
        if bool(lut_id) != bool(lut_hash):
            raise StyleWorkerError("LUT ID 与精确文件哈希必须同时提供。")
        if preset_pair and lut_pair:
            raise StyleWorkerError("不能同时指定 Lightroom 预设和 .cube LUT。")
        if lut_pair:
            if not preview_only:
                raise StyleWorkerError(".cube LUT 当前只支持精确预览。")
            if group_id is None:
                raise StyleWorkerError(".cube LUT 预览必须指定一个照片组。")
            if not 0 <= int(amount) <= 200:
                raise StyleWorkerError("LUT 强度必须是 0 到 200。")
            result = _run_lut_preview(
                run_root,
                data_root,
                batch_id,
                base_revision,
                group_id=group_id,
                lut_id=str(lut_id),
                lut_hash=str(lut_hash),
                amount=int(amount),
                engine_factory=lut_engine_factory,
                progress=progress,
            )
            if progress is not None:
                progress(
                    {
                        "status": "completed",
                        "phase": "finalize",
                        "stage_label": "创意 LUT 预览已完成",
                        "completed": 1,
                        "current": 1,
                        "total": 1,
                        "overall_percent": 100.0,
                        "updated_at": _now(),
                    }
                )
            return result
        _recover_cancelled_multi_group_previews(
            run_root,
            data_root,
            base_revision=base_revision,
            status_reader=batch_status_reader,
            # Recovery only adopts verified immutable cache files.  Reporting
            # its local copies as the later ``collect`` stage would move the
            # pipeline past ``process`` before the fresh Lightroom batch has
            # even started.  The new plan immediately reports the adopted
            # files as cached ``process`` progress with the correct full total.
            progress=None,
        )
        started = start_style_preview_batch(
            run_root,
            data_root,
            batch_id,
            base_revision,
            scope=resolved_scope,
            group_id=group_id,
            preset_id=preset_id,
            preset_hash=preset_hash,
            amount=amount,
            preview_only=preview_only,
            preserve_confirmed=preserve_confirmed,
            catalog=catalog,
            lightroom_exe=lightroom_exe,
            startup_timeout=startup_timeout,
            status_reader=bridge_status_reader,
            batch_creator=batch_creator,
            cascade_runner=cascade_runner,
            progress=progress,
        )
        plan = started["plan"]
        published = int(started["published_count"])
        recommendation_cached = bool(started.get("recommendation_cached"))
        cancelled_batch = False
        if published:
            allow_partial = False
            try:
                batch_status = batch_waiter(
                    data_root,
                    batch_id,
                    batch_timeout,
                    status_reader=batch_status_reader,
                    progress_phase="process",
                    progress_label="Lightroom 真实预览",
                    progress_unit="项预览",
                    progress_offset=int(started["cached_count"]),
                    progress_total=(
                        int(started["cached_count"]) + published
                    ),
                )
            except LightroomApplyError:
                batch_status = batch_status_reader(data_root, batch_id)
                worker_group_ids = {
                    str(value)
                    for value in (plan.get("worker") or {}).get("group_ids", [])
                }
                allow_partial = (
                    resolved_scope == "group"
                    and len(worker_group_ids) > 1
                    and str(batch_status.get("status") or "") == "failed"
                )
                cancelled_batch = (
                    resolved_scope == "group"
                    and len(worker_group_ids) > 1
                    and str(batch_status.get("status") or "")
                    in {"cancelling", "cancelled"}
                    and isinstance(batch_status.get("cancellation"), Mapping)
                )
                if not (allow_partial or cancelled_batch):
                    raise
            plan = collect_style_preview_batch(
                run_root,
                data_root,
                batch_id,
                batch_status=batch_status,
                status_reader=batch_status_reader,
                allow_partial=allow_partial,
                allow_cancelled=cancelled_batch,
                progress=progress,
            )
        worker = plan.get("worker") if isinstance(plan.get("worker"), dict) else {}
        target_ids = {str(value) for value in worker.get("group_ids", [])}
        if preview_only:
            for group in plan.get("groups", []):
                if str(group.get("group_id")) not in target_ids:
                    continue
                forced_task = next(
                    (
                        item
                        for item in group.get("render_tasks", [])
                        if item.get("render_status") == "ready"
                    ),
                    None,
                )
                if not isinstance(forced_task, dict):
                    raise StyleWorkerError("用户选择的风格预览没有完成。")
                neutral_task = group.get("neutral_render_task")
                if isinstance(neutral_task, dict):
                    group["neutral_preview_key"] = neutral_task.get("preview_key")
                    group["neutral_preview_path"] = neutral_task.get("preview_path")
                group["selected"] = {
                    "kind": "preset",
                    "preset_id": forced_task.get("preset_id"),
                    "preset_hash": forced_task.get("preset_hash"),
                    "look_kind": forced_task.get("look_kind") or "develop_preset",
                    "profile_name": forced_task.get("profile_name"),
                    "profile_hash": forced_task.get("profile_hash"),
                    "xmp_compatible": bool(forced_task.get("xmp_compatible")),
                    "amount": int(forced_task.get("amount", amount)),
                    "preview_key": forced_task.get("preview_key"),
                    "preview_path": forced_task.get("preview_path"),
                    "preview_samples": _ready_preview_samples(group),
                }
                if forced_task.get("look_kind") == "lightroom_profile":
                    group["selected"].update(
                        look_descriptor=forced_task.get("look_descriptor"),
                        look_descriptor_hash=forced_task.get("look_descriptor_hash"),
                        look_uuid=forced_task.get("look_uuid"),
                    )
                group["manual_override"] = True
                group["status"] = "complete"
                group["recommendation_status"] = "complete"
                group["missing_stages"] = [
                    str(value)
                    for value in group.get("missing_stages", [])
                    if str(value) not in {"lightroom_exact_preview", "qrealign_rerank"}
                ]
        elif not recommendation_cached:
            scorer = scorer_factory(data_root)
            try:
                score_groups = [
                    group
                    for group in plan.get("groups", [])
                    if str(group.get("group_id")) in target_ids
                    and str(group.get("recommendation_status")) != "failed"
                ]
                for index, group in enumerate(score_groups, start=1):
                    try:
                        _score_group(group, scorer)
                    except Exception as exc:
                        if resolved_scope != "group" or len(target_ids) <= 1:
                            raise
                        group_key = str(group.get("group_id"))
                        group["recommendation_status"] = "failed"
                        group["error"] = str(exc)[:2000] or type(exc).__name__
                        failed_ids = {
                            str(value) for value in worker.get("failed_group_ids", [])
                        }
                        failed_ids.add(group_key)
                        worker["failed_group_ids"] = sorted(
                            failed_ids, key=lambda value: int(value)
                        )
                        worker["successful_group_ids"] = [
                            str(value)
                            for value in worker.get("successful_group_ids", [])
                            if str(value) != group_key
                        ]
                    _notify(
                        progress,
                        "rerank",
                        "Q-ReAlign 复评真实 Lightroom 预览",
                        index,
                        len(score_groups),
                    )
            finally:
                scorer.release()
        failed_group_ids = [
            str(value) for value in worker.get("failed_group_ids", [])
        ]
        succeeded_group_ids = sorted(
            target_ids.difference(failed_group_ids),
            key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
        )
        worker["successful_group_ids"] = succeeded_group_ids
        worker["failed_group_ids"] = failed_group_ids
        cancelled_batch = cancelled_batch or bool(worker.get("cancelled"))
        plan["status"] = (
            "cancelled"
            if cancelled_batch
            else "partial"
            if failed_group_ids
            else "complete"
        )
        worker["status"] = plan["status"]
        worker["updated_at"] = _now()
        plan["worker"] = worker
        plan["updated_at"] = _now()
        write_json(run_root / STYLE_RECOMMENDATION_PATH, plan)
        if not preview_only and plan["status"] == "complete":
            _save_request_cache(run_root, plan)
        develop = _merge_develop(
            run_root,
            plan,
            base_revision=base_revision,
            preview_only=preview_only,
        )
        finalize_label = (
            f"任务已取消，保留 {len(succeeded_group_ids)} 个完整组的安全预览"
            if cancelled_batch
            else f"已保存推荐，{len(failed_group_ids)} 个组可重跑"
            if failed_group_ids
            else "保存真实风格推荐"
        )
        _notify(progress, "finalize", finalize_label, 1, 1)
        if progress is not None:
            progress(
                {
                    "status": "cancelled" if cancelled_batch else "completed",
                    "phase": "finalize",
                    "stage_label": (
                        f"已取消；{len(succeeded_group_ids)} 个组完成，"
                        f"{len(failed_group_ids)} 个组可重跑"
                        if cancelled_batch
                        else f"推荐已保存，{len(failed_group_ids)} 个组失败"
                        if failed_group_ids
                        else "真实风格预览与推荐已完成"
                    ),
                    "completed": 1,
                    "current": 1,
                    "total": 1,
                    "overall_percent": 100.0,
                    "updated_at": _now(),
                }
            )
        return {
            "status": plan["status"],
            "batch_id": started.get("batch_id"),
            "published_count": published,
            "cached_count": int(started["cached_count"]),
            "recommendation_cached": recommendation_cached,
            "succeeded_group_count": len(succeeded_group_ids),
            "failed_group_count": len(failed_group_ids),
            "recommendation": plan,
            "develop": develop,
        }
    except Exception as exc:
        _record_failure(
            run_root,
            exc,
            base_revision=base_revision,
            group_id=group_id,
        )
        if progress is not None:
            progress(
                {
                    "status": "failed",
                    "phase": "failed",
                    "stage_label": "真实风格推荐失败",
                    "message": str(exc),
                    "overall_percent": 100.0,
                    "updated_at": _now(),
                }
            )
        else:
            emit_cli_progress(
                "finalize",
                "真实风格推荐失败",
                1,
                1,
                unit="项",
                event="phase_end",
            )
        if isinstance(exc, StyleWorkerError):
            raise
        raise StyleWorkerError(str(exc) or type(exc).__name__) from exc


run_style_recommendation = run_style_worker
