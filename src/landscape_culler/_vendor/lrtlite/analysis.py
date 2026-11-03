"""Numeric core: luminance sampling, smoothing, Holy Grail ramps and deflicker.

Everything here is pure Python and deterministic so the generated sidecars can be
diffed and unit-tested.
"""

from __future__ import annotations

import math
from pathlib import Path

from .sequence import Frame

# ---------------------------------------------------------------------------
# luminance sampling
# ---------------------------------------------------------------------------


def read_luminance(path: str | Path, size: int = 96) -> float | None:
    """Mean linear-ish luminance of a preview, normalised to 0..1.

    RAW files are not decodable with Pillow; callers fall back to EXIF-derived
    brightness in that case.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return None
    try:
        with Image.open(path) as img:
            img = img.convert("L")
            img.thumbnail((size, size))
            pixels = list(img.getdata())
    except Exception:
        return None
    if not pixels:
        return None
    return sum(pixels) / (len(pixels) * 255.0)


def sample_luminance(
    frames: list[Frame],
    force: bool = False,
    progress=None,
    *,
    should_stop=None,
    use_previews: bool | str = "auto",
) -> list[float | None]:
    """Measure preview luminance; falls back to RAW embedded previews.

    ``use_previews="auto"`` disables the preview pass for network folders: on a
    WebDAV/NAS share extracting 250 embedded previews would download gigabytes.
    """
    todo = [frame for frame in frames if force or frame.luminance is None]
    total = len(todo)
    missing: list[Frame] = []
    for index, frame in enumerate(todo, start=1):
        if should_stop is not None and should_stop():
            break
        frame.luminance = read_luminance(frame.path)
        if frame.luminance is None:
            missing.append(frame)
        if progress:
            progress(index, total)

    previews_allowed = use_previews
    if previews_allowed == "auto":
        from .paths import is_network_path

        previews_allowed = bool(todo) and not is_network_path(todo[0].path)

    if previews_allowed and missing:
        _luminance_from_previews(missing, progress)

    if not todo and progress:
        progress(len(frames), len(frames))
    return [frame.luminance for frame in frames]


def _luminance_from_previews(frames: list[Frame], progress=None) -> None:
    """Extract RAW preview JPEGs in one exiftool run and measure them."""
    import tempfile
    from pathlib import Path

    from .exif import extract_previews

    with tempfile.TemporaryDirectory(prefix="lrtlite-preview-") as tmp:
        previews = extract_previews([frame.path for frame in frames], Path(tmp))
        missing = [frame for frame in frames if frame.path not in previews]
        if missing:
            previews.update(
                extract_previews(
                    [frame.path for frame in missing], Path(tmp), tag="ThumbnailImage"
                )
            )
        for index, frame in enumerate(frames, start=1):
            preview = previews.get(frame.path)
            if preview is not None:
                frame.luminance = read_luminance(preview)
            if progress:
                progress(index, len(frames))


# ---------------------------------------------------------------------------
# smoothing
# ---------------------------------------------------------------------------


def moving_average(values: list[float], window: int) -> list[float]:
    """Centred moving average with shrinking windows at the edges."""
    if window <= 1 or not values:
        return list(values)
    half = window // 2
    out: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        chunk = [v for v in values[lo:hi] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else values[i])
    return out


def median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


# ---------------------------------------------------------------------------
# Holy Grail: equalise brightness steps in an exposure ramp
# ---------------------------------------------------------------------------


def ev_series(frames: list[Frame]) -> list[float | None]:
    return [f.exif.ev100 if f.exif else None for f in frames]


def holy_grail_offsets(
    frames: list[Frame],
    *,
    strength: float = 1.0,
    smooth_window: int = 21,
) -> list[float]:
    """Per-frame exposure offsets (EV) that flatten the brightness staircase.

    The ramp is derived from the recorded camera exposure (EXIF EV100), which is
    exactly the quantity a Holy Grail shoot changes step by step. A long moving
    average is the *intended* slow brightness trend; the difference between the
    observed curve and that trend is the visible staircase, and the correction is
    its negation (scaled by ``strength``).

    A short taper keeps the correction itself smooth so Lightroom does not show a
    new jump at the window edges.
    """
    evs = ev_series(frames)
    known = [(i, v) for i, v in enumerate(evs) if v is not None]
    if len(known) < 3:
        return [0.0] * len(frames)

    filled = _interpolate_gaps([v for v in evs])
    window = max(5, min(smooth_window, max(5, len(filled) // 2)))
    trend = moving_average(filled, window)
    # corrected EV == trend by construction, so the staircase becomes a ramp
    correction = [base - observed for observed, base in zip(filled, trend)]
    return _clamp_symmetric([c * strength for c in correction], 5.0)


def _interpolate_gaps(values: list[float | None]) -> list[float]:
    known = [(i, v) for i, v in enumerate(values) if v is not None]
    if not known:
        return [0.0] * len(values)
    out: list[float] = []
    first_idx, first_val = known[0]
    last_idx, last_val = known[-1]
    for i in range(len(values)):
        if values[i] is not None:
            out.append(float(values[i]))  # type: ignore[arg-type]
            continue
        if i < first_idx:
            out.append(float(first_val))
        elif i > last_idx:
            out.append(float(last_val))
        else:
            prev_i, prev_v = max((k for k in known if k[0] < i), key=lambda kv: kv[0])
            next_i, next_v = min((k for k in known if k[0] > i), key=lambda kv: kv[0])
            ratio = (i - prev_i) / (next_i - prev_i)
            out.append(float(prev_v) + (float(next_v) - float(prev_v)) * ratio)
    return out


def _clamp_symmetric(values: list[float], limit: float) -> list[float]:
    return [max(-limit, min(limit, v)) for v in values]


# ---------------------------------------------------------------------------
# Deflicker: flatten frame-to-frame luminance flicker
# ---------------------------------------------------------------------------


def deflicker_offsets(
    lums: list[float | None],
    *,
    smooth_window: int = 11,
    strength: float = 1.0,
) -> list[float]:
    """Exposure offsets (EV) that pull each frame towards a smoothed reference."""
    known = [v for v in lums if v is not None]
    if len(known) < 3:
        return [0.0] * len(lums)
    filled = _interpolate_gaps(lums)
    # A window that spans a large share of the sequence would flatten the
    # intended day-to-night trend instead of the flicker, so clamp it.
    window = max(3, min(smooth_window, max(3, len(filled) // 6)))
    reference = moving_average(filled, window)
    offsets = []
    for observed, target in zip(filled, reference):
        if observed <= 1e-4 or target <= 1e-4:
            offsets.append(0.0)
            continue
        offsets.append(math.log2(target / observed) * strength)
    return _clamp_symmetric(offsets, 2.0)


# ---------------------------------------------------------------------------
# Keyframe heuristics
# ---------------------------------------------------------------------------


def auto_keyframes(
    frames: list[Frame],
    *,
    min_gap: int = 2,
    sensitivity: float = 1.0,
) -> list[int]:
    """Pick indices where brightness **or** camera exposure changes noticeably.

    Both signals are used because a Holy Grail shoot changes the camera exposure
    (visible in EXIF EV100) while a normal day-to-night shot changes the scene
    brightness (visible in the rendered luminance). Each signal is normalised by
    its own median change, so the threshold means "N times a typical step".
    """
    n = len(frames)
    if n < 2:
        return [0] if frames else []

    lums = [f.luminance for f in frames]
    evs = [f.exif.ev100 if f.exif else None for f in frames]

    signals: list[list[float]] = []
    for series in (lums, evs):
        if sum(1 for v in series if v is not None) < max(3, n // 4):
            continue
        filled = _interpolate_gaps(series)
        # No pre-smoothing: a Holy Grail step is one hard edge and smoothing it
        # would spread the change below the detection threshold.
        deltas = [0.0] + [abs(b - a) for a, b in zip(filled, filled[1:])]
        scale = sum(deltas) / len(deltas) if deltas else 0.0
        if scale <= 1e-9:
            continue
        signals.append([d / scale for d in deltas])

    if not signals:
        step = max(1, n // 8)
        return sorted({0, n - 1, *range(0, n, step)})

    combined = [max(values) for values in zip(*signals)]
    threshold = 2.5 / max(sensitivity, 1e-6)

    picks = {0, n - 1}
    last_pick = -min_gap
    for i, score in enumerate(combined):
        if score >= threshold and (i - last_pick) >= min_gap:
            picks.add(i)
            last_pick = i
    return sorted(picks)


def corrected_ev_series(frames: list[Frame]) -> list[float | None]:
    """EV100 plus the sidecar exposure offset: the brightness the ramp produces."""
    out: list[float | None] = []
    for frame in frames:
        ev = frame.exif.ev100 if frame.exif else None
        out.append(None if ev is None else ev + frame.exposure)
    return out


def spread(values: list[float | None]) -> float | None:
    """Standard deviation of the known values (``None`` when unusable)."""
    known = [v for v in values if v is not None]
    if len(known) < 2:
        return None
    mean = sum(known) / len(known)
    return math.sqrt(sum((v - mean) ** 2 for v in known) / len(known))


def jumps(values: list[float | None]) -> list[float]:
    """Absolute frame-to-frame changes, ignoring unknown values."""
    return [abs(b - a) for a, b in zip(values, values[1:]) if a is not None and b is not None]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def evenly_spaced_keyframes(count: int, keyframes: int) -> list[int]:
    if count <= 0:
        return []
    keyframes = max(2, min(keyframes, count))
    step = (count - 1) / (keyframes - 1)
    return sorted({int(round(i * step)) for i in range(keyframes)})
