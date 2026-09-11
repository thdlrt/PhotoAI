from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, unquote

from .util import atomic_create_text, atomic_write_text, read_json, write_json

BRIDGE_DIRECTORY = "lightroom-bridge"
TASK_PROTOCOL = "PHOTO_AI_LR_TASK/1"
RESULT_PROTOCOL = "PHOTO_AI_LR_RESULT/1"
HEARTBEAT_PROTOCOL = "PHOTO_AI_LR_HEARTBEAT/1"
CONFIG_PROTOCOL = "PHOTO_AI_LR_CONFIG/1"
CANCEL_PROTOCOL = "PHOTO_AI_LR_CANCEL/1"
PRESET_PROTOCOL = "PHOTO_AI_LR_PRESET/1"
LOOK_DESCRIPTOR_PROTOCOL = "PHOTO_AI_LR_LOOK/1"
LOOK_DESCRIPTOR_ENTRY_PROTOCOL = "PHOTO_AI_LR_LOOK_ENTRY/1"
PLUGIN_VERSION = "0.3.8"
PLUGIN_OWNERSHIP_FILE = ".photoai-installed.json"
LIGHTROOM_MINIMUM_VERSION = (14, 3)
LIGHTROOM_VALIDATED_VERSION = (15, 3)

MAX_LOOK_DESCRIPTOR_BYTES = 4 * 1024 * 1024
MAX_LOOK_DESCRIPTOR_NODES = 10_000
MAX_LOOK_DESCRIPTOR_DEPTH = 16

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SAFE_SETTING = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
_SAFE_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,95}$")
_SAFE_LOOK_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SHA256 = re.compile(r"^[A-Fa-f0-9]{64}$")
_LOOK_ROOT_FIELDS = {
    "Amount",
    "Cluster",
    "Group",
    "Name",
    "Parameters",
    "SupportsAmount",
    "UUID",
}
_CROP_ALIASES = {
    "left": "CropLeft",
    "top": "CropTop",
    "right": "CropRight",
    "bottom": "CropBottom",
    "angle": "CropAngle",
    "CropLeft": "CropLeft",
    "CropTop": "CropTop",
    "CropRight": "CropRight",
    "CropBottom": "CropBottom",
    "CropAngle": "CropAngle",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BridgePaths:
    root: Path
    pending: Path
    running: Path
    done: Path
    failed: Path
    cancelled: Path
    backups: Path
    batches: Path
    logs: Path
    previews: Path
    exports: Path
    presets: Path
    heartbeat: Path
    config: Path


@dataclass(frozen=True)
class LightroomTask:
    photo_path: Path | str | None
    rating: int = 0
    auto_tone: bool = True
    auto_white_balance: bool = True
    lens_profile: bool = True
    remove_chromatic_aberration: bool = True
    crop: Mapping[str, Any] | None = None
    style: Mapping[str, Any] | None = None
    task_id: str | None = None
    task_type: str = "apply"
    output_mode: str | None = None
    preset_uuid: str | None = None
    preset_scope: str = "catalog"
    preset_amount: int = 100
    look_descriptor_path: Path | str | None = None
    look_descriptor_hash: str | None = None
    look_uuid: str | None = None
    look_amount: int = 100
    jpeg_output_dir: Path | str | None = None
    source_batch_id: str | None = None
    source_task_id: str | None = None


def bridge_paths(data_dir: Path | str, *, create: bool = False) -> BridgePaths:
    """Return the complete bridge layout rooted below the caller's data directory.

    No queue state is written to a user profile or to the Lightroom installation.
    The caller is responsible for passing the configured application state
    directory; desktop builds keep it below ``PHOTO_AI_CONTENT_ROOT``.
    """

    root = Path(data_dir).expanduser().resolve() / BRIDGE_DIRECTORY
    paths = BridgePaths(
        root=root,
        pending=root / "pending",
        running=root / "running",
        done=root / "done",
        failed=root / "failed",
        cancelled=root / "cancelled",
        backups=root / "backups",
        batches=root / "batches",
        logs=root / "logs",
        previews=root / "previews",
        exports=root / "exports",
        presets=root / "presets",
        heartbeat=root / "heartbeat.line",
        config=root / "plugin-config.line",
    )
    if create:
        for directory in (
            paths.root,
            paths.pending,
            paths.running,
            paths.done,
            paths.failed,
            paths.cancelled,
            paths.backups,
            paths.batches,
            paths.logs,
            paths.previews,
            paths.exports,
            paths.presets,
        ):
            directory.mkdir(parents=True, exist_ok=True)
    return paths


def _validate_identifier(value: str, label: str) -> str:
    value = str(value).strip()
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{label} must contain only letters, digits, dot, underscore, or hyphen"
        )
    return value


def _encode_scalar(value: Any) -> str:
    if isinstance(value, bool):
        encoded = "b|1" if value else "b|0"
    elif isinstance(value, int):
        encoded = f"i|{value}"
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                "non-finite values are not supported by the Lightroom bridge"
            )
        encoded = f"f|{value:.12g}"
    elif isinstance(value, (str, Path)):
        encoded = f"s|{value}"
    else:
        raise TypeError(f"unsupported bridge value type: {type(value).__name__}")
    return quote(encoded, safe="")


def _decode_scalar(value: str) -> bool | int | float | str:
    decoded = unquote(value)
    if len(decoded) < 2 or decoded[1] != "|":
        raise ValueError("invalid typed field")
    kind, payload = decoded[0], decoded[2:]
    if kind == "b":
        if payload not in {"0", "1"}:
            raise ValueError("invalid boolean field")
        return payload == "1"
    if kind == "i":
        return int(payload)
    if kind == "f":
        result = float(payload)
        if not math.isfinite(result):
            raise ValueError("invalid numeric field")
        return result
    if kind == "s":
        return payload
    raise ValueError(f"unknown field type {kind!r}")


def _line(protocol: str, fields: Mapping[str, Any]) -> str:
    parts = [protocol]
    for key, value in fields.items():
        if not _SAFE_FIELD.fullmatch(key):
            raise ValueError(f"invalid protocol field {key!r}")
        parts.append(f"{key}={_encode_scalar(value)}")
    return "\t".join(parts) + "\n"


def _parse_line(line: str, protocol: str) -> dict[str, Any]:
    parts = line.rstrip("\r\n").split("\t")
    if not parts or parts[0] != protocol:
        raise ValueError(f"expected {protocol}")
    fields: dict[str, Any] = {}
    for field in parts[1:]:
        key, separator, value = field.partition("=")
        if not separator or not key or key in fields:
            raise ValueError("invalid or duplicate protocol field")
        fields[key] = _decode_scalar(value)
    return fields


def _normalize_crop(crop: Mapping[str, Any] | None) -> dict[str, int | float]:
    if not crop:
        return {}
    result: dict[str, int | float] = {}
    for key, value in crop.items():
        canonical = _CROP_ALIASES.get(str(key))
        if canonical is None:
            raise ValueError(f"unsupported crop field {key!r}")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"crop field {key!r} must be a finite number")
        result[canonical] = value
    bounds = [
        result.get(key) for key in ("CropLeft", "CropTop", "CropRight", "CropBottom")
    ]
    if any(value is not None for value in bounds):
        if any(value is None for value in bounds):
            raise ValueError("crop bounds must include left, top, right, and bottom")
        left, top, right, bottom = (float(value) for value in bounds)
        if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
            raise ValueError("crop bounds must be ordered values between 0 and 1")
    if "CropAngle" in result and not -45.0 <= float(result["CropAngle"]) <= 45.0:
        raise ValueError("crop angle must be between -45 and 45 degrees")
    return result


def _normalize_style(
    style: Mapping[str, Any] | None,
) -> dict[str, bool | int | float | str]:
    result: dict[str, bool | int | float | str] = {}
    for key, value in (style or {}).items():
        key = str(key)
        if not _SAFE_SETTING.fullmatch(key):
            raise ValueError(f"invalid Lightroom setting name {key!r}")
        if not isinstance(value, (bool, int, float, str)):
            raise TypeError(f"Lightroom setting {key!r} must be a scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Lightroom setting {key!r} must be finite")
        result[key] = value
    return result


def _normalize_look_uuid(value: Any, label: str = "look_uuid") -> str:
    return _validate_identifier(str(value), label)


def _normalize_look_hash(value: Any) -> str:
    digest = str(value).strip().lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError("look_descriptor_hash must be a 64-character SHA-256 digest")
    return digest


def _normalize_look_amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 200:
        raise ValueError("look_amount must be an integer from 0 to 200")
    return value


def _normalize_look_descriptor_path(
    value: Path | str,
    digest: str,
    *,
    expected_root: Path | str | None = None,
) -> str:
    text = str(value).strip()
    path = PureWindowsPath(text)
    if (
        not text
        or not path.is_absolute()
        or not path.drive
        or text.startswith(("\\\\", "//"))
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise ValueError(
            "look_descriptor_path must be an absolute local Windows path without relative segments"
        )
    lowered = [part.casefold() for part in path.parts]
    if len(lowered) < 5 or lowered[-4:-1] != ["lightroom-bridge", "presets", "looks"]:
        raise ValueError(
            "look_descriptor_path must be directly inside lightroom-bridge\\presets\\looks"
        )
    if path.name.casefold() != f"{digest}.look":
        raise ValueError(
            "look descriptor filename must equal its SHA-256 digest plus .look"
        )
    if expected_root is not None:
        actual_parent = PureWindowsPath(str(path.parent))
        allowed_parent = PureWindowsPath(str(expected_root))
        if (
            str(actual_parent).replace("/", "\\").casefold()
            != str(allowed_parent).replace("/", "\\").casefold()
        ):
            raise ValueError(
                "look_descriptor_path is outside the configured bridge root"
            )
    return str(path)


def _look_path_segment(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("look descriptor keys cannot be boolean")
    segment = str(value)
    if not _SAFE_LOOK_PATH_SEGMENT.fullmatch(segment):
        raise ValueError(f"invalid look descriptor path segment {segment!r}")
    if segment.isdigit() and (segment == "0" or segment.startswith("0")):
        raise ValueError(
            "look descriptor numeric paths must be positive canonical indices"
        )
    return segment


def _flatten_look_descriptor(
    value: Any,
    *,
    path: tuple[str, ...] = (),
    depth: int = 0,
    rows: list[tuple[str, str, Any | None]],
) -> None:
    if depth > MAX_LOOK_DESCRIPTOR_DEPTH:
        raise ValueError("look descriptor is nested too deeply")
    if len(rows) >= MAX_LOOK_DESCRIPTOR_NODES:
        raise ValueError("look descriptor contains too many nodes")
    encoded_path = "/".join(path)
    if isinstance(value, Mapping):
        if path:
            rows.append((encoded_path, "table", None))
        items = sorted(
            ((_look_path_segment(key), item) for key, item in value.items()),
            key=lambda pair: pair[0],
        )
        for key, item in items:
            _flatten_look_descriptor(
                item,
                path=(*path, key),
                depth=depth + 1,
                rows=rows,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if path:
            rows.append((encoded_path, "table", None))
        for index, item in enumerate(value, start=1):
            _flatten_look_descriptor(
                item,
                path=(*path, str(index)),
                depth=depth + 1,
                rows=rows,
            )
        return
    if not path:
        raise ValueError("look descriptor root must be a table")
    if not isinstance(value, (bool, int, float, str)):
        raise TypeError(
            f"unsupported look descriptor value type: {type(value).__name__}"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("look descriptor numbers must be finite")
    rows.append((encoded_path, "value", value))


def serialize_look_descriptor(look: Mapping[str, Any], *, look_uuid: str) -> str:
    """Serialize a data-only Lightroom Creative Look descriptor.

    The descriptor is never executable Lua. Nested tables are represented by
    typed, percent-encoded path/value records so the plug-in can rebuild the
    exact ``Look`` table without ``loadfile``/``dofile``.
    """

    uuid_value = _normalize_look_uuid(look_uuid)
    if "SchemaVersion" in look:
        source_hash = str(look.get("Hash") or "").lower()
        unhashed = {key: value for key, value in look.items() if key != "Hash"}
        expected_source_hash = hashlib.sha256(
            json.dumps(
                unhashed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", "surrogatepass")
        ).hexdigest()
        if not _SHA256.fullmatch(source_hash) or source_hash != expected_source_hash:
            raise ValueError(
                "compact Look descriptor Hash does not match its canonical payload"
            )
        group = look.get("Group", "")
        look = {
            "Amount": 1,
            "Cluster": look.get("Cluster", ""),
            "Group": {"x-default": group},
            "Name": look.get("Name", ""),
            "Parameters": look.get("Parameters", {}),
            "SupportsAmount": bool(look.get("SupportsAmount", False)),
            "UUID": look.get("UUID"),
        }
    if str(look.get("UUID", "")) != uuid_value:
        raise ValueError("look descriptor UUID must match look_uuid")
    descriptor_amount = look.get("Amount")
    if (
        isinstance(descriptor_amount, bool)
        or not isinstance(descriptor_amount, (int, float))
        or not math.isfinite(float(descriptor_amount))
        or not 0 <= float(descriptor_amount) <= 2
    ):
        raise ValueError("look descriptor Amount must be a finite factor from 0 to 2")
    rows: list[tuple[str, str, Any | None]] = []
    _flatten_look_descriptor(look, rows=rows)
    if not rows or len(rows) > MAX_LOOK_DESCRIPTOR_NODES:
        raise ValueError("look descriptor must contain at least one node")
    lines = [
        _line(
            LOOK_DESCRIPTOR_PROTOCOL,
            {"uuid": uuid_value, "entry_count": len(rows)},
        ).rstrip("\n")
    ]
    for path, kind, value in rows:
        fields: dict[str, Any] = {"path": path, "kind": kind}
        if kind == "value":
            fields["value"] = value
        lines.append(_line(LOOK_DESCRIPTOR_ENTRY_PROTOCOL, fields).rstrip("\n"))
    result = "\n".join(lines) + "\n"
    if len(result.encode("utf-8")) > MAX_LOOK_DESCRIPTOR_BYTES:
        raise ValueError("look descriptor exceeds the 4 MiB safety limit")
    return result


def parse_look_descriptor(
    contents: str, *, expected_uuid: str | None = None
) -> dict[str, Any]:
    """Parse and validate a data-only Creative Look descriptor."""

    if len(contents.encode("utf-8")) > MAX_LOOK_DESCRIPTOR_BYTES:
        raise ValueError("look descriptor exceeds the 4 MiB safety limit")
    lines = contents.splitlines()
    if not lines:
        raise ValueError("look descriptor is empty")
    header = _parse_line(lines[0], LOOK_DESCRIPTOR_PROTOCOL)
    if set(header) != {"uuid", "entry_count"}:
        raise ValueError("look descriptor header contains unsupported fields")
    uuid_value = _normalize_look_uuid(header["uuid"], "look descriptor uuid")
    if expected_uuid is not None and uuid_value != _normalize_look_uuid(expected_uuid):
        raise ValueError("look descriptor UUID does not match the requested Look")
    entry_count = header["entry_count"]
    if (
        isinstance(entry_count, bool)
        or not isinstance(entry_count, int)
        or not 1 <= entry_count <= MAX_LOOK_DESCRIPTOR_NODES
        or len(lines) != entry_count + 1
    ):
        raise ValueError("look descriptor entry_count does not match its records")
    root: dict[Any, Any] = {}
    seen: set[str] = set()
    for line in lines[1:]:
        fields = _parse_line(line, LOOK_DESCRIPTOR_ENTRY_PROTOCOL)
        if set(fields).difference({"path", "kind", "value"}):
            raise ValueError("look descriptor entry contains unsupported fields")
        path = fields.get("path")
        kind = fields.get("kind")
        if not isinstance(path, str) or not path or path in seen:
            raise ValueError("look descriptor paths must be unique, non-empty strings")
        seen.add(path)
        segments = path.split("/")
        if len(segments) > MAX_LOOK_DESCRIPTOR_DEPTH:
            raise ValueError("look descriptor is nested too deeply")
        keys: list[str | int] = []
        for segment in segments:
            segment = _look_path_segment(segment)
            keys.append(int(segment) if segment.isdigit() else segment)
        parent: dict[Any, Any] = root
        for key in keys[:-1]:
            child = parent.get(key)
            if not isinstance(child, dict):
                raise ValueError(
                    "look descriptor table parents must precede their children"
                )
            parent = child
        leaf = keys[-1]
        if leaf in parent:
            raise ValueError("look descriptor path collides with an existing node")
        if kind == "table" and "value" not in fields:
            parent[leaf] = {}
        elif kind == "value" and "value" in fields:
            parent[leaf] = fields["value"]
        else:
            raise ValueError("look descriptor entry kind/value shape is invalid")
    if root.get("UUID") != uuid_value:
        raise ValueError("look descriptor body UUID does not match its header")
    unknown_root = set(root).difference(_LOOK_ROOT_FIELDS)
    if unknown_root:
        raise ValueError(f"unsupported Look root field: {min(unknown_root)!r}")
    amount = root.get("Amount")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(float(amount))
        or not 0 <= float(amount) <= 2
    ):
        raise ValueError(
            "look descriptor body Amount must be a finite factor from 0 to 2"
        )
    if not isinstance(root.get("Parameters"), dict):
        raise ValueError("look descriptor Parameters must be a table")
    return root


def look_descriptor_sha256(contents: str | bytes) -> str:
    payload = contents.encode("utf-8") if isinstance(contents, str) else bytes(contents)
    if len(payload) > MAX_LOOK_DESCRIPTOR_BYTES:
        raise ValueError("look descriptor exceeds the 4 MiB safety limit")
    return hashlib.sha256(payload).hexdigest()


def write_lightroom_look_descriptor(
    data_dir: Path | str,
    look: Mapping[str, Any],
    *,
    look_uuid: str,
) -> dict[str, Any]:
    """Publish one immutable Creative Look descriptor below the configured bridge."""

    data_root = Path(data_dir).expanduser().resolve()
    if not data_root.is_absolute() or str(data_root).startswith(("\\\\", "//")):
        raise ValueError(
            "Lightroom Look descriptors require a local absolute data directory"
        )
    paths = bridge_paths(data_dir, create=True)
    contents = serialize_look_descriptor(look, look_uuid=look_uuid)
    digest = look_descriptor_sha256(contents)
    directory = paths.presets / "looks"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.look"
    if target.is_file():
        if target.read_bytes() != contents.encode("utf-8"):
            raise RuntimeError(
                "content-addressed Lightroom Look descriptor does not match its filename"
            )
    else:
        try:
            atomic_create_text(target, contents)
        except FileExistsError:
            if target.read_bytes() != contents.encode("utf-8"):
                raise RuntimeError(
                    "content-addressed Lightroom Look descriptor appeared with different bytes"
                ) from None
    _normalize_look_descriptor_path(
        target,
        digest,
        expected_root=directory,
    )
    return {
        "look_descriptor_path": str(target),
        "look_descriptor_hash": digest,
        "look_uuid": _normalize_look_uuid(look_uuid),
    }


def serialize_task_line(
    task: LightroomTask | Mapping[str, Any],
    *,
    batch_id: str,
    task_id: str | None = None,
) -> str:
    """Serialize one photo operation to the bridge's tab-separated protocol."""

    if not isinstance(task, LightroomTask):
        task = LightroomTask(**dict(task))
    batch_id = _validate_identifier(batch_id, "batch_id")
    resolved_task_id = _validate_identifier(
        task_id or task.task_id or uuid.uuid4().hex, "task_id"
    )
    task_type = str(task.task_type).strip().lower()
    if task_type not in {
        "apply",
        "preview",
        "enumerate_presets",
        "repair_preview_metadata",
        "cleanup_transient_snapshot",
    }:
        raise ValueError(
            "task_type must be apply, preview, enumerate_presets, "
            "repair_preview_metadata, or cleanup_transient_snapshot"
        )
    if task_type == "enumerate_presets":
        photo_path: Path | None = None
    else:
        if task.photo_path is None:
            raise ValueError("photo_path is required for photo tasks")
        photo_path = Path(task.photo_path).expanduser()
        if not photo_path.is_absolute():
            raise ValueError("photo_path must be absolute")

    output_mode = (
        str(task.output_mode or ("jpeg" if task_type == "preview" else "xmp"))
        .strip()
        .lower()
    )
    if output_mode not in {"xmp", "jpeg", "both"}:
        raise ValueError("output_mode must be xmp, jpeg, or both")
    if task_type == "preview" and output_mode != "jpeg":
        raise ValueError("preview tasks support only jpeg output")

    preset_uuid = None
    preset_scope = str(task.preset_scope).strip().lower()
    if task.preset_uuid is not None:
        preset_uuid = _validate_identifier(str(task.preset_uuid), "preset_uuid")
        if preset_scope not in {"catalog", "plugin"}:
            raise ValueError("preset_scope must be catalog or plugin")
    if isinstance(task.preset_amount, bool) or not isinstance(task.preset_amount, int):
        raise ValueError("preset_amount must be an integer from 0 to 200")
    if not 0 <= task.preset_amount <= 200:
        raise ValueError("preset_amount must be an integer from 0 to 200")

    look_identity_fields = (task.look_descriptor_hash, task.look_uuid)
    look_requested = task.look_descriptor_path is not None or any(
        value is not None for value in look_identity_fields
    )
    if look_requested and not all(
        value is not None for value in look_identity_fields
    ):
        raise ValueError(
            "look_descriptor_hash and look_uuid must be provided together"
        )
    look_amount = _normalize_look_amount(task.look_amount)
    look_descriptor_hash = None
    look_uuid = None
    if look_requested:
        if preset_uuid is not None:
            raise ValueError(
                "Creative Look descriptors cannot be combined with preset_uuid"
            )
        look_descriptor_hash = _normalize_look_hash(task.look_descriptor_hash)
        if task.look_descriptor_path is not None:
            _normalize_look_descriptor_path(
                task.look_descriptor_path, look_descriptor_hash
            )
        look_uuid = _normalize_look_uuid(task.look_uuid)
    elif look_amount != 100:
        raise ValueError("look_amount requires a Creative Look descriptor")

    jpeg_output_dir = None
    if task.jpeg_output_dir is not None:
        jpeg_output_dir = Path(task.jpeg_output_dir).expanduser()
        if not jpeg_output_dir.is_absolute():
            raise ValueError("jpeg_output_dir must be absolute")
    if task_type == "enumerate_presets" and (
        preset_uuid is not None or look_requested or task.jpeg_output_dir is not None
    ):
        raise ValueError(
            "enumerate_presets does not accept preset, Look, or JPEG output settings"
        )
    if (
        isinstance(task.rating, bool)
        or not isinstance(task.rating, int)
        or not 0 <= task.rating <= 5
    ):
        raise ValueError("rating must be an integer from 0 to 5")
    rating = task.rating
    crop = _normalize_crop(task.crop)
    style = _normalize_style(task.style)
    source_batch_id = None
    source_task_id = None
    if task.source_batch_id is not None:
        source_batch_id = _validate_identifier(
            str(task.source_batch_id), "source_batch_id"
        )
    if task.source_task_id is not None:
        source_task_id = _validate_identifier(
            str(task.source_task_id), "source_task_id"
        )
    if task_type == "cleanup_transient_snapshot":
        if source_batch_id is None or source_task_id is None:
            raise ValueError(
                "cleanup_transient_snapshot requires source_batch_id and source_task_id"
            )
        if not source_batch_id.startswith("export-") or not source_task_id.startswith(
            "photo-"
        ):
            raise ValueError(
                "cleanup_transient_snapshot may target only export-* / photo-* snapshots"
            )
    elif source_batch_id is not None or source_task_id is not None:
        raise ValueError(
            "source_batch_id and source_task_id require cleanup_transient_snapshot"
        )
    if task_type in {"repair_preview_metadata", "cleanup_transient_snapshot"}:
        if output_mode != "xmp":
            raise ValueError(f"{task_type} tasks support only xmp output")
        if rating != 0 or any(
            (
                task.auto_tone,
                task.auto_white_balance,
                task.lens_profile,
                task.remove_chromatic_aberration,
            )
        ):
            raise ValueError(
                f"{task_type} tasks cannot apply ratings or develop settings"
            )
        if (
            crop
            or style
            or preset_uuid is not None
            or preset_scope != "catalog"
            or task.preset_amount != 100
            or look_requested
            or look_amount != 100
            or jpeg_output_dir is not None
        ):
            raise ValueError(
                f"{task_type} tasks cannot apply crop, style, preset, Look, or JPEG settings"
            )

    fields: dict[str, Any] = {
        "batch_id": batch_id,
        "task_id": resolved_task_id,
        "photo_path": str(photo_path) if photo_path is not None else "",
        "rating": rating,
        "auto_tone": bool(task.auto_tone),
        "auto_white_balance": bool(task.auto_white_balance),
        "lens_profile": bool(task.lens_profile),
        "remove_ca": bool(task.remove_chromatic_aberration),
    }
    # Keep the legacy apply-to-XMP task byte shape unchanged.  Optional fields
    # are published only when the caller explicitly requests the new protocol
    # primitives, so Lightroom plug-in upgrades do not invalidate old queues.
    if task_type != "apply":
        fields["task_type"] = task_type
    if output_mode != "xmp" or task.output_mode is not None:
        fields["output_mode"] = output_mode
    if preset_uuid is not None:
        fields["preset_uuid"] = preset_uuid
        fields["preset_scope"] = preset_scope
        fields["preset_amount"] = task.preset_amount
    if look_requested:
        fields["look_descriptor_hash"] = look_descriptor_hash
        fields["look_uuid"] = look_uuid
        fields["look_amount"] = look_amount
    if jpeg_output_dir is not None:
        fields["jpeg_output_dir"] = str(jpeg_output_dir)
    if source_batch_id is not None:
        fields["source_batch_id"] = source_batch_id
        fields["source_task_id"] = source_task_id
    for key, value in sorted(crop.items()):
        fields[f"crop.{key}"] = value
    for key, value in sorted(style.items()):
        fields[f"style.{key}"] = value
    return _line(TASK_PROTOCOL, fields)


def parse_task_line(line: str) -> dict[str, Any]:
    fields = _parse_line(line, TASK_PROTOCOL)
    required = {
        "batch_id",
        "task_id",
        "photo_path",
        "rating",
        "auto_tone",
        "auto_white_balance",
        "lens_profile",
        "remove_ca",
    }
    missing = required.difference(fields)
    if missing:
        raise ValueError(f"task line is missing: {', '.join(sorted(missing))}")
    result = {key: fields.pop(key) for key in required}
    result["task_type"] = fields.pop("task_type", "apply")
    result["output_mode"] = fields.pop(
        "output_mode", "jpeg" if result["task_type"] == "preview" else "xmp"
    )
    result["preset_uuid"] = fields.pop("preset_uuid", None)
    result["preset_scope"] = fields.pop("preset_scope", "catalog")
    result["preset_amount"] = fields.pop("preset_amount", 100)
    legacy_look_descriptor_path = fields.pop("look_descriptor_path", None)
    # PHOTO_AI_LR_TASK/1 originally carried an absolute descriptor path.  New
    # tasks carry only the content identity; accept a well-formed old field for
    # queue compatibility, validate it below, and never expose or reuse it.
    result["look_descriptor_path"] = None
    result["look_descriptor_hash"] = fields.pop("look_descriptor_hash", None)
    result["look_uuid"] = fields.pop("look_uuid", None)
    result["look_amount"] = fields.pop("look_amount", 100)
    result["jpeg_output_dir"] = fields.pop("jpeg_output_dir", None)
    result["source_batch_id"] = fields.pop("source_batch_id", None)
    result["source_task_id"] = fields.pop("source_task_id", None)
    result["crop"] = {}
    result["style"] = {}
    for key, value in fields.items():
        if key.startswith("crop."):
            result["crop"][key[5:]] = value
        elif key.startswith("style."):
            result["style"][key[6:]] = value
        else:
            raise ValueError(f"unknown task field {key!r}")
    _validate_identifier(str(result["batch_id"]), "batch_id")
    _validate_identifier(str(result["task_id"]), "task_id")
    if result["task_type"] not in {
        "apply",
        "preview",
        "enumerate_presets",
        "repair_preview_metadata",
        "cleanup_transient_snapshot",
    }:
        raise ValueError("unknown task_type")
    if result["output_mode"] not in {"xmp", "jpeg", "both"}:
        raise ValueError("unknown output_mode")
    if result["task_type"] == "preview" and result["output_mode"] != "jpeg":
        raise ValueError("preview tasks support only jpeg output")
    if result["task_type"] == "enumerate_presets":
        if result["photo_path"] != "":
            raise ValueError("enumerate_presets photo_path must be empty")
    elif (
        not isinstance(result["photo_path"], str)
        or not Path(result["photo_path"]).is_absolute()
    ):
        raise ValueError("task photo_path must be absolute")
    if result["preset_uuid"] is not None:
        _validate_identifier(str(result["preset_uuid"]), "preset_uuid")
        if result["preset_scope"] not in {"catalog", "plugin"}:
            raise ValueError("preset_scope must be catalog or plugin")
    if (
        isinstance(result["preset_amount"], bool)
        or not isinstance(result["preset_amount"], int)
        or not 0 <= result["preset_amount"] <= 200
    ):
        raise ValueError("preset_amount must be an integer from 0 to 200")
    look_identity_fields = (
        result["look_descriptor_hash"],
        result["look_uuid"],
    )
    look_requested = legacy_look_descriptor_path is not None or any(
        value is not None for value in look_identity_fields
    )
    if look_requested and not all(
        value is not None for value in look_identity_fields
    ):
        raise ValueError(
            "look_descriptor_hash and look_uuid must be provided together"
        )
    result["look_amount"] = _normalize_look_amount(result["look_amount"])
    if look_requested:
        if result["preset_uuid"] is not None:
            raise ValueError(
                "Creative Look descriptors cannot be combined with preset_uuid"
            )
        result["look_descriptor_hash"] = _normalize_look_hash(
            result["look_descriptor_hash"]
        )
        if legacy_look_descriptor_path is not None:
            _normalize_look_descriptor_path(
                legacy_look_descriptor_path, result["look_descriptor_hash"]
            )
        result["look_uuid"] = _normalize_look_uuid(result["look_uuid"])
    elif result["look_amount"] != 100:
        raise ValueError("look_amount requires a Creative Look descriptor")
    if result["task_type"] == "enumerate_presets" and (
        result["preset_uuid"] is not None
        or look_requested
        or result["jpeg_output_dir"] is not None
    ):
        raise ValueError(
            "enumerate_presets does not accept preset, Look, or JPEG output settings"
        )
    if result["jpeg_output_dir"] is not None:
        if (
            not isinstance(result["jpeg_output_dir"], str)
            or not Path(result["jpeg_output_dir"]).is_absolute()
        ):
            raise ValueError("jpeg_output_dir must be absolute")
    if (
        isinstance(result["rating"], bool)
        or not isinstance(result["rating"], int)
        or not 0 <= result["rating"] <= 5
    ):
        raise ValueError("task rating must be an integer from 0 to 5")
    for key in ("auto_tone", "auto_white_balance", "lens_profile", "remove_ca"):
        if not isinstance(result[key], bool):
            raise ValueError(f"task {key} must be boolean")
    crop = _normalize_crop(result["crop"])
    style = _normalize_style(result["style"])
    source_batch_id = result["source_batch_id"]
    source_task_id = result["source_task_id"]
    if source_batch_id is not None:
        source_batch_id = _validate_identifier(str(source_batch_id), "source_batch_id")
    if source_task_id is not None:
        source_task_id = _validate_identifier(str(source_task_id), "source_task_id")
    if result["task_type"] == "cleanup_transient_snapshot":
        if source_batch_id is None or source_task_id is None:
            raise ValueError(
                "cleanup_transient_snapshot requires source_batch_id and source_task_id"
            )
        if not source_batch_id.startswith("export-") or not source_task_id.startswith(
            "photo-"
        ):
            raise ValueError(
                "cleanup_transient_snapshot may target only export-* / photo-* snapshots"
            )
    elif source_batch_id is not None or source_task_id is not None:
        raise ValueError(
            "source_batch_id and source_task_id require cleanup_transient_snapshot"
        )
    if result["task_type"] in {"repair_preview_metadata", "cleanup_transient_snapshot"}:
        task_type = result["task_type"]
        if result["output_mode"] != "xmp":
            raise ValueError(f"{task_type} tasks support only xmp output")
        if result["rating"] != 0 or any(
            result[key]
            for key in ("auto_tone", "auto_white_balance", "lens_profile", "remove_ca")
        ):
            raise ValueError(
                f"{task_type} tasks cannot apply ratings or develop settings"
            )
        if (
            crop
            or style
            or result["preset_uuid"] is not None
            or result["preset_scope"] != "catalog"
            or result["preset_amount"] != 100
            or look_requested
            or result["look_amount"] != 100
            or result["jpeg_output_dir"] is not None
        ):
            raise ValueError(
                f"{task_type} tasks cannot apply crop, style, preset, Look, or JPEG settings"
            )
    return result


def parse_preset_line(line: str) -> dict[str, Any]:
    """Parse one preset descriptor emitted by the Lightroom plug-in."""

    fields = _parse_line(line, PRESET_PROTOCOL)
    required = {"scope", "uuid", "name", "folder", "file"}
    missing = required.difference(fields)
    if missing:
        raise ValueError(f"preset line is missing: {', '.join(sorted(missing))}")
    unknown = set(fields).difference(required)
    if unknown:
        raise ValueError(f"unknown preset field {sorted(unknown)[0]!r}")
    if fields["scope"] not in {"catalog", "plugin"}:
        raise ValueError("preset scope must be catalog or plugin")
    _validate_identifier(str(fields["uuid"]), "preset uuid")
    for key in ("name", "folder", "file"):
        if not isinstance(fields[key], str):
            raise ValueError(f"preset {key} must be text")
    return fields


def read_lightroom_preset_listing(path: Path | str) -> list[dict[str, Any]]:
    """Read a deterministic preset listing artifact returned by Lightroom."""

    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [parse_preset_line(line) for line in lines if line.strip()]


def create_lightroom_batch(
    data_dir: Path | str,
    tasks: Sequence[LightroomTask | Mapping[str, Any]]
    | Iterable[LightroomTask | Mapping[str, Any]],
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Atomically publish a batch of independent photo tasks for Lightroom."""

    paths = bridge_paths(data_dir, create=True)
    batch_id = _validate_identifier(
        batch_id
        or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:10]}",
        "batch_id",
    )
    if (paths.cancelled / f"{batch_id}.cancel").is_file():
        raise RuntimeError(
            f"Lightroom batch was cancelled before publication: {batch_id}"
        )
    task_rows: list[dict[str, str]] = []
    lines: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_task in tasks:
        task = (
            raw_task
            if isinstance(raw_task, LightroomTask)
            else LightroomTask(**dict(raw_task))
        )
        task_id = _validate_identifier(task.task_id or uuid.uuid4().hex, "task_id")
        if task_id in seen:
            raise ValueError(f"duplicate task_id {task_id!r}")
        seen.add(task_id)
        line = serialize_task_line(task, batch_id=batch_id, task_id=task_id)
        parsed = parse_task_line(line)
        if parsed["look_descriptor_hash"] is not None:
            digest = parsed["look_descriptor_hash"]
            descriptor_root = paths.presets / "looks"
            descriptor_path = descriptor_root / f"{digest}.look"
            if task.look_descriptor_path is not None:
                _normalize_look_descriptor_path(
                    task.look_descriptor_path,
                    digest,
                    expected_root=descriptor_root,
                )
            if not descriptor_path.is_file():
                raise ValueError(
                    "Lightroom Look descriptor is missing from the configured bridge root"
                )
            descriptor_bytes = descriptor_path.read_bytes()
            if look_descriptor_sha256(descriptor_bytes) != digest:
                raise ValueError("Lightroom Look descriptor SHA-256 mismatch")
            parse_look_descriptor(
                descriptor_bytes.decode("utf-8"),
                expected_uuid=parsed["look_uuid"],
            )
        file_name = f"{batch_id}--{task_id}.task"
        lines.append((file_name, line))
        task_rows.append(
            {
                "task_id": task_id,
                "photo_path": str(parsed["photo_path"]),
                "file_name": file_name,
            }
        )
    if not task_rows:
        raise ValueError("a Lightroom batch must contain at least one task")

    batch_file = paths.batches / f"{batch_id}.json"
    if batch_file.exists():
        raise FileExistsError(f"batch already exists: {batch_id}")
    manifest = {
        "schema_version": 1,
        "batch_id": batch_id,
        "created_at": _utc_now(),
        "task_count": len(task_rows),
        "tasks": task_rows,
    }
    write_json(batch_file, manifest)
    published: list[Path] = []
    try:
        for file_name, line in lines:
            target = paths.pending / file_name
            atomic_create_text(target, line)
            published.append(target)
    except Exception:
        for target in published:
            target.unlink(missing_ok=True)
        batch_file.unlink(missing_ok=True)
        raise
    return read_lightroom_batch_status(data_dir, batch_id)


def create_lightroom_preset_enumeration(
    data_dir: Path | str,
    *,
    batch_id: str | None = None,
    task_id: str = "presets",
) -> dict[str, Any]:
    """Publish one catalog/plugin preset-enumeration task."""

    return create_lightroom_batch(
        data_dir,
        [LightroomTask(None, task_type="enumerate_presets", task_id=task_id)],
        batch_id=batch_id,
    )


def create_lightroom_preview_metadata_repair(
    data_dir: Path | str,
    photo_paths: Sequence[Path | str] | Iterable[Path | str],
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Publish controlled cleanup tasks for stale style-preview metadata.

    The Lightroom plug-in accepts these tasks only for originals that are
    already present in the active catalog.  It backs up each current sidecar,
    removes only bridge-owned ``style-*`` preview snapshots, and asks
    Lightroom to save its current catalog state.  It never imports a photo or
    applies ratings/develop settings.
    """

    tasks = [
        LightroomTask(
            photo_path=photo_path,
            rating=0,
            auto_tone=False,
            auto_white_balance=False,
            lens_profile=False,
            remove_chromatic_aberration=False,
            task_type="repair_preview_metadata",
            output_mode="xmp",
            task_id=f"repair-{index:06d}",
        )
        for index, photo_path in enumerate(photo_paths, start=1)
    ]
    return create_lightroom_batch(data_dir, tasks, batch_id=batch_id)


def create_lightroom_transient_snapshot_cleanup(
    data_dir: Path | str,
    records: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Publish exact, idempotent cleanup tasks for stale final-export snapshots.

    Each record must bind one catalog photo to the original ``export-*`` batch
    and ``photo-*`` task that created its safety snapshot.  The plug-in neither
    imports photos nor reads/writes sidecars for this task type.
    """

    tasks = []
    for index, record in enumerate(records, start=1):
        tasks.append(
            LightroomTask(
                photo_path=record["photo_path"],
                rating=0,
                auto_tone=False,
                auto_white_balance=False,
                lens_profile=False,
                remove_chromatic_aberration=False,
                task_type="cleanup_transient_snapshot",
                output_mode="xmp",
                source_batch_id=str(record["source_batch_id"]),
                source_task_id=str(record["source_task_id"]),
                task_id=f"cleanup-{index:06d}",
            )
        )
    return create_lightroom_batch(data_dir, tasks, batch_id=batch_id)


def _read_batch_cancellation(
    paths: BridgePaths, batch_id: str
) -> dict[str, Any] | None:
    marker = paths.cancelled / f"{batch_id}.cancel"
    if not marker.is_file():
        return None
    try:
        fields = _parse_line(marker.read_text(encoding="utf-8"), CANCEL_PROTOCOL)
        if str(fields.get("batch_id", "")) != batch_id:
            raise ValueError("cancellation marker batch_id does not match its filename")
        return {**fields, "marker_path": str(marker)}
    except (OSError, ValueError, TypeError) as exc:
        # Presence is the safety signal.  A malformed marker must still stop the
        # plug-in instead of allowing edits to continue.
        return {"batch_id": batch_id, "marker_path": str(marker), "error": str(exc)}


def cancel_lightroom_batch(
    data_dir: Path | str,
    batch_id: str,
    *,
    reason: str = "cancelled by the local Web job manager",
) -> dict[str, Any]:
    """Atomically request cancellation without deleting any queue state.

    The marker may be published before the batch manifest exists.  This closes
    the race between a Web cancellation request and the worker publishing its
    first task: ``create_lightroom_batch`` will refuse a pre-cancelled id, while
    the Lightroom plug-in checks the same durable marker for already-published
    pending/running tasks.
    """

    paths = bridge_paths(data_dir, create=True)
    batch_id = _validate_identifier(batch_id, "batch_id")
    marker = paths.cancelled / f"{batch_id}.cancel"
    line = _line(
        CANCEL_PROTOCOL,
        {
            "batch_id": batch_id,
            "requested_at": _utc_now(),
            "reason": str(reason)[:1000],
        },
    )
    try:
        atomic_create_text(marker, line)
    except FileExistsError:
        pass
    manifest = paths.batches / f"{batch_id}.json"
    if manifest.is_file():
        return read_lightroom_batch_status(data_dir, batch_id)
    return {
        "batch_id": batch_id,
        "status": "cancelled",
        "counts": {
            status: 0
            for status in (
                "pending",
                "running",
                "done",
                "failed",
                "cancelled",
                "missing",
            )
        },
        "completed_count": 0,
        "task_count": 0,
        "tasks": [],
        "cancellation": _read_batch_cancellation(paths, batch_id),
    }


def _result_for(
    paths: BridgePaths, file_name: str
) -> tuple[str, dict[str, Any] | None]:
    stem = Path(file_name).stem
    locations = (
        ("done", paths.done / f"{stem}.result"),
        ("failed", paths.failed / f"{stem}.result"),
        ("cancelled", paths.cancelled / f"{stem}.result"),
        ("running", paths.running / file_name),
        ("pending", paths.pending / file_name),
    )
    for status, path in locations:
        if not path.is_file():
            continue
        if status in {"done", "failed", "cancelled"}:
            try:
                return status, _parse_line(
                    path.read_text(encoding="utf-8"), RESULT_PROTOCOL
                )
            except (OSError, ValueError) as exc:
                return status, {"status": status, "error": f"invalid result: {exc}"}
        return status, None
    return "missing", None


def read_lightroom_batch_status(data_dir: Path | str, batch_id: str) -> dict[str, Any]:
    paths = bridge_paths(data_dir)
    batch_id = _validate_identifier(batch_id, "batch_id")
    manifest_file = paths.batches / f"{batch_id}.json"
    if not manifest_file.is_file():
        raise FileNotFoundError(f"unknown Lightroom batch: {batch_id}")
    manifest = read_json(manifest_file)
    cancellation = _read_batch_cancellation(paths, batch_id)
    counts = {
        status: 0
        for status in ("pending", "running", "done", "failed", "cancelled", "missing")
    }
    rows: list[dict[str, Any]] = []
    for task in manifest.get("tasks", []):
        status, result = _result_for(paths, task["file_name"])
        counts[status] += 1
        rows.append({**task, "status": status, "result": result})
    terminal = counts["done"] + counts["failed"] + counts["cancelled"] == len(rows)
    overall = (
        "incomplete"
        if counts["missing"]
        else "cancelling"
        if cancellation is not None and (counts["pending"] or counts["running"])
        else "cancelled"
        if cancellation is not None
        else "complete"
        if terminal and counts["failed"] == 0
        else "failed"
        if terminal
        else "running"
        if counts["running"]
        else "pending"
    )
    return {
        **manifest,
        "status": overall,
        "counts": counts,
        "completed_count": counts["done"] + counts["failed"] + counts["cancelled"],
        "cancellation": cancellation,
        "tasks": rows,
    }


def _parse_heartbeat(
    path: Path, *, now: datetime, stale_after_seconds: float
) -> dict[str, Any]:
    if not path.is_file():
        return {"state": "offline", "last_seen": None, "age_seconds": None}
    try:
        fields = _parse_line(path.read_text(encoding="utf-8"), HEARTBEAT_PROTOCOL)
        stamp = datetime.fromisoformat(str(fields["at"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = max(0.0, (now - stamp.astimezone(timezone.utc)).total_seconds())
        return {
            **fields,
            "state": "online" if age <= stale_after_seconds else "stale",
            "last_seen": stamp.isoformat(),
            "age_seconds": round(age, 3),
        }
    except (KeyError, OSError, ValueError, TypeError) as exc:
        return {
            "state": "invalid",
            "last_seen": None,
            "age_seconds": None,
            "error": str(exc),
        }


def read_lightroom_bridge_status(
    data_dir: Path | str,
    *,
    batch_id: str | None = None,
    include_batches: bool = True,
    stale_after_seconds: float = 15.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    paths = bridge_paths(data_dir)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    queue_counts = {
        name: len(
            list(
                directory.glob(
                    "*.task" if name in {"pending", "running"} else "*.result"
                )
            )
        )
        if directory.is_dir()
        else 0
        for name, directory in (
            ("pending", paths.pending),
            ("running", paths.running),
            ("done", paths.done),
            ("failed", paths.failed),
            ("cancelled", paths.cancelled),
        )
    }
    status: dict[str, Any] = {
        "root": str(paths.root),
        "configured": paths.config.is_file(),
        "heartbeat": _parse_heartbeat(
            paths.heartbeat, now=now, stale_after_seconds=stale_after_seconds
        ),
        "queue": queue_counts,
    }
    if batch_id is not None:
        status["batch"] = read_lightroom_batch_status(data_dir, batch_id)
    elif include_batches:
        batches: list[dict[str, Any]] = []
        if paths.batches.is_dir():
            for batch_file in sorted(
                paths.batches.glob("*.json"),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            ):
                try:
                    batches.append(
                        read_lightroom_batch_status(data_dir, batch_file.stem)
                    )
                except (OSError, ValueError, KeyError):
                    continue
        status["batches"] = batches
    return status


def _plugin_source_dir() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "integrations"
        / "photo-ai-lightroom.lrplugin"
    )


def _installed_plugin_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return (
        Path(appdata) / "Adobe" / "Lightroom" / "Modules" / "PhotoAI.lrplugin"
    ).resolve()


def installed_lightroom_plugin_dir() -> Path:
    path = _installed_plugin_dir()
    if path is None:
        raise RuntimeError("Windows APPDATA 不可用，无法定位 Lightroom 插件目录。")
    return path


def _plugin_payload_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"bridge-path.txt", PLUGIN_OWNERSHIP_FILE}:
            continue
        if any(part in {"", ".", ".."} for part in Path(relative).parts):
            raise ValueError("Lightroom 插件模板包含无效路径。")
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "Info.lua" not in files:
        raise FileNotFoundError("Lightroom 插件模板缺少 Info.lua。")
    return files


def install_lightroom_plugin(
    template_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Install/update the owned plug-in without overwriting user modifications."""

    source = (
        Path(template_dir).expanduser().resolve()
        if template_dir is not None
        else _plugin_source_dir().resolve()
    )
    destination = installed_lightroom_plugin_dir()
    expected = _plugin_payload_files(source)
    ownership_path = destination / PLUGIN_OWNERSHIP_FILE
    previous: dict[str, Any] = {}
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(f"Lightroom 插件目标不是文件夹：{destination}")
        if not ownership_path.is_file() and any(destination.iterdir()):
            raise RuntimeError(
                "Lightroom 插件目录已存在但不属于照片选片；请先在插件管理器中处理同名插件。"
            )
        if ownership_path.is_file():
            previous = read_json(ownership_path)
            tracked = previous.get("files") if isinstance(previous, dict) else None
            if not isinstance(tracked, dict):
                raise RuntimeError("Lightroom 插件所有权记录已损坏，已拒绝覆盖。")
            actual = _plugin_payload_files(destination)
            if actual != {str(key): str(value) for key, value in tracked.items()}:
                raise RuntimeError("Lightroom 插件已被修改，已保留现状并拒绝自动覆盖。")

    destination.mkdir(parents=True, exist_ok=True)
    previous_files = previous.get("files", {}) if isinstance(previous, dict) else {}
    for relative in sorted(set(previous_files) - set(expected)):
        obsolete = (destination / Path(relative)).resolve()
        if _path_is_within(obsolete, destination) and obsolete.is_file():
            obsolete.unlink()
    for relative in expected:
        source_file = source / Path(relative)
        target = (destination / Path(relative)).resolve()
        if not _path_is_within(target, destination):
            raise ValueError("Lightroom 插件文件发生路径越界。")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(source_file, temporary)
        os.replace(temporary, target)
    write_json(
        ownership_path,
        {
            "schema_version": 1,
            "application": "PhotoAI",
            "plugin_version": PLUGIN_VERSION,
            "installed_at": _utc_now(),
            "files": expected,
        },
    )
    os.environ["PHOTO_AI_LIGHTROOM_PLUGIN_DIR"] = str(destination)
    return {
        "installed": True,
        "plugin_dir": str(destination),
        "file_count": len(expected),
        "plugin_version": PLUGIN_VERSION,
    }


def remove_owned_lightroom_plugin() -> dict[str, Any]:
    """Remove the installed plug-in only while every owned source file matches."""

    destination = installed_lightroom_plugin_dir()
    ownership_path = destination / PLUGIN_OWNERSHIP_FILE
    if not destination.is_dir() or not ownership_path.is_file():
        return {"removed": False, "reason": "not_owned", "plugin_dir": str(destination)}
    try:
        ownership = read_json(ownership_path)
        if not isinstance(ownership, dict):
            raise ValueError("invalid ownership manifest")
        tracked = ownership.get("files")
        if ownership.get("application") != "PhotoAI" or not isinstance(tracked, dict):
            raise ValueError("invalid ownership manifest")
        expected = {str(key): str(value) for key, value in tracked.items()}
        actual = _plugin_payload_files(destination)
    except (OSError, TypeError, ValueError) as exc:
        return {
            "removed": False,
            "reason": "ownership_invalid",
            "error": str(exc),
            "plugin_dir": str(destination),
        }
    if actual != expected:
        return {"removed": False, "reason": "modified", "plugin_dir": str(destination)}
    allowed = set(expected) | {"bridge-path.txt", PLUGIN_OWNERSHIP_FILE}
    extras = [
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.relative_to(destination).as_posix() not in allowed
    ]
    if extras:
        return {
            "removed": False,
            "reason": "extra_files",
            "plugin_dir": str(destination),
        }
    shutil.rmtree(destination)
    return {"removed": True, "plugin_dir": str(destination)}


def _default_plugin_dir() -> Path:
    configured = os.environ.get("PHOTO_AI_LIGHTROOM_PLUGIN_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    installed = _installed_plugin_dir()
    if installed is not None and (installed / "Info.lua").is_file():
        return installed
    return _plugin_source_dir()


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))])
    except ValueError:
        return False
    return os.path.commonpath(
        [os.path.normcase(str(path)), os.path.normcase(str(root))]
    ) == os.path.normcase(str(root))


def _configured_style_root(data_dir: Path) -> Path:
    """Resolve plugin-managed styles without inventing another drive fallback."""

    content_root_value = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    style_root_value = os.environ.get("PHOTO_AI_STYLES_DIR")
    if content_root_value and style_root_value:
        content_root = Path(content_root_value).expanduser().resolve()
        style_root = Path(style_root_value).expanduser().resolve()
        if _path_is_within(data_dir, content_root) and _path_is_within(
            style_root, content_root
        ):
            return style_root / "style-library"
        raise ValueError(
            "Lightroom bridge/style paths are outside PHOTO_AI_CONTENT_ROOT"
        )
    return data_dir / "style-library"


def write_lightroom_plugin_config(
    data_dir: Path | str,
    *,
    plugin_dir: Path | str | None = None,
) -> dict[str, str]:
    """Create the configured bridge layout and point the Lua plug-in at it."""

    data_root = Path(data_dir).expanduser().resolve()
    paths = bridge_paths(data_root, create=True)
    style_root = _configured_style_root(data_root)
    style_root.mkdir(parents=True, exist_ok=True)
    plugin = (
        Path(plugin_dir).expanduser().resolve()
        if plugin_dir is not None
        else _default_plugin_dir()
    )
    if not (plugin / "Info.lua").is_file():
        raise FileNotFoundError(f"Lightroom plug-in not found: {plugin}")
    fields = {"root": str(paths.root), "protocol": 1, "plugin_version": PLUGIN_VERSION}
    atomic_write_text(paths.config, _line(CONFIG_PROTOCOL, fields))
    pointer = plugin / "bridge-path.txt"
    atomic_write_text(pointer, f"{paths.root}\n{style_root}\n")
    return {
        "root": str(paths.root),
        "style_root": str(style_root),
        "config_file": str(paths.config),
        "plugin_pointer": str(pointer),
        "plugin_dir": str(plugin),
    }


def _file_version(path: Path) -> tuple[int, int, int, int] | None:
    if os.name != "nt":
        return None
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.c_uint32),
                ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32),
                ("dwFileVersionLS", ctypes.c_uint32),
                ("dwProductVersionMS", ctypes.c_uint32),
                ("dwProductVersionLS", ctypes.c_uint32),
                ("dwFileFlagsMask", ctypes.c_uint32),
                ("dwFileFlags", ctypes.c_uint32),
                ("dwFileOS", ctypes.c_uint32),
                ("dwFileType", ctypes.c_uint32),
                ("dwFileSubtype", ctypes.c_uint32),
                ("dwFileDateMS", ctypes.c_uint32),
                ("dwFileDateLS", ctypes.c_uint32),
            ]

        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(
            buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return None
        info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        return (
            info.dwProductVersionMS >> 16,
            info.dwProductVersionMS & 0xFFFF,
            info.dwProductVersionLS >> 16,
            info.dwProductVersionLS & 0xFFFF,
        )
    except (AttributeError, OSError, ValueError):
        return None


def _registry_lightroom_paths() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    paths: list[Path] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Lightroom.exe",
                    0,
                    winreg.KEY_READ | view,
                ) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    paths.append(Path(value))
            except OSError:
                continue
    return paths


def _lightroom_candidates(search_roots: Sequence[Path | str] | None) -> list[Path]:
    candidates = _registry_lightroom_paths() if search_roots is None else []
    roots: list[Path] = []
    if search_roots is None:
        explicit = os.environ.get("PHOTO_AI_LIGHTROOM")
        if explicit:
            explicit_path = Path(explicit).expanduser()
            if explicit_path.suffix.casefold() == ".exe":
                candidates.append(explicit_path)
            else:
                roots.append(explicit_path)
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(variable):
                roots.append(Path(os.environ[variable]) / "Adobe")
    else:
        roots.extend(Path(root).expanduser() for root in search_roots)
    for root in roots:
        if root.is_file():
            candidates.append(root)
            continue
        candidates.extend(
            [
                root / "Lightroom.exe",
                root / "Adobe Lightroom Classic" / "Lightroom.exe",
            ]
        )
        try:
            candidates.extend(root.glob("Adobe Lightroom Classic*\\Lightroom.exe"))
        except OSError:
            pass
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key not in seen and candidate.is_file():
            seen.add(key)
            unique.append(candidate.resolve())
    return unique


def _lightroom_support_level(version: tuple[int, ...] | None) -> str:
    if version is None or version[:2] < LIGHTROOM_MINIMUM_VERSION:
        return "incompatible"
    if version[:2] == LIGHTROOM_VALIDATED_VERSION:
        return "validated"
    return "allowed_untested"


def _lightroom_version_from_name(path: Path) -> tuple[int, int] | None:
    matched = re.search(r"(?<!\d)(\d{2})\.(\d+)(?!\d)", path.parent.name)
    if matched is None:
        return None
    return int(matched.group(1)), int(matched.group(2))


def detect_lightroom_classic_15_3(
    search_roots: Sequence[Path | str] | None = None,
) -> Path | None:
    """Return Lightroom Classic 14.3+; retain the historical API name."""

    for candidate in _lightroom_candidates(search_roots):
        version = _file_version(candidate)
        inferred = version or _lightroom_version_from_name(candidate)
        if _lightroom_support_level(inferred) != "incompatible":
            return candidate
    return None


def lightroom_classic_status(
    search_roots: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    candidates = _lightroom_candidates(search_roots)
    installs = []
    for path in candidates:
        version = _file_version(path)
        inferred = version or _lightroom_version_from_name(path)
        support_level = _lightroom_support_level(inferred)
        installs.append(
            {
                "path": str(path),
                "version": ".".join(map(str, version)) if version else None,
                "compatible": support_level != "incompatible",
                "support_level": support_level,
            }
        )
    exact = detect_lightroom_classic_15_3(search_roots)
    selected = next((item for item in installs if item["path"] == str(exact)), None)
    return {
        "required_version": ">=14.3",
        "validated_version": "15.3",
        "compatible": exact is not None,
        "executable": str(exact) if exact else None,
        "support_level": (
            str(selected.get("support_level")) if selected else "incompatible"
        ),
        "compatibility_note": (
            "已在 Lightroom Classic 15.3 完整验证。"
            if selected and selected.get("support_level") == "validated"
            else "版本满足插件最低要求；连接后按实际插件能力使用。"
            if selected
            else "需要 Lightroom Classic 14.3 或更高版本。"
        ),
        "installations": installs,
    }


def get_lightroom_plugin_status(
    data_dir: Path | str,
    *,
    search_roots: Sequence[Path | str] | None = None,
    stale_after_seconds: float = 15.0,
    plugin_dir: Path | str | None = None,
    include_batches: bool = True,
) -> dict[str, Any]:
    """Return one UI-ready status object for the installation and file bridge."""

    bridge = read_lightroom_bridge_status(
        data_dir,
        stale_after_seconds=stale_after_seconds,
        include_batches=include_batches,
    )
    source_dir = (
        Path(plugin_dir).expanduser().resolve()
        if plugin_dir is not None
        else _default_plugin_dir()
    )
    pointer = source_dir / "bridge-path.txt"
    configured_root = None
    configured_style_root = None
    try:
        if pointer.is_file():
            lines = pointer.read_text(encoding="utf-8").splitlines()
            configured_root = lines[0].strip()
            configured_style_root = lines[1].strip() if len(lines) > 1 else None
    except (OSError, IndexError):
        configured_root = None
    bridge["plugin"] = {
        "version": PLUGIN_VERSION,
        "source_dir": str(source_dir),
        "source_available": (source_dir / "Info.lua").is_file(),
        "pointer_file": str(pointer),
        "points_to_this_bridge": configured_root == str(bridge_paths(data_dir).root),
        "style_root": configured_style_root,
    }
    bridge["lightroom"] = lightroom_classic_status(search_roots)
    return bridge


# Short aliases keep the module pleasant for a future Web/CLI integration while
# retaining explicit names in the public implementation above.
create_batch = create_lightroom_batch
read_batch_status = read_lightroom_batch_status
read_status = read_lightroom_bridge_status
write_plugin_config = write_lightroom_plugin_config
plugin_status = get_lightroom_plugin_status
