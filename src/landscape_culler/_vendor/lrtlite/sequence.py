"""Sequence model: which files belong to the timelapse and what state they are in."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .exif import ExifInfo, read_exif, read_exif_batch
from .xmp import KEYFRAME_RATINGS, XmpSidecar

RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".rw2",
    ".orf", ".rwl", ".dng", ".pef", ".srw", ".raw", ".x3f", ".3fr", ".kdc",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".webp"}
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | IMAGE_EXTENSIONS

_SKIP_DIRS = {"lrtimelapse", "lrt_", "_corrupt", "lrtlite", ".lrt", "__pycache__"}


def natural_key(path: Path) -> list:
    """Sort ``IMG_2.jpg`` before ``IMG_10.jpg``."""
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def sidecar_for(path: Path) -> Path:
    """Case handling: Lightroom writes ``NAME.xmp`` next to ``NAME.CR3``."""
    return path.with_suffix(".xmp")


@dataclass
class Frame:
    path: Path
    index: int
    exif: ExifInfo | None = None
    sidecar: XmpSidecar | None = None
    luminance: float | None = None
    sidecar_missing: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def rating(self) -> int:
        return self.sidecar.rating if self.sidecar else 0

    @property
    def is_keyframe(self) -> bool:
        return self.sidecar is not None and self.sidecar.is_keyframe

    @property
    def exposure(self) -> float:
        return self.sidecar.exposure() if self.sidecar else 0.0

    def rating_label(self) -> str:
        return {0: "-", 2: "HG<", 3: "HG>", 4: "KEY", 5: "CROP"}.get(self.rating, str(self.rating))


@dataclass
class Sequence:
    root: Path
    frames: list[Frame] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.frames)

    def __iter__(self):
        return iter(self.frames)

    @property
    def keyframes(self) -> list[Frame]:
        return [f for f in self.frames if f.is_keyframe]

    def load_sidecars(self) -> None:
        for frame in self.frames:
            sc = sidecar_for(frame.path)
            frame.sidecar_missing = not sc.exists()
            frame.sidecar = XmpSidecar.load_or_new(sc)

    def load_exif(self, only_missing: bool = True, progress=None) -> None:
        """Batch-read EXIF (one exiftool process per chunk, cached by mtime)."""
        targets = [
            frame for frame in self.frames
            if not (only_missing and frame.exif is not None)
        ]
        if not targets:
            if progress:
                progress(len(self.frames), len(self.frames))
            return
        batch = read_exif_batch(
            [frame.path for frame in targets],
            progress=None if progress is None else lambda done, total: progress(done, total),
        )
        for frame in targets:
            info = batch.get(frame.path)
            if info is not None:
                frame.exif = info
        if progress:
            progress(len(targets), len(targets))

    def span(self) -> tuple[float | None, float | None]:
        evs = [f.exif.ev100 for f in self.frames if f.exif and f.exif.ev100 is not None]
        return (min(evs), max(evs)) if evs else (None, None)


def scan(folder: str | Path) -> Sequence:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"not a folder: {root}")
    frames: list[Frame] = []
    for entry in sorted(root.iterdir(), key=natural_key):
        if entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if not is_supported(entry):
            continue
        if any(token in entry.name.lower() for token in _SKIP_DIRS):
            continue
        frames.append(Frame(path=entry, index=len(frames)))
    seq = Sequence(root=root, frames=frames)
    seq.load_sidecars()
    return seq
