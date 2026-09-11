from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any


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
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


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
