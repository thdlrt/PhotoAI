from __future__ import annotations

import json
from pathlib import Path

import pytest

from landscape_culler.settings_transfer import (
    MAX_SETTINGS_TRANSFER_BYTES,
    SettingsTransferError,
    build_settings_export,
    normalize_settings_payload,
    preferences_path,
    read_local_preferences,
    write_local_preferences,
)


def _current() -> dict:
    return {
        "schema_version": 1,
        "ui": {"theme": "dark"},
        "workflow_defaults": {"retain_ratio": 0.30, "mode": "deep"},
        "model_profile_preference": "16gb",
        "style_sources": {
            "include_lightroom_presets": True,
            "include_user_uploads": True,
            "hidden_resource_ids": ["xmp-aaaaaaaa"],
        },
        "export_defaults": {
            "xmp": True,
            "jpeg": False,
            "jpeg_settings": {
                "color_space": "sRGB",
                "size": "original",
                "quality": 90,
                "sharpening": "screen_standard",
                "collision": "suffix",
            },
        },
    }


def test_normalize_settings_import_keeps_only_supported_preferences() -> None:
    incoming = {
        "schema_version": 1,
        "workflow_defaults": {
            "retain_ratio": 0.45,
            "mode": "fast",
            "input_root": r"X:\camera",
        },
        "model_profile_preference": "8gb",
        "style_sources": {
            "include_lightroom_presets": False,
            "include_user_uploads": True,
            "hidden_resource_ids": ["lut-deadbeef", "xmp-feedface"],
            "resources": [{"path": r"E:\styles\one.xmp"}],
        },
        "export_defaults": {
            "xmp": False,
            "jpeg": True,
            "output_path": r"D:\renders",
        },
        "projects": [{"input_root": r"X:\photos"}],
        "models": [{"path": r"E:\models\weights.bin"}],
        "lightroom": {"executable_path": r"C:\Lightroom.exe"},
        "gpu_info": {"name": "RTX"},
        "token": "secret",
    }

    settings, ignored = normalize_settings_payload(incoming, current=_current())

    assert settings["workflow_defaults"] == {
        "retain_ratio": 0.45,
        "mode": "fast",
    }
    assert settings["model_profile_preference"] == "8gb"
    assert settings["style_sources"] == {
        "include_lightroom_presets": False,
        "include_user_uploads": True,
        "hidden_resource_ids": ["lut-deadbeef", "xmp-feedface"],
    }
    assert settings["export_defaults"]["xmp"] is False
    assert settings["export_defaults"]["jpeg"] is True
    assert set(ignored) == {
        "gpu_info",
        "lightroom",
        "models",
        "projects",
        "token",
    }
    serialized = json.dumps(settings, ensure_ascii=False)
    assert not any(value in serialized for value in ("X:\\", "E:\\", "D:\\"))
    assert "secret" not in serialized


def test_partial_import_preserves_other_preferences_and_can_clear_profile() -> None:
    settings, _ = normalize_settings_payload(
        {"schema_version": 1, "model_profile_preference": None},
        current=_current(),
    )
    assert settings["model_profile_preference"] is None
    assert settings["workflow_defaults"] == _current()["workflow_defaults"]
    assert settings["style_sources"] == _current()["style_sources"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"workflow_defaults": {"retain_ratio": 2, "mode": "deep"}}, "保留比例"),
        ({"model_profile_preference": "32gb"}, "8gb 或 16gb"),
        (
            {"style_sources": {"hidden_resource_ids": ["not-a-resource-id"]}},
            "风格资源标识",
        ),
        (
            {
                "export_defaults": {
                    "xmp": True,
                    "jpeg": True,
                    "jpeg_settings": {"quality": 85},
                }
            },
            "固定为 90",
        ),
    ],
)
def test_settings_import_rejects_unsupported_values(
    changes: dict, message: str
) -> None:
    with pytest.raises(SettingsTransferError, match=message):
        normalize_settings_payload({"schema_version": 1, **changes})


def test_settings_import_rejects_wrong_schema_empty_and_oversized() -> None:
    with pytest.raises(SettingsTransferError, match="版本"):
        normalize_settings_payload({"schema_version": 2, "ui": {}})
    with pytest.raises(SettingsTransferError, match="没有可导入"):
        normalize_settings_payload({"schema_version": 1, "projects": []})
    with pytest.raises(SettingsTransferError, match="256 KB"):
        normalize_settings_payload(
            {"schema_version": 1, "ui": {"padding": "x" * MAX_SETTINGS_TRANSFER_BYTES}}
        )


def test_local_preferences_and_export_are_path_free(tmp_path: Path) -> None:
    current = _current()
    write_local_preferences(tmp_path, current)

    stored_path = preferences_path(tmp_path)
    stored = json.loads(stored_path.read_text(encoding="utf-8"))
    assert "style_sources" not in stored
    assert read_local_preferences(tmp_path)["model_profile_preference"] == "16gb"

    exported = build_settings_export(
        tmp_path,
        style_settings={
            "include_lightroom_presets": False,
            "include_user_uploads": True,
            "hidden_resource_ids": ["xmp-abcdef12"],
            "preset_path": r"E:\styles\one.xmp",
        },
        active_profile="8gb",
    )
    assert exported["model_profile_preference"] == "16gb"
    assert exported["style_sources"]["hidden_resource_ids"] == ["xmp-abcdef12"]
    serialized = json.dumps(exported, ensure_ascii=False)
    assert "preset_path" not in serialized
    assert "E:\\" not in serialized
