import shutil
import subprocess
from pathlib import Path

import pytest


def test_diagnostics_frontend_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_diagnostics_contract.mjs")),
        str(root / "src/landscape_culler/static/app.js")], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_project_ownership_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_project_ownership_contract.mjs")),
                             str(root / "src/landscape_culler/static/app.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_photo_viewer_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_photo_viewer_contract.mjs")),
                             str(root / "src/landscape_culler/static/photo-viewer.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_review_controls_frontend_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_review_controls_contract.mjs")),
                             str(root / "src/landscape_culler/static/app.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_rating_queue_frontend_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_rating_queue_contract.mjs")),
                             str(root / "src/landscape_culler/static/app.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_job_controls_frontend_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_job_controls_contract.mjs")),
                             str(root / "src/landscape_culler/static/app.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_vision_settings_frontend_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_vision_settings_contract.mjs")),
                             str(root / "src/landscape_culler/static/app.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_skip_crop_frontend_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_skip_crop_contract.mjs")),
                             str(root / "src/landscape_culler/static/app.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_develop_recovery_frontend_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required")
    root = Path(__file__).parents[1]
    result = subprocess.run([node, str(Path(__file__).with_name("frontend_develop_recovery_contract.mjs")),
                             str(root / "src/landscape_culler/static/app.js")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
