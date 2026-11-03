from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from .constants import READABLE_RAW_EXTENSIONS

PHOTO_TRASH_DIRECTORY = ".photo-ai-trash"


def is_photo_trash_path(path: Path) -> bool:
    return any(part.casefold() == PHOTO_TRASH_DIRECTORY for part in path.parts)


def raw_photo_paths(input_root: Path) -> list[Path]:
    if is_photo_trash_path(input_root):
        return []
    paths: list[Path] = []

    def scan_error(error: OSError) -> None:
        raise error

    for current, directories, filenames in os.walk(input_root, onerror=scan_error):
        parent = Path(current)
        directories[:] = [name for name in directories
                          if name.casefold() != PHOTO_TRASH_DIRECTORY
                          and not (parent / name).is_symlink()
                          and not (parent / name).is_junction()]
        for name in filenames:
            path = parent / name
            if path.suffix.lower() in READABLE_RAW_EXTENSIONS and path.is_file() and not path.is_symlink():
                paths.append(path)
    return sorted(paths, key=lambda path: str(path).lower())


def runs_root(data_dir: Path) -> Path:
    """Return the portable project store, retaining the legacy layout in dev."""

    configured = os.environ.get("PHOTO_AI_PROJECTS_DIR")
    if not configured:
        return Path(data_dir) / "runs"
    root = Path(configured).expanduser().resolve()
    content_root = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    if content_root:
        try:
            root.relative_to(Path(content_root).expanduser().resolve())
        except ValueError as exc:
            raise RuntimeError("工程目录超出 PHOTO_AI_CONTENT_ROOT。") from exc
    return root


def sequence_number(path: Path) -> int | None:
    matches = re.findall(r"(\d+)", path.stem)
    return int(matches[-1]) if matches else None


def cache_key(path: Path) -> str:
    stat = path.stat()
    payload = f"{str(path).lower()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def quick_fingerprint(path: Path, block_size: int = 1024 * 1024) -> dict[str, Any]:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(block_size))
        if stat.st_size > block_size:
            handle.seek(max(0, stat.st_size - block_size))
            digest.update(handle.read(block_size))
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "quick_sha256": digest.hexdigest()}


def full_fingerprint(path: Path, block_size: int = 4 * 1024 * 1024) -> dict[str, Any]:
    """Hash the complete file when byte-for-byte preservation must be proven.

    ``quick_fingerprint`` deliberately samples only the beginning and end and is
    appropriate for cache invalidation.  Lightroom hand-off is a stricter trust
    boundary: this helper reads every byte so a same-size middle-of-file change
    cannot pass unnoticed.
    """

    if block_size <= 0:
        raise ValueError("block_size must be greater than zero")
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(block_size):
            digest.update(chunk)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        _replace_with_retry(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass  # Cleanup must not hide the original write error.


def _replace_with_retry(source: Path, destination: Path) -> None:
    # Readers and antivirus on Windows can briefly deny delete sharing.
    for attempt in range(10):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 9:
                raise
            time.sleep(min(0.02 * 2**attempt, 0.25))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Publish a complete binary cache; never expose a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_create_text(path: Path, text: str) -> None:
    """Atomically create a text file and fail if the destination already exists."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "nt":
            # Unlike os.replace(), Windows rename refuses to overwrite an
            # XMP that appeared after our existence check.
            os.rename(temp, path)
        else:
            # A same-filesystem hard link is an atomic no-replace publish.
            os.link(temp, path)
            temp.unlink()
    finally:
        if temp.exists():
            temp.unlink()


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
