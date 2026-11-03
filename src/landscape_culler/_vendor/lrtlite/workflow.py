"""The workflow operations exposed by the CLI and the GUI.

Each operation returns a small result object so the UI can print what happened
without parsing sidecar files again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import analysis
from .sequence import Frame, Sequence, scan, sidecar_for
from .xmp import RATING_KEYFRAME, XmpSidecar

#: scalar properties that must not be linearly interpolated
_NO_INTERPOLATE = {
    "crs:Version",
    "crs:ProcessVersion",
    "crs:HasSettings",
    "crs:HasCrop",
    "crs:CropConstrainToWarp",
    "crs:AlreadyApplied",
    "xmp:Rating",
}


@dataclass
class OpResult:
    action: str
    changed: int = 0
    written: int = 0
    notes: list[str] = field(default_factory=list)
    preview: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        text = f"{self.action}: {self.changed} frame(s) changed, {self.written} sidecar(s) written"
        if self.notes:
            text += "\n  " + "\n  ".join(self.notes)
        return text


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def load(folder: str | Path, *, with_exif: bool = False, progress=None) -> Sequence:
    seq = scan(folder)
    if with_exif:
        seq.load_exif(progress=_scaled(progress, 0.0, 1.0))
    return seq


def _ensure_sidecar(frame: Frame) -> XmpSidecar:
    if frame.sidecar is None:
        frame.sidecar = XmpSidecar.load_or_new(sidecar_for(frame.path))
    return frame.sidecar


def _sub(progress, start: float, end: float):
    """Map a 0..1 fraction into a slice of the progress bar (write loops)."""
    if progress is None:
        return None

    def wrapped(fraction: float) -> None:
        progress(start + (end - start) * max(0.0, min(1.0, fraction)))

    return wrapped


def _write(seq: Sequence, frames: list[Frame], *, dry_run: bool, progress=None) -> int:
    if dry_run:
        return 0
    written = 0
    total = max(1, len(frames))
    for index, frame in enumerate(frames, start=1):
        sidecar = _ensure_sidecar(frame)
        sidecar.save(sidecar_for(frame.path))
        frame.sidecar_missing = False
        written += 1
        if progress:
            progress(index / total)
    return written


def _scaled(progress, start: float, end: float):
    """Map a ``(done, total)`` callback into a slice of the 0..1 progress bar."""
    if progress is None:
        return None

    def wrapped(done: int, total: int) -> None:
        ratio = 1.0 if not total else max(0.0, min(1.0, done / total))
        progress(start + (end - start) * ratio)

    return wrapped


def numeric_settings(sidecar: XmpSidecar) -> dict[str, float]:
    """Scalar CRS/XMP properties of a sidecar, as floats."""
    out: dict[str, float] = {}
    desc = sidecar.description
    for el in list(desc):
        if len(el):  # structs and arrays (ToneCurve, Look, masks) are skipped
            continue
        qname = _qname(el.tag)
        if qname is None or qname in _NO_INTERPOLATE:
            continue
        try:
            out[qname] = float((el.text or "").strip())
        except ValueError:
            continue
    for key, value in desc.attrib.items():
        qname = _qname(key)
        if qname is None or qname in _NO_INTERPOLATE:
            continue
        try:
            out[qname] = float(value)
        except ValueError:
            continue
    return out


def _qname(tag: str) -> str | None:
    from .xmp import NSMAP

    if not tag.startswith("{"):
        return None
    uri, _, local = tag[1:].partition("}")
    for prefix, ns in NSMAP.items():
        if ns == uri:
            return f"{prefix}:{local}"
    return None


def _keyframes_in_order(seq: Sequence) -> list[Frame]:
    return [frame for frame in seq if frame.is_keyframe]


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


def initialize(
    folder: str | Path, *, dry_run: bool = False, force: bool = False, progress=None
) -> OpResult:
    """Write a baseline sidecar next to every frame (Lightroom-readable)."""
    seq = load(folder)
    result = OpResult("initialize")
    touched: list[Frame] = []
    total = max(1, len(seq))
    for position, frame in enumerate(seq, start=1):
        target = sidecar_for(frame.path)
        exists = target.exists()
        if not (exists and not force):
            sidecar = XmpSidecar.new()
            sidecar.rating = 0
            sidecar.set("crs:HasSettings", "true")
            sidecar.set("crs:Version", "14")
            frame.sidecar = sidecar
            touched.append(frame)
        if progress:
            progress(0.7 * position / total)
    result.changed = len(touched)
    result.written = _write(seq, touched, dry_run=dry_run, progress=_sub(progress, 0.7, 1.0))
    result.notes.append(f"{len(seq)} frame(s) scanned, {len(touched)} sidecar(s) created")
    return result


def apply_keyframes(
    folder: str | Path,
    indices: list[int],
    *,
    rating: int = RATING_KEYFRAME,
    clear_others: bool = True,
    dry_run: bool = False,
    progress=None,
) -> OpResult:
    """Mark the given frame indices as keyframes with their star rating."""
    seq = load(folder)
    wanted = sorted({i for i in indices if 0 <= i < len(seq)})
    result = OpResult("keyframes")
    touched: list[Frame] = []
    total = max(1, len(seq))
    for position, frame in enumerate(seq, start=1):
        sidecar = _ensure_sidecar(frame)
        new_rating = rating if frame.index in wanted else (0 if clear_others else sidecar.rating)
        if sidecar.rating != new_rating:
            sidecar.rating = new_rating
            touched.append(frame)
        if progress:
            progress(0.7 * position / total)
    result.changed = len(touched)
    result.written = _write(seq, touched, dry_run=dry_run, progress=_sub(progress, 0.7, 1.0))
    result.notes.append(f"{len(wanted)} keyframe(s) marked with {rating} star(s): {wanted[:12]}{'...' if len(wanted) > 12 else ''}")
    result.preview = [(f.name, f.rating_label()) for f in seq.keyframes]
    return result


def auto_keyframes(
    folder: str | Path, *, sensitivity: float = 1.0, dry_run: bool = False, progress=None
) -> OpResult:
    seq = load(folder)
    seq.load_exif(progress=_scaled(progress, 0.0, 0.35))
    analysis.sample_luminance(seq.frames, progress=_scaled(progress, 0.35, 0.7))
    picks = analysis.auto_keyframes(seq.frames, sensitivity=sensitivity)
    return apply_keyframes(folder, picks, dry_run=dry_run, progress=_sub(progress, 0.7, 1.0))


def evenly_spaced(
    folder: str | Path, count: int, *, dry_run: bool = False, progress=None
) -> OpResult:
    seq = load(folder)
    picks = analysis.evenly_spaced_keyframes(len(seq), count)
    return apply_keyframes(folder, picks, dry_run=dry_run, progress=progress)


def transition(
    folder: str | Path,
    *,
    fields: list[str] | None = None,
    dry_run: bool = False,
    progress=None,
) -> OpResult:
    """Interpolate Lightroom settings between consecutive keyframes."""
    seq = load(folder)
    keyframes = _keyframes_in_order(seq)
    result = OpResult("transition")
    if len(keyframes) < 2:
        result.notes.append("need at least two keyframes (2/3/4/5 stars)")
        return result

    allow = set(fields) if fields else None
    touched: list[Frame] = []
    interpolated_fields: set[str] = set()
    estimated = max(1, len(seq) - len(keyframes))

    for left, right in zip(keyframes, keyframes[1:]):
        if right.index - left.index <= 1:
            continue
        left_vals = numeric_settings(_ensure_sidecar(left))
        right_vals = numeric_settings(_ensure_sidecar(right))
        shared = set(left_vals) & set(right_vals)
        if allow is not None:
            shared &= allow
        span = right.index - left.index
        for frame in seq.frames[left.index + 1:right.index]:
            sidecar = _ensure_sidecar(frame)
            ratio = (frame.index - left.index) / span
            for name in sorted(shared):
                value = left_vals[name] + (right_vals[name] - left_vals[name]) * ratio
                sidecar.set(name, f"{value:.4f}".rstrip("0").rstrip("."))
                interpolated_fields.add(name)
            touched.append(frame)
            frame.sidecar = sidecar
            if progress:
                progress(0.6 * min(1.0, len(touched) / estimated))

    result.changed = len(touched)
    result.written = _write(seq, touched, dry_run=dry_run, progress=_sub(progress, 0.6, 1.0))
    result.notes.append(f"{len(keyframes)} keyframe(s), {len(touched)} intermediate frame(s)")
    result.notes.append(f"fields: {', '.join(sorted(interpolated_fields)) or 'none shared'}")
    return result


def holy_grail(
    folder: str | Path,
    *,
    strength: float = 1.0,
    smooth_window: int = 21,
    between_hg_keyframes: bool = True,
    dry_run: bool = False,
    progress=None,
) -> OpResult:
    """Equalise the brightness staircase of a day-to-night shoot."""
    seq = load(folder)
    seq.load_exif(progress=_scaled(progress, 0.0, 0.5))
    result = OpResult("holy grail")

    offsets = analysis.holy_grail_offsets(seq.frames, strength=strength, smooth_window=smooth_window)
    if not any(abs(o) > 1e-4 for o in offsets):
        evs = [f.exif.ev100 for f in seq if f.exif and f.exif.ev100 is not None]
        if not evs:
            result.notes.append("EXIF 里没有快门/光圈，无法计算圣杯曲线（RAW 需要 exiftool）")
        elif max(evs) - min(evs) < 0.05:
            result.notes.append(
                f"整段曝光本来就一致（EV {evs[0]:.2f}，跨度 {max(evs) - min(evs):.2f}），无需圣杯校正"
            )
        else:
            result.notes.append("曝光曲线已经很平滑，没有台阶需要压平")
        return result

    lo, hi = 0, len(seq) - 1
    if between_hg_keyframes:
        left = [f.index for f in seq if f.rating == 2]
        right = [f.index for f in seq if f.rating == 3]
        if left and right:
            lo, hi = min(left), max(right)
        elif left:
            lo = min(left)
        elif right:
            hi = max(right)

    touched: list[Frame] = []
    applied = 0.0
    for frame in seq.frames:
        if not (lo <= frame.index <= hi):
            continue
        offset = offsets[frame.index]
        if abs(offset) < 1e-4:
            continue
        sidecar = _ensure_sidecar(frame)
        sidecar.set_exposure(sidecar.exposure() + offset)
        touched.append(frame)
        applied = max(applied, abs(offset))

    result.changed = len(touched)
    result.written = _write(seq, touched, dry_run=dry_run, progress=_sub(progress, 0.5, 1.0))
    result.notes.append(f"window {lo}..{hi}, max correction {applied:.2f} EV")
    result.notes.append("write the values, then read metadata in Lightroom to see the flattened ramp")
    return result


def deflicker(
    folder: str | Path,
    *,
    smooth_window: int = 11,
    strength: float = 1.0,
    dry_run: bool = False,
    progress=None,
) -> OpResult:
    """Reduce frame-to-frame luminance flicker with exposure offsets."""
    seq = load(folder)
    lums = analysis.sample_luminance(seq.frames, force=True, progress=_scaled(progress, 0.0, 0.6))
    offsets = analysis.deflicker_offsets(lums, smooth_window=smooth_window, strength=strength)
    result = OpResult("deflicker")
    if not any(abs(o) > 1e-4 for o in offsets):
        result.notes.append("没有可用的亮度信号（RAW 预览也取不到），回退到 EXIF 曝光")
        seq.load_exif(progress=_scaled(progress, 0.6, 0.7))
        offsets = analysis.holy_grail_offsets(seq.frames, strength=strength, smooth_window=smooth_window)
        if not any(abs(o) > 1e-4 for o in offsets):
            evs = [f.exif.ev100 for f in seq if f.exif and f.exif.ev100 is not None]
            if evs and max(evs) - min(evs) < 0.05:
                result.notes.append(
                    f"整段曝光一致（EV {evs[0]:.2f}），没有闪烁需要去除"
                )
            return result

    touched: list[Frame] = []
    for frame, offset in zip(seq.frames, offsets):
        if abs(offset) < 1e-4:
            continue
        sidecar = _ensure_sidecar(frame)
        sidecar.set_exposure(sidecar.exposure() + offset)
        touched.append(frame)

    result.changed = len(touched)
    result.written = _write(seq, touched, dry_run=dry_run, progress=_sub(progress, 0.7, 1.0))
    result.notes.append(f"smoothed over {smooth_window} frames, {len(touched)} frame(s) corrected")
    return result


def clear_ratings(folder: str | Path, *, dry_run: bool = False, progress=None) -> OpResult:
    """Drop keyframe stars (LRT's "remove keyframes")."""
    seq = load(folder)
    touched: list[Frame] = []
    for frame in seq:
        if frame.sidecar and frame.sidecar.rating:
            frame.sidecar.rating = 0
            touched.append(frame)
    result = OpResult("clear keyframes")
    result.changed = len(touched)
    result.written = _write(seq, touched, dry_run=dry_run, progress=_sub(progress, 0.5, 1.0))
    return result


def verify(folder: str | Path) -> tuple[dict[str, float | None], str]:
    """Measure how flat the exposure ramp is, using EXIF EV plus sidecar exposure.

    ``before`` is the camera exposure alone (sidecars ignored), ``after`` adds the
    written ``crs:Exposure2012`` offsets. A lower standard deviation and a smaller
    frame-to-frame jump means a smoother day-to-night ramp.
    """
    seq = load(folder)
    seq.load_exif()
    raw = [f.exif.ev100 if f.exif else None for f in seq]
    corrected = analysis.corrected_ev_series(seq)
    before_jumps = analysis.jumps(raw)
    after_jumps = analysis.jumps(corrected)

    metrics: dict[str, float | None] = {
        "before_spread": analysis.spread(raw),
        "after_spread": analysis.spread(corrected),
        "before_max_jump": max(before_jumps) if before_jumps else None,
        "after_max_jump": max(after_jumps) if after_jumps else None,
        "before_p90_jump": analysis.percentile(before_jumps, 0.9),
        "after_p90_jump": analysis.percentile(after_jumps, 0.9),
        "max_offset": max((abs(f.exposure) for f in seq), default=0.0),
    }
    lines = [
        f"frames            : {len(seq)}",
        f"keyframes         : {len(seq.keyframes)}",
        f"EV spread before  : {_fmt(metrics['before_spread'])}",
        f"EV spread after   : {_fmt(metrics['after_spread'])}",
        f"max jump before   : {_fmt(metrics['before_max_jump'])} EV",
        f"max jump after    : {_fmt(metrics['after_max_jump'])} EV",
        f"p90 jump before   : {_fmt(metrics['before_p90_jump'])} EV",
        f"p90 jump after    : {_fmt(metrics['after_p90_jump'])} EV",
        f"max |exposure|    : {metrics['max_offset']:.2f} EV",
    ]
    return metrics, "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"
