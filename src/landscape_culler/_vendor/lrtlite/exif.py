"""EXIF access for the LRT Lite sequence model.

Preference order:

1. an ``exiftool`` binary (LRTimelapse ships one; ``LRT_EXIFTOOL`` overrides)
2. Pillow's EXIF reader for JPEG/TIFF (no external dependency)

Reads are batched (one exiftool process per chunk of files instead of one per
file) and every child process is started without a console window, so scanning a
long sequence neither flickers terminal windows nor dominates the run time.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

_TAGS = ("ISO", "ExposureTime", "FNumber", "DateTimeOriginal", "Model")

Progress = Callable[[int, int], None]
_CACHE: dict[tuple[str, int, int], "ExifInfo"] = {}


def _hidden_kwargs() -> dict:
    """Start a child process without flashing a console window on Windows."""
    if os.name != "nt":  # pragma: no cover - POSIX servers
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


def find_exiftool() -> str | None:
    from ...timelapse import find_media_tool

    return find_media_tool("exiftool")


@dataclass
class ExifInfo:
    path: Path
    iso: float | None = None
    exposure_time: float | None = None  # seconds
    aperture: float | None = None
    timestamp: str | None = None
    camera: str | None = None

    @property
    def ev100(self) -> float | None:
        """Exposure value at ISO 100; higher means a darker scene setting."""
        if not self.exposure_time or not self.aperture:
            return None
        iso = self.iso or 100.0
        return math.log2((self.aperture**2) / self.exposure_time) - math.log2(iso / 100.0)

    @property
    def has_exposure(self) -> bool:
        return self.exposure_time is not None and self.aperture is not None


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _cache_key(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (_key(path), stat.st_mtime_ns, stat.st_size)


def read_exif_batch(
    paths: Iterable[Path],
    *,
    exiftool: str | None = None,
    chunk_size: int = 16,
    workers: int = 4,
    progress: Progress | None = None,
    timeout_per_file: float = 2.0,
) -> dict[Path, ExifInfo]:
    """Read EXIF for many files, in parallel chunks, cached by mtime.

    Network shares answer a header read in ~0.2 s per file, so 250 frames took
    minutes when done serially. Four workers with small chunks keep the progress
    bar moving and cut the wall time by roughly the worker count.
    """
    wanted = [Path(path) for path in paths]
    result: dict[Path, ExifInfo] = {}
    pending: list[Path] = []

    for path in wanted:
        key = _cache_key(path)
        if key and key in _CACHE:
            result[path] = _CACHE[key]
        else:
            pending.append(path)

    tool = exiftool or find_exiftool()
    total = len(pending)
    done = 0

    if tool and pending:
        chunks = [pending[i:i + chunk_size] for i in range(0, total, chunk_size)]
        workers = max(1, min(workers, len(chunks)))
        timeout = max(30.0, timeout_per_file * chunk_size)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_exiftool, tool, chunk, timeout): chunk for chunk in chunks
            }
            for future in as_completed(futures):
                chunk = futures[future]
                try:
                    payload = future.result()
                except Exception:  # pragma: no cover - defensive
                    payload = {}
                # non-ASCII folders come back mojibake even with a UTF-8 reader,
                # so match by base name and finally by input order (exiftool
                # returns one row per input file, in order).
                by_name = {Path(info.path).name.lower(): info for info in payload.values()}
                ordered = list(payload.values())
                for position, path in enumerate(chunk):
                    info = payload.get(_key(path)) or by_name.get(path.name.lower())
                    if info is None and position < len(ordered):
                        info = ordered[position]
                    if info is None:
                        info = _read_with_pillow(path)
                    info.path = path
                    key = _cache_key(path)
                    if key:
                        _CACHE[key] = info
                    result[path] = info
                done += len(chunk)
                if progress:
                    progress(done, total)

    for path in pending:
        if path in result:
            continue
        info = _read_with_pillow(path)
        info.path = path
        key = _cache_key(path)
        if key:
            _CACHE[key] = info
        result[path] = info
        done += 1
        if progress:
            progress(done, total)

    if progress and total == 0:
        progress(0, 0)
    return result


def _run_exiftool(tool: str, paths: list[Path], timeout: float = 60.0) -> dict[str, ExifInfo]:
    command = [
        tool,
        "-fast",
        "-j",
        "-n",
        *[f"-{tag}" for tag in _TAGS],
        *[str(path) for path in paths],
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}

    out: dict[str, ExifInfo] = {}
    for row in rows:
        source = row.get("SourceFile")
        if not source:
            continue
        info = ExifInfo(
            path=Path(source),
            iso=_as_float(row.get("ISO")),
            exposure_time=_as_float(row.get("ExposureTime")),
            aperture=_as_float(row.get("FNumber")),
            timestamp=row.get("DateTimeOriginal"),
            camera=row.get("Model"),
        )
        out[_key(Path(source))] = info
    return out


def read_exif(path: str | Path, exiftool: str | None = None) -> ExifInfo:
    p = Path(path)
    batch = read_exif_batch([p], exiftool=exiftool)
    return batch.get(p) or _read_with_pillow(p)


def clear_cache() -> None:
    """Drop cached EXIF values (tests and long-lived servers)."""
    _CACHE.clear()


def extract_previews(
    paths: Iterable[Path],
    destination: str | Path,
    *,
    exiftool: str | None = None,
    tag: str = "PreviewImage",
) -> dict[Path, Path]:
    """Write each RAW's embedded preview JPEG with **one** exiftool process.

    Pillow cannot decode ARW/CR3/NEF, but every one of them carries a preview
    image. ``exiftool -b -PreviewImage -w`` extracts them all in a single run;
    the caller then measures luminance from the JPEGs.
    """
    tool = exiftool or find_exiftool()
    wanted = [Path(path) for path in paths]
    if not tool or not wanted:
        return {}
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    template = str(dest / "%f.jpg")
    command = [tool, "-b", f"-{tag}", "-w", template, *[str(path) for path in wanted]]
    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=max(60, 5 * len(wanted)),
            check=False,
            **_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    found: dict[Path, Path] = {}
    for path in wanted:
        candidate = dest / f"{path.stem}.jpg"
        if candidate.is_file() and candidate.stat().st_size > 0:
            found[path] = candidate
    return found


def _read_with_pillow(path: Path) -> ExifInfo:
    info = ExifInfo(path=path)
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return info
    try:
        with Image.open(path) as img:
            exif = img.getexif()
    except Exception:
        return info
    info.iso = _as_float(exif.get(34855))
    info.exposure_time = _as_float(exif.get(33434))
    info.aperture = _as_float(exif.get(33437))
    info.timestamp = str(exif.get(36867) or exif.get(306) or "") or None
    info.camera = str(exif.get(272) or "") or None
    return info


def _as_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(text)
    except ValueError:
        return None
