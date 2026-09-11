from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_crop_next_frontend_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the frontend behavior contract")

    result = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("frontend_crop_next_contract.mjs")),
            str(ROOT / "src" / "landscape_culler" / "static" / "app.js"),
            str(ROOT / "src" / "landscape_culler" / "templates" / "index.html"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "crop-next frontend contract passed" in result.stdout


def test_workflow_simplification_frontend_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the frontend behavior contract")

    result = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("frontend_workflow_simplification_contract.mjs")),
            str(ROOT / "src" / "landscape_culler" / "static" / "app.js"),
            str(ROOT / "src" / "landscape_culler" / "templates" / "index.html"),
            str(ROOT / "src" / "landscape_culler" / "static" / "app.css"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "workflow simplification frontend contract passed" in result.stdout


def test_desktop_folder_picker_frontend_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the frontend behavior contract")

    result = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("frontend_desktop_folder_picker_contract.mjs")),
            str(ROOT / "src" / "landscape_culler" / "static" / "app.js"),
            str(ROOT / "src" / "landscape_culler" / "templates" / "index.html"),
            str(ROOT / "src" / "landscape_culler" / "static" / "app.css"),
            str(ROOT / "desktop" / "src-tauri" / "capabilities" / "default.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "frontend desktop folder picker contract passed" in result.stdout
