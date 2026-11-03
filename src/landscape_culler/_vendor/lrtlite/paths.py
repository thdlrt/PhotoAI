"""Path helpers: is this folder a network share?

RAW previews and per-file header reads behave very differently on a WebDAV/NAS
mount than on a local SSD, and the UI should say so instead of looking frozen.
"""

from __future__ import annotations

import os
from pathlib import Path

_DRIVE_REMOTE = 4


def is_network_path(path: str | Path) -> bool:
    raw = str(path)
    if raw.startswith("\\\\") or raw.startswith("//"):
        return True
    drive = Path(raw).drive
    if drive and os.name == "nt":
        try:
            import ctypes

            return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == _DRIVE_REMOTE
        except Exception:  # pragma: no cover - non-windows / restricted
            return False
    return False


def describe_location(path: str | Path) -> str:
    return "网络路径" if is_network_path(path) else "本地路径"
