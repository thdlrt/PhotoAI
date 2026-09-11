from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


# ``landscape_culler.web`` exposes a module-level ASGI app.  Pytest imports
# that module during collection for progress/API helpers, before a per-test
# fixture can redirect storage.  Always bind import-time state and caches to a
# process-scoped E-drive QA directory so collection can never recover or
# cancel a production Lightroom/Web job from ``.runtime/data``.
if "landscape_culler.web" in sys.modules:
    raise RuntimeError(
        "landscape_culler.web was imported before pytest storage isolation"
    )
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PYTEST_IMPORT_ROOT = (
    _PROJECT_ROOT / ".runtime" / "qa" / "pytest-import" / str(os.getpid())
)
if os.name == "nt" and _PYTEST_IMPORT_ROOT.resolve().drive.casefold() != "e:":
    raise RuntimeError("pytest 导入隔离目录必须位于 E 盘。")
_PYTEST_ENV_DIRS = {
    "PHOTO_AI_DATA_DIR": _PYTEST_IMPORT_ROOT / "data",
    "TEMP": _PYTEST_IMPORT_ROOT / "temp",
    "TMP": _PYTEST_IMPORT_ROOT / "temp",
    "TMPDIR": _PYTEST_IMPORT_ROOT / "temp",
    "UV_CACHE_DIR": _PYTEST_IMPORT_ROOT / "cache" / "uv",
    "PIP_CACHE_DIR": _PYTEST_IMPORT_ROOT / "cache" / "pip",
    "XDG_CACHE_HOME": _PYTEST_IMPORT_ROOT / "cache" / "xdg",
    "MPLCONFIGDIR": _PYTEST_IMPORT_ROOT / "cache" / "matplotlib",
    "HF_HOME": _PYTEST_IMPORT_ROOT / "cache" / "huggingface",
    "HF_HUB_CACHE": _PYTEST_IMPORT_ROOT / "cache" / "huggingface" / "hub",
    "TRANSFORMERS_CACHE": (
        _PYTEST_IMPORT_ROOT / "cache" / "huggingface" / "transformers"
    ),
    "TORCH_HOME": _PYTEST_IMPORT_ROOT / "cache" / "torch",
    "OLLAMA_MODELS": _PYTEST_IMPORT_ROOT / "cache" / "ollama-models",
}
for _name, _path in _PYTEST_ENV_DIRS.items():
    _path.mkdir(parents=True, exist_ok=True)
    # Deliberately overwrite an inherited production environment from
    # scripts/env.ps1; setdefault would leave the import-time hazard intact.
    os.environ[_name] = str(_path)
tempfile.tempdir = str(_PYTEST_ENV_DIRS["TEMP"])


# Unit and API tests exercise the complete deterministic crop pipeline without
# downloading multi-gigabyte model weights.  Dedicated smart-crop tests inject
# semantic/VLM results; real-model acceptance runs use PHOTO_AI_SMART_CROP_MODE=full.
os.environ.setdefault("PHOTO_AI_SMART_CROP_MODE", "heuristic")
