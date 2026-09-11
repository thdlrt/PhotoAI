from __future__ import annotations

import os
import tempfile
from pathlib import Path

import landscape_culler.web as web_module


def test_module_level_web_app_uses_process_scoped_qa_runtime() -> None:
    project_root = Path(__file__).resolve().parents[1]
    qa_root = (project_root / ".runtime" / "qa" / "pytest-import").resolve()
    data_dir = Path(os.environ["PHOTO_AI_DATA_DIR"]).resolve()
    session_root = data_dir.parent

    assert data_dir == web_module.app.state.data_dir
    assert data_dir == session_root / "data"
    assert session_root.parent == qa_root
    assert session_root.name == str(os.getpid())
    if os.name == "nt":
        assert session_root.drive.casefold() == "e:"
    assert data_dir != (project_root / ".runtime" / "data").resolve()
    assert Path(tempfile.gettempdir()).resolve() == session_root / "temp"

    for name in (
        "TEMP",
        "TMP",
        "TMPDIR",
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "XDG_CACHE_HOME",
        "MPLCONFIGDIR",
        "HF_HOME",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TORCH_HOME",
        "OLLAMA_MODELS",
    ):
        path = Path(os.environ[name]).resolve()
        assert path.is_relative_to(session_root)
        assert path.is_dir()
