from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from landscape_culler import content_root
from landscape_culler.content_root import (
    CONTENT_ROOT_ENV,
    MARKER_NAME,
    ContentRootNotConfiguredError,
    ContentRootUnavailableError,
    ContentRootValidationError,
    ResourcePathError,
    apply_runtime_environment,
    initialize_content_root,
    resolve_content_root,
    resolve_resource_path,
    sanitize_settings_export,
    validate_content_root,
    write_settings_export,
)
from landscape_culler.util import runs_root


def _initialize(tmp_path: Path, name: str = "content"):
    return initialize_content_root(
        tmp_path / name,
        install_dir=tmp_path / "program",
        apply_environment=False,
        persist_registry=False,
    )


def test_initialize_creates_stable_owned_layout_and_marker(tmp_path: Path) -> None:
    layout = _initialize(tmp_path)
    payload = json.loads(layout.marker.read_text(encoding="utf-8"))

    assert layout.root == (tmp_path / "content").resolve()
    assert payload["application"] == "PhotoAI"
    assert payload["layout_version"] == 1
    assert payload["root_id"]
    assert not any(
        drive in layout.marker.read_text(encoding="utf-8")
        for drive in ("E:\\", "X:\\")
    )
    assert {path.name for path in layout.owned_directories()} == {
        "state",
        "projects",
        "models",
        "runtimes",
        "tools",
        "styles",
        "cache",
        "downloads",
        "temp",
        "logs",
        "backups",
    }
    assert all(path.is_dir() for path in layout.owned_directories())
    assert layout.data_dir == layout.state

    reopened = _initialize(tmp_path)
    reopened_payload = json.loads(reopened.marker.read_text(encoding="utf-8"))
    assert reopened_payload["root_id"] == payload["root_id"]
    assert reopened_payload["created_utc"] == payload["created_utc"]


def test_initialize_rejects_nonempty_unowned_directory(tmp_path: Path) -> None:
    selected = tmp_path / "existing"
    selected.mkdir()
    (selected / "photo.arw").write_bytes(b"raw")

    with pytest.raises(ContentRootValidationError, match="已有其他内容"):
        initialize_content_root(
            selected,
            install_dir=tmp_path / "program",
            apply_environment=False,
            persist_registry=False,
        )
    assert not (selected / MARKER_NAME).exists()
    assert (selected / "photo.arw").read_bytes() == b"raw"


def test_resolve_has_no_default_or_disconnected_drive_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = _initialize(tmp_path, "registered")
    monkeypatch.setattr(content_root, "read_content_root_pointer", lambda: None)

    with pytest.raises(ContentRootNotConfiguredError):
        resolve_content_root(
            environment={}, install_dir=tmp_path / "program"
        )

    missing = tmp_path / "disconnected" / "PhotoAI"
    monkeypatch.setattr(
        content_root, "read_content_root_pointer", lambda: str(fallback.root)
    )
    with pytest.raises(ContentRootUnavailableError):
        resolve_content_root(
            environment={CONTENT_ROOT_ENV: str(missing)},
            install_dir=tmp_path / "program",
        )


def test_environment_pointer_overrides_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = _initialize(tmp_path, "selected")
    registered = _initialize(tmp_path, "registered")
    monkeypatch.setattr(
        content_root, "read_content_root_pointer", lambda: str(registered.root)
    )

    resolved = resolve_content_root(
        environment={CONTENT_ROOT_ENV: str(selected.root)},
        install_dir=tmp_path / "program",
    )
    assert resolved.root == selected.root


def test_resolve_rejects_missing_or_corrupt_ownership_state(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    with pytest.raises(ContentRootUnavailableError, match="所有权标记"):
        resolve_content_root(selected, install_dir=tmp_path / "program")

    layout = _initialize(tmp_path, "corrupt")
    layout.marker.write_text("{}", encoding="utf-8")
    with pytest.raises(ContentRootValidationError, match="不属于"):
        resolve_content_root(layout.root, install_dir=tmp_path / "program")


def test_validate_allows_install_data_child_but_rejects_install_or_parent(
    tmp_path: Path,
) -> None:
    install = tmp_path / "program"
    install.mkdir()
    selected = install / "mutable-data"
    selected.mkdir()

    assert validate_content_root(selected, install_dir=install) == selected.resolve()
    with pytest.raises(ContentRootValidationError, match="安装目录"):
        validate_content_root(install, install_dir=install)
    with pytest.raises(ContentRootValidationError, match="安装目录"):
        validate_content_root(tmp_path, install_dir=install)


def test_managed_worker_can_resolve_owned_root_containing_its_runtime(
    tmp_path: Path,
) -> None:
    layout = _initialize(tmp_path, "managed")
    worker_dir = layout.runtimes / "engines" / "active" / "venv" / "Scripts"
    worker_dir.mkdir(parents=True)

    with pytest.raises(ContentRootValidationError):
        resolve_content_root(layout.root, install_dir=worker_dir)

    resolved = resolve_content_root(
        layout.root,
        install_dir=worker_dir,
        allow_managed_runtime=True,
    )

    assert resolved.root == layout.root


@pytest.mark.skipif(os.name != "nt", reason="Windows volume policy")
def test_validate_windows_volume_policy(tmp_path: Path) -> None:
    separate_install = tmp_path.parent / "program-outside-selected-root"
    with pytest.raises(ContentRootValidationError, match="盘符根目录"):
        validate_content_root(
            Path(tmp_path.anchor), install_dir=separate_install
        )
    with pytest.raises(ContentRootValidationError, match="UNC"):
        validate_content_root(
            r"\\server\share\PhotoAI", install_dir=separate_install
        )
    with pytest.raises(ContentRootValidationError, match="本机磁盘"):
        validate_content_root(
            tmp_path,
            install_dir=separate_install,
            drive_type_getter=lambda _: 4,
            filesystem_type_getter=lambda _: "NTFS",
        )
    with pytest.raises(ContentRootValidationError, match="NTFS/ReFS"):
        validate_content_root(
            tmp_path,
            install_dir=separate_install,
            drive_type_getter=lambda _: 3,
            filesystem_type_getter=lambda _: "exFAT",
        )


def test_resolve_resource_path_blocks_absolute_and_parent_traversal(
    tmp_path: Path,
) -> None:
    layout = _initialize(tmp_path)
    expected = layout.cache / "previews" / "one.jpg"
    assert resolve_resource_path(layout.root, "cache/previews/one.jpg") == expected

    with pytest.raises(ResourcePathError, match="相对路径"):
        resolve_resource_path(layout.root, tmp_path / "outside.jpg")
    with pytest.raises(ResourcePathError, match="路径段"):
        resolve_resource_path(layout.root, "cache/../outside.jpg")
    with pytest.raises(ResourcePathError):
        resolve_resource_path(layout.root, "../outside.jpg")
    with pytest.raises(ResourcePathError, match="不存在"):
        resolve_resource_path(layout.root, "cache/missing.json", must_exist=True)


def test_resolve_resource_path_blocks_symlink_escape(tmp_path: Path) -> None:
    layout = _initialize(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = layout.cache / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks is unavailable")

    with pytest.raises(ResourcePathError, match="越过"):
        resolve_resource_path(layout.root, "cache/escape/result.json")


def test_runtime_environment_forces_all_mutable_caches_below_root(
    tmp_path: Path,
) -> None:
    layout = _initialize(tmp_path)
    environment = {
        "TEMP": str(tmp_path / "old-system-temp"),
        "HF_HOME": str(tmp_path / "old-hf"),
    }
    assigned = apply_runtime_environment(layout, environment=environment)

    assert assigned[CONTENT_ROOT_ENV] == str(layout.root)
    assert assigned["PHOTO_AI_DATA_DIR"] == str(layout.state)
    assert assigned["PHOTO_AI_PROJECTS_DIR"] == str(layout.projects)
    assert assigned["UV_PYTHON_NO_REGISTRY"] == "1"
    assert assigned["PYTHONNOUSERSITE"] == "1"
    for key in (
        "UV_PYTHON_INSTALL_DIR",
        "UV_CACHE_DIR",
        "PIP_CACHE_DIR",
        "XDG_CACHE_HOME",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HF_ASSETS_CACHE",
        "HF_DATASETS_CACHE",
        "TORCH_HOME",
        "OLLAMA_MODELS",
        "TEMP",
        "TMP",
        "TMPDIR",
    ):
        mapped = Path(assigned[key]).resolve()
        assert mapped.is_relative_to(layout.root)
        assert mapped.is_dir()
        assert environment[key] == assigned[key]

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PHOTO_AI_CONTENT_ROOT", str(layout.root))
        patch.setenv("PHOTO_AI_PROJECTS_DIR", str(layout.projects))
        assert runs_root(layout.state) == layout.projects

    with pytest.raises(ContentRootValidationError, match="规范布局"):
        apply_runtime_environment(
            replace(layout, temp=tmp_path / "outside-temp"), environment={}
        )


def test_settings_export_is_whitelisted_and_machine_independent(
    tmp_path: Path,
) -> None:
    source = {
        "ui": {"theme": "dark", "session_token": "secret"},
        "workflow_defaults": {
            "keep_ratio": 0.35,
            "input_root": r"X:\photos",
        },
        "model_profile_preference": "16gb",
        "style_sources": {
            "lightroom": True,
            "disabled_resource_ids": ["preset-deadbeef", r"E:\preset.xmp"],
            "resources": [{"path": r"E:\private\preset.xmp"}],
        },
        "export_defaults": {
            "jpeg_quality": 90,
            "output_path": r"D:\renders",
            "conflict_policy": "suffix",
        },
        "projects": [{"input_root": r"X:\camera"}],
        "lightroom": {"executable_path": r"C:\Program Files\Lightroom.exe"},
        "token": "top-secret",
    }
    snapshot = json.loads(json.dumps(source))

    exported = sanitize_settings_export(source)

    assert source == snapshot
    assert exported == {
        "schema_version": 1,
        "ui": {"theme": "dark"},
        "workflow_defaults": {"keep_ratio": 0.35},
        "model_profile_preference": "16gb",
        "style_sources": {
            "lightroom": True,
            "disabled_resource_ids": ["preset-deadbeef"],
        },
        "export_defaults": {
            "jpeg_quality": 90,
            "conflict_policy": "suffix",
        },
    }
    serialized = json.dumps(exported, ensure_ascii=False)
    assert "X:\\" not in serialized
    assert "D:\\" not in serialized
    assert "secret" not in serialized

    destination = tmp_path / "settings.photoai-settings"
    write_settings_export(destination, source)
    assert json.loads(destination.read_text(encoding="utf-8")) == exported
