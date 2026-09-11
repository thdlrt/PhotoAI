from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from landscape_culler.style_library import (
    _lua_literal,
    load_style_index,
    managed_style_root,
    managed_registry_lua_path,
    managed_registry_path,
    package_checksums_path,
    registration_state_path,
    sync_style_library,
)
from landscape_culler.util import read_json, write_json


def test_managed_style_root_uses_portable_content_layout(
    tmp_path: Path, monkeypatch
) -> None:
    content_root = (tmp_path / "portable").resolve()
    state = content_root / "state"
    styles = content_root / "styles"
    state.mkdir(parents=True)
    styles.mkdir()
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(content_root))
    monkeypatch.setenv("PHOTO_AI_STYLES_DIR", str(styles))

    assert managed_style_root(state) == styles / "style-library"

    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    with pytest.raises(ValueError, match="PHOTO_AI_CONTENT_ROOT"):
        managed_style_root(outside)


def _preset(
    path: Path,
    *,
    uuid: str,
    name: str,
    exposure: str = "+0.10",
    preset_type: str = "Normal",
    supports_color: str = "True",
    adaptive: bool = False,
    tone_curve: bool = False,
    complex_look: bool = False,
    group: str = "Landscape",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    adaptive_xml = (
        "<crs:MaskGroupBasedCorrections><rdf:Seq/></crs:MaskGroupBasedCorrections>"
        if adaptive
        else ""
    )
    tone_curve_xml = (
        "<crs:ToneCurvePV2012><rdf:Seq>"
        "<rdf:li>0, 0</rdf:li><rdf:li>255, 255</rdf:li>"
        "</rdf:Seq></crs:ToneCurvePV2012>"
        if tone_curve
        else ""
    )
    complex_look_xml = (
        '<crs:Look><rdf:Description crs:Name="Adobe Color" '
        'crs:UUID="PROFILE"/></crs:Look>'
        if complex_look
        else ""
    )
    path.write_text(
        f'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:PresetType="{preset_type}" crs:UUID="{uuid}" crs:SupportsAmount="True"
   crs:SupportsColor="{supports_color}" crs:CameraModelRestriction=""
   crs:Exposure2012="{exposure}" crs:Version="15.3" crs:ProcessVersion="15.3">
   <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">{name}</rdf:li></rdf:Alt></crs:Name>
   <crs:Group><rdf:Alt><rdf:li xml:lang="x-default">{group}</rdf:li></rdf:Alt></crs:Group>
   {adaptive_xml}{tone_curve_xml}{complex_look_xml}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>''',
        encoding="utf-8",
    )


def _creative_look(path: Path, *, table_payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:PresetType="Look" crs:Cluster="Adobe"
   crs:UUID="9901408EBF8D496E99EFC526805F2F7C"
   crs:SupportsAmount="True" crs:SupportsColor="True"
   crs:Version="18.0" crs:ProcessVersion="15.4"
   crs:Saturation="-27" crs:LookTable="LOOK-TABLE-ID"
   crs:Table_LOOK-TABLE-ID="{table_payload}"
   crs:RGBTable="RGB-TABLE-ID" crs:RGBTableAmount="0.5"
   crs:Table_RGB-TABLE-ID="rgb-table-payload" crs:HasSettings="True">
   <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">Film-Inspired 12</rdf:li></rdf:Alt></crs:Name>
   <crs:Group><rdf:Alt><rdf:li xml:lang="x-default">Film-Inspired</rdf:li></rdf:Alt></crs:Group>
   <crs:ToneCurvePV2012><rdf:Seq>
    <rdf:li>0, 0</rdf:li><rdf:li>128, 132</rdf:li><rdf:li>255, 255</rdf:li>
   </rdf:Seq></crs:ToneCurvePV2012>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>''',
        encoding="utf-8",
    )


def test_style_library_indexes_managed_and_installed_without_copying(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    installed = tmp_path / "Lightroom" / "Resources" / "Settings"
    film = managed / "OpenFilmStocks" / "film.xmp"
    duplicate = managed / "lightroom-workflow" / "same-recipe.xmp"
    labs = managed / "Lightroom-Presets" / "labs.xmp"
    adaptive = managed / "OpenFilmStocks" / "adaptive.xmp"
    adobe = installed / "Premium" / "travel.xmp"
    profile = installed / "Adobe" / "Profiles" / "not-a-preset.xmp"
    _preset(film, uuid="AAAA", name="Film Warm")
    _preset(duplicate, uuid="BBBB", name="Duplicate Name")
    _preset(labs, uuid="CCCC", name="Labs Golden", exposure="+0.20")
    _preset(adaptive, uuid="DDDD", name="Adaptive Sky", exposure="+0.30", adaptive=True)
    _preset(adobe, uuid="EEEE", name="Travel Color", exposure="+0.40")
    _preset(profile, uuid="FFFF", name="Profile", preset_type="Look")

    result = sync_style_library(data_dir, managed_root=managed, adobe_roots=[installed])

    assert result["summary"]["total"] == 5
    assert result["summary"]["managed"] == 4
    assert result["summary"]["installed"] == 1
    assert result["summary"]["skipped_non_normal"] == 1
    by_uuid = {item["uuid"]: item for item in result["entries"]}
    assert by_uuid["AAAA"]["license"] == "MIT"
    assert by_uuid["AAAA"]["source_supports_amount"] is True
    assert by_uuid["AAAA"]["supports_amount"] is False
    assert by_uuid["AAAA"]["category"] in {"film", "landscape"}
    duplicate_pair = [by_uuid["AAAA"], by_uuid["BBBB"]]
    assert sum(bool(item.get("duplicate_of")) for item in duplicate_pair) == 1
    assert sum(bool(item.get("ai_eligible")) for item in duplicate_pair) == 0
    assert {item["registration_status"] for item in duplicate_pair} == {
        "awaiting_registration",
        "duplicate",
    }
    assert by_uuid["CCCC"]["experimental"] is True
    assert by_uuid["CCCC"]["ai_eligible"] is False
    assert by_uuid["DDDD"]["adaptive"] is True
    assert by_uuid["DDDD"]["ai_eligible"] is False
    assert by_uuid["EEEE"]["source_kind"] == "adobe-installed"
    assert by_uuid["EEEE"]["supports_amount"] is True
    assert by_uuid["EEEE"]["registration_status"] == "installed"
    assert by_uuid["EEEE"]["path"] == str(adobe.resolve())
    assert "FFFF" not in by_uuid
    assert not (managed / adobe.name).exists()
    assert result["default_pool"] == ["uuid:eeee"]
    registry = read_json(managed_registry_path(data_dir))
    assert registry["registration_status"] == "awaiting_lightroom_plugin"
    assert registry["summary"] == {"total": 2, "with_uuid": 2}
    assert all(item["target_scope"] == "plugin" for item in registry["entries"])
    assert all(
        item["plugin_name"].startswith("PhotoAI::") for item in registry["entries"]
    )
    settings = registry["entries"][0]["develop_settings"]
    assert settings["Exposure2012"] == 0.1
    assert settings["ProcessVersion"] == "15.3"
    assert "SupportsAmount" not in settings
    lua_registry = managed_registry_lua_path(data_dir).read_text(encoding="utf-8")
    assert lua_registry.startswith("return {")
    assert registry["registry_hash"] in lua_registry
    assert registry["entries"][0]["plugin_name"] in lua_registry
    package_sums = package_checksums_path(data_dir).read_text(encoding="utf-8")
    assert "  index.json" in package_sums
    assert "  managed-preset-registry.json" in package_sums
    assert "  managed-preset-registry.lua" in package_sums
    assert "PACKAGE-SHA256SUMS" not in package_sums
    assert load_style_index(data_dir)["generated_at"] == result["generated_at"]


def test_style_library_settings_control_sources_and_persist_hidden_items(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    uploaded_root = managed / "user-upload"
    adobe_copy_root = managed / "sources" / "adobe-local-copy"
    write_json(
        uploaded_root / "SOURCE.json",
        {
            "id": "user-upload",
            "title": "User upload",
            "license_spdx": "user-provided",
            "tier": "curated_candidate",
        },
    )
    write_json(
        adobe_copy_root / "SOURCE.json",
        {
            "id": "adobe-local-copy",
            "title": "Adobe local copy",
            "license_spdx": "installed-reference",
            "tier": "local_only_proprietary",
        },
    )
    _preset(uploaded_root / "presets" / "user.xmp", uuid="USER-001", name="User")
    _preset(adobe_copy_root / "adobe.xmp", uuid="ADOBE-001", name="Adobe")
    write_json(
        managed / "settings.json",
        {
            "include_lightroom_presets": False,
            "include_adobe_local_copy": False,
            "hidden_resource_ids": [],
        },
    )

    first = sync_style_library(data_dir)

    assert first["installed_roots"] == []
    assert {item.get("source_id") for item in first["entries"]} == {"user-upload"}
    resource_id = first["entries"][0]["resource_id"]
    write_json(
        managed / "settings.json",
        {
            "include_lightroom_presets": False,
            "include_adobe_local_copy": False,
            "hidden_resource_ids": [resource_id],
        },
    )

    hidden = sync_style_library(data_dir)
    entry = hidden["entries"][0]
    assert entry["resource_id"] == resource_id
    assert entry["user_hidden"] is True
    assert entry["hidden"] is True
    assert entry["ai_eligible"] is False

    write_json(
        managed / "settings.json",
        {
            "include_lightroom_presets": False,
            "include_adobe_local_copy": False,
            "hidden_resource_ids": [],
        },
    )
    restored = sync_style_library(data_dir)
    assert restored["entries"][0]["user_hidden"] is False
    assert restored["entries"][0]["hidden"] is False


def test_two_source_mode_keeps_only_lightroom_and_user_uploads(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    installed = tmp_path / "Lightroom" / "Settings"
    user_root = managed / "user-upload"
    bundled_root = managed / "OpenFilmStocks"
    adobe_copy_root = managed / "sources" / "adobe-local-copy"
    write_json(
        user_root / "SOURCE.json",
        {
            "id": "user-upload",
            "title": "User upload",
            "license_spdx": "user-provided",
            "tier": "curated_candidate",
        },
    )
    write_json(
        adobe_copy_root / "SOURCE.json",
        {
            "id": "adobe-local-copy",
            "title": "Adobe local copy",
            "license_spdx": "installed-reference",
            "tier": "local_only_proprietary",
        },
    )
    _preset(user_root / "presets" / "user.xmp", uuid="USER-002", name="User")
    _preset(bundled_root / "film.xmp", uuid="BUNDLED-001", name="Bundled")
    _preset(adobe_copy_root / "copy.xmp", uuid="COPY-001", name="Copy")
    _preset(installed / "installed.xmp", uuid="LR-001", name="Installed")
    write_json(
        managed / "settings.json",
        {
            "source_mode": "lightroom_user_only",
            "include_lightroom_presets": False,
            "include_user_uploads": True,
        },
    )

    first = sync_style_library(data_dir, adobe_roots=[installed])
    by_uuid = {item["uuid"]: item for item in first["entries"]}

    assert set(by_uuid) == {"USER-002", "LR-001"}
    assert by_uuid["USER-002"]["source_disabled"] is False
    assert by_uuid["LR-001"]["source_disabled"] is True
    assert by_uuid["LR-001"]["hidden"] is True

    write_json(
        managed / "settings.json",
        {
            "source_mode": "lightroom_user_only",
            "include_lightroom_presets": True,
            "include_user_uploads": False,
        },
    )
    second = sync_style_library(data_dir, adobe_roots=[installed])
    by_uuid = {item["uuid"]: item for item in second["entries"]}
    assert by_uuid["USER-002"]["source_disabled"] is True
    assert by_uuid["LR-001"]["source_disabled"] is False


def test_generic_adobe_creative_profile_enters_lut_pool(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    installed = tmp_path / "Lightroom" / "Resources" / "Settings"
    profile = installed / "Adobe" / "Profiles" / "Modern" / "Modern 10.xmp"
    _preset(
        profile,
        uuid="0682C30D08BCAEE35E8B7643AFAF64C1",
        name="Modern 10",
        preset_type="Look",
        group="Modern",
    )

    result = sync_style_library(data_dir, managed_root=managed, adobe_roots=[installed])

    assert result["summary"]["creative_profiles"] == 1
    assert result["summary"]["skipped_non_normal"] == 0
    entry = result["entries"][0]
    assert entry["look_kind"] == "lightroom_profile"
    assert entry["profile_name"] == "Modern 10"
    assert entry["supports_amount"] is True
    assert entry["xmp_compatible"] is True
    assert entry["plugin_registration_supported"] is False
    assert entry["registration_status"] == "installed"
    assert entry["ai_eligible"] is True
    assert result["default_pool"] == [entry["preset_id"]]


def test_creative_look_descriptor_is_compact_stable_and_cache_addressed(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    installed = tmp_path / "Lightroom" / "Resources" / "Settings"
    profile = installed / "Premium" / "Style - Film" / "Profiles" / "Film 12.xmp"
    table_payload = "look-table-a-" + ("A" * 16_384)
    _creative_look(profile, table_payload=table_payload)

    first = sync_style_library(data_dir, managed_root=managed, adobe_roots=[installed])

    entry = first["entries"][0]
    descriptor = entry["look_descriptor"]
    assert descriptor["UUID"] == "9901408EBF8D496E99EFC526805F2F7C"
    assert descriptor["Name"] == "Film-Inspired 12"
    assert descriptor["Group"] == "Film-Inspired"
    assert descriptor["Cluster"] == "Adobe"
    assert descriptor["SupportsAmount"] is True
    assert descriptor["Parameters"] == {
        "LookTable": "LOOK-TABLE-ID",
        "ProcessVersion": "15.4",
        "RGBTable": "RGB-TABLE-ID",
        "RGBTableAmount": 0.5,
        "Saturation": -27,
        "ToneCurvePV2012": ["0, 0", "128, 132", "255, 255"],
    }
    assert not any(key.startswith("Table_") for key in descriptor["Parameters"])
    assert descriptor["TableDigests"]["Table_LOOK-TABLE-ID"] == {
        "sha256": hashlib.sha256(table_payload.encode()).hexdigest(),
        "size": len(table_payload.encode()),
    }
    assert descriptor["TableDigests"]["Table_RGB-TABLE-ID"] == {
        "sha256": hashlib.sha256(b"rgb-table-payload").hexdigest(),
        "size": len(b"rgb-table-payload"),
    }
    unhashed = {key: value for key, value in descriptor.items() if key != "Hash"}
    expected_hash = hashlib.sha256(
        json.dumps(
            unhashed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert descriptor["Hash"] == expected_hash
    assert entry["look_descriptor_hash"] == expected_hash
    assert entry["cache_identity"] == f"look:{expected_hash}"
    assert table_payload not in json.dumps(first, ensure_ascii=False)

    second = sync_style_library(data_dir, managed_root=managed, adobe_roots=[installed])
    assert second["summary"]["parsed"] == 0
    assert second["summary"]["reused"] == 1
    assert second["entries"][0]["look_descriptor_hash"] == expected_hash

    changed_payload = "look-table-b-" + ("A" * 16_384)
    _creative_look(profile, table_payload=changed_payload)
    third = sync_style_library(data_dir, managed_root=managed, adobe_roots=[installed])
    changed = third["entries"][0]
    assert third["summary"]["parsed"] == 1
    assert changed["look_descriptor_hash"] != expected_hash
    assert changed["cache_identity"] != entry["cache_identity"]


def test_lua_registry_serializer_is_stable_and_escapes_code() -> None:
    value = {
        "z-key": 'quote " slash \\ newline\n tab\t control\x01 中文',
        "a": [None, True, False, -2, 1.25],
    }

    rendered = _lua_literal(value)

    assert rendered == _lua_literal(value)
    assert rendered.index("a =") < rendered.index('["z-key"] =')
    assert '\\"' in rendered
    assert "\\\\" in rendered
    assert "\\n" in rendered
    assert "\\t" in rendered
    assert "\\001" in rendered
    assert "中文" in rendered

    try:
        _lua_literal(float("inf"))
    except ValueError as exc:
        assert "finite" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("non-finite Lua number was accepted")


def test_managed_preset_enters_ai_pool_only_after_exact_hash_registration(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    preset = managed / "OpenFilmStocks" / "one.xmp"
    _preset(preset, uuid="AAAA", name="Film One")

    first = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])
    entry = first["entries"][0]
    assert entry["compatibility"] == "compatible"
    assert entry["registration_status"] == "awaiting_registration"
    assert entry["ai_eligible"] is False
    assert first["default_pool"] == []

    write_json(
        registration_state_path(data_dir),
        {
            "schema_version": 1,
            "entries": [
                {
                    "preset_id": entry["preset_id"],
                    "file_hash": entry["file_hash"],
                    "plugin_name": entry["plugin_name"],
                    "plugin_uuid": "PLUGIN-AAAA",
                    "scope": "plugin",
                    "status": "registered",
                }
            ],
        },
    )
    second = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])

    registered = second["entries"][0]
    assert registered["registration_status"] == "registered"
    assert registered["preset_scope"] == "plugin"
    assert registered["runtime_preset_uuid"] == "PLUGIN-AAAA"
    assert registered["source_supports_amount"] is True
    assert registered["supports_amount"] is False
    assert registered["ai_eligible"] is True
    assert second["default_pool"] == ["uuid:aaaa"]


def test_registry_serializes_flat_rdf_sequence_but_rejects_nested_look(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    _preset(
        managed / "OpenFilmStocks" / "curve.xmp",
        uuid="AAAA",
        name="Curve",
        exposure="-1.25",
        tone_curve=True,
    )
    _preset(
        managed / "OpenFilmStocks" / "nested.xmp",
        uuid="BBBB",
        name="Nested Look",
        complex_look=True,
    )

    result = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])
    by_uuid = {item["uuid"]: item for item in result["entries"]}
    supported = by_uuid["AAAA"]
    rejected = by_uuid["BBBB"]

    assert supported["plugin_registration_supported"] is True
    assert supported["develop_settings"]["Exposure2012"] == -1.25
    assert supported["develop_settings"]["ProcessVersion"] == "15.3"
    assert supported["develop_settings"]["ToneCurvePV2012"] == [
        "0, 0",
        "255, 255",
    ]
    assert rejected["plugin_registration_supported"] is False
    assert rejected["registration_status"] == "unsupported"
    assert rejected["develop_settings"] is None
    assert rejected["plugin_registration_reasons"] == [
        "Look: nested RDF structure is not lossless"
    ]
    registry = read_json(managed_registry_path(data_dir))
    assert {item["plugin_preset_id"] for item in registry["entries"]} == {"AAAA"}


def test_nonstandard_source_uuids_remain_opaque_and_do_not_collide(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    _preset(
        managed / "Lightroom-Presets" / "one.xmp",
        uuid="SP401-OPAQUE-ID",
        name="One",
    )
    _preset(
        managed / "Lightroom-Presets" / "two.xmp",
        uuid="RR401-OPAQUE-ID",
        name="Two",
        exposure="+0.20",
    )

    result = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])

    assert {item["uuid"] for item in result["entries"]} == {
        "SP401-OPAQUE-ID",
        "RR401-OPAQUE-ID",
    }
    assert all(item["uuid_standard"] is False for item in result["entries"])
    assert len({item["preset_id"] for item in result["entries"]}) == 2


def test_style_library_sync_reuses_unchanged_and_reparses_hash_change(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    preset = managed / "OpenFilmStocks" / "one.xmp"
    _preset(preset, uuid="AAAA", name="One")

    first = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])
    second = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])
    assert first["summary"]["parsed"] == 1
    assert second["summary"]["parsed"] == 0
    assert second["summary"]["reused"] == 1

    _preset(preset, uuid="AAAA", name="One", exposure="+0.95")
    third = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])
    assert third["summary"]["parsed"] == 1
    assert third["entries"][0]["file_hash"] != first["entries"][0]["file_hash"]


def test_invalid_xmp_remains_visible_but_never_enters_ai_pool(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    broken = managed / "OpenFilmStocks" / "broken.xmp"
    broken.parent.mkdir(parents=True)
    broken.write_text("<not-xml", encoding="utf-8")

    result = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])

    assert result["entries"][0]["compatibility"] == "invalid"
    assert result["entries"][0]["ai_eligible"] is False
    assert result["default_pool"] == []


def test_managed_resource_uses_pinned_source_manifest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    source_root = managed / "sources" / "custom-id"
    _preset(source_root / "xmp" / "one.xmp", uuid="AAAA", name="One")
    write_json(
        source_root / "SOURCE.json",
        {
            "id": "custom-id",
            "title": "Pinned Source",
            "tier": "community_experimental",
            "source_url": "https://example.invalid/source",
            "commit": "abc123",
            "license_spdx": "MIT",
        },
    )

    result = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])

    entry = result["entries"][0]
    assert entry["source"] == "Pinned Source"
    assert entry["source_id"] == "custom-id"
    assert entry["source_commit"] == "abc123"
    assert entry["source_tier"] == "community_experimental"
    assert entry["experimental"] is True
    assert entry["ai_eligible"] is False


def test_local_only_adobe_copy_is_not_registered_as_plugin_preset(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    source_root = managed / "sources" / "adobe-local-copy"
    _preset(source_root / "xmp" / "one.xmp", uuid="AAAA", name="Adobe Local")
    write_json(
        source_root / "SOURCE.json",
        {
            "id": "adobe-local-copy",
            "title": "Adobe Lightroom installed presets (local copy)",
            "tier": "local_only_proprietary",
            "license_spdx": "LicenseRef-Adobe-Proprietary-Local-Install",
        },
    )

    result = sync_style_library(data_dir, managed_root=managed, adobe_roots=[])

    entry = result["entries"][0]
    assert entry["registration_status"] == "local_copy"
    assert entry["preset_scope"] == "catalog"
    assert entry["ai_eligible"] is False
    assert read_json(managed_registry_path(data_dir))["entries"] == []


def test_bw_and_utility_are_excluded_without_misreading_sharpen_masking(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    managed = data_dir / "style-library"
    installed = tmp_path / "Lightroom" / "Resources" / "Settings"
    _preset(managed / "OpenFilmStocks" / "Base B_W.xmp", uuid="AAAA", name="Base B/W")
    _preset(
        managed / "OpenFilmStocks" / "1 ----- ACTION -----.xmp",
        uuid="DDDD",
        name="1 ----- ACTION -----",
    )
    sharp = installed / "Adobe" / "Presets" / "Sharpening" / "Sharp.xmp"
    _preset(sharp, uuid="BBBB", name="Sharpening - Medium")
    creative = installed / "Premium" / "Travel.xmp"
    _preset(creative, uuid="CCCC", name="Travel Natural")

    result = sync_style_library(data_dir, managed_root=managed, adobe_roots=[installed])
    by_uuid = {item["uuid"]: item for item in result["entries"]}

    assert by_uuid["AAAA"]["black_and_white"] is True
    assert by_uuid["AAAA"]["ai_eligible"] is False
    assert by_uuid["DDDD"]["separator"] is True
    assert by_uuid["DDDD"]["hidden"] is True
    assert by_uuid["DDDD"]["preset_id"] not in result["visible_pool"]
    registry = read_json(managed_registry_path(data_dir))
    assert "uuid:dddd" not in {item["preset_id"] for item in registry["entries"]}
    assert by_uuid["BBBB"]["adaptive"] is False
    assert by_uuid["BBBB"]["utility"] is True
    assert by_uuid["BBBB"]["ai_eligible"] is False
    assert by_uuid["CCCC"]["adaptive"] is False
    assert by_uuid["CCCC"]["ai_eligible"] is True
