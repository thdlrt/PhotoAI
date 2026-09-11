from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from typing import Any

PROGRESS_PREFIX = "@@PHOTO_AI_PROGRESS@@"


def progress_enabled() -> bool:
    """Return whether the CLI is running under the local web job manager."""

    return os.environ.get("PHOTO_AI_PROGRESS", "").casefold() == "json"


def _number(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, number)


def _decimal(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, number) if math.isfinite(number) else None


def heartbeat_timestamp() -> str:
    """Return a stable UTC timestamp for long-running progress heartbeats."""

    return datetime.now(UTC).isoformat()


def emit_progress(
    phase: str,
    label: str,
    current: int,
    total: int,
    *,
    unit: str = "",
    cached: int = 0,
    event: str = "progress",
    detail: str | None = None,
    current_resource: str | None = None,
    downloaded_bytes: int | None = None,
    total_bytes: int | None = None,
    bytes_per_second: float | None = None,
    eta_seconds: float | None = None,
    resumed_bytes: int | None = None,
    elapsed_seconds: float | None = None,
    heartbeat_at: str | None = None,
) -> None:
    """Emit a newline-delimited event that is reliable through Windows pipes.

    Terminal runs keep their normal tqdm output.  Web jobs opt in with
    PHOTO_AI_PROGRESS=json and receive at most roughly 200 intermediate events
    per phase, plus every phase boundary.
    """

    if not progress_enabled():
        return
    current_value = _number(current)
    total_value = _number(total)
    if total_value:
        current_value = min(current_value, total_value)
    has_telemetry = any(
        value is not None
        for value in (
            detail,
            current_resource,
            downloaded_bytes,
            total_bytes,
            bytes_per_second,
            eta_seconds,
            resumed_bytes,
            elapsed_seconds,
            heartbeat_at,
        )
    )
    if event == "progress" and total_value and not has_telemetry:
        step = max(1, math.ceil(total_value / 200))
        if current_value not in {0, 1, total_value} and current_value % step:
            return
    payload = {
        "v": 1,
        "event": event,
        "phase": str(phase),
        "label": str(label),
        "current": current_value,
        "total": total_value,
        "unit": str(unit),
        "cached": min(_number(cached), total_value) if total_value else _number(cached),
    }
    optional_text = {
        "detail": detail,
        "current_resource": current_resource,
        "heartbeat_at": heartbeat_at,
    }
    for key, value in optional_text.items():
        if value is not None and str(value).strip():
            payload[key] = str(value).strip()
    optional_numbers = {
        "downloaded_bytes": downloaded_bytes,
        "total_bytes": total_bytes,
        "resumed_bytes": resumed_bytes,
    }
    for key, value in optional_numbers.items():
        if value is not None:
            payload[key] = _number(value)
    optional_decimals = {
        "bytes_per_second": bytes_per_second,
        "eta_seconds": eta_seconds,
        "elapsed_seconds": elapsed_seconds,
    }
    for key, value in optional_decimals.items():
        normalized = _decimal(value)
        if normalized is not None:
            payload[key] = round(normalized, 3)
    # Keep the pipe protocol ASCII-only. Frozen Windows executables can retain a
    # legacy stderr code page even when their parent requests UTF-8; JSON Unicode
    # escapes round-trip to the original text without depending on that code page.
    print(
        f"{PROGRESS_PREFIX}{json.dumps(payload, ensure_ascii=True, separators=(',', ':'))}",
        flush=True,
    )


def phase_start(
    phase: str,
    label: str,
    total: int,
    *,
    current: int = 0,
    unit: str = "",
    cached: int = 0,
) -> None:
    emit_progress(
        phase,
        label,
        current,
        total,
        unit=unit,
        cached=cached,
        event="phase_start",
    )


def phase_end(
    phase: str,
    label: str,
    total: int,
    *,
    unit: str = "",
    cached: int = 0,
) -> None:
    emit_progress(
        phase,
        label,
        total,
        total,
        unit=unit,
        cached=cached,
        event="phase_end",
    )


def parse_progress_line(line: str) -> dict[str, Any] | None:
    """Parse one structured event; malformed tool output remains ordinary log text."""

    if not line.startswith(PROGRESS_PREFIX):
        return None
    try:
        payload = json.loads(line[len(PROGRESS_PREFIX) :])
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    if payload.get("event") not in {"phase_start", "progress", "phase_end"}:
        return None
    phase = str(payload.get("phase", "")).strip()
    label = str(payload.get("label", "")).strip()
    if not phase or not label:
        return None
    total = _number(payload.get("total"))
    current = _number(payload.get("current"))
    if total:
        current = min(current, total)
    return {
        "v": 1,
        "event": payload["event"],
        "phase": phase,
        "label": label,
        "current": current,
        "total": total,
        "unit": str(payload.get("unit", "")),
        "cached": min(_number(payload.get("cached")), total) if total else _number(payload.get("cached")),
        **{
            key: str(payload[key]).strip()
            for key in ("detail", "current_resource", "heartbeat_at")
            if payload.get(key) is not None and str(payload[key]).strip()
        },
        **{
            key: _number(payload[key])
            for key in ("downloaded_bytes", "total_bytes", "resumed_bytes")
            if payload.get(key) is not None
        },
        **{
            key: decimal
            for key in ("bytes_per_second", "eta_seconds", "elapsed_seconds")
            if (decimal := _decimal(payload.get(key))) is not None
        },
    }
