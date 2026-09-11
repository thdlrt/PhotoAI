from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .util import atomic_write_text, read_json, write_json

STYLE_LIBRARY_SCHEMA_VERSION = 3
STYLE_PRESET_PARSER_VERSION = 7
STYLE_REGISTRY_SCHEMA_VERSION = 1
LOOK_DESCRIPTOR_SCHEMA_VERSION = 1
CRS_NAMESPACE = "http://ns.adobe.com/camera-raw-settings/1.0/"
RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
CRS = f"{{{CRS_NAMESPACE}}}"
RDF = f"{{{RDF_NAMESPACE}}}"

# Lightroom's shipped recipes are referenced in place. Managed resources live
# below the configured application style root and are never inferred from a
# development-machine drive letter.
DEFAULT_ADOBE_SETTINGS_ROOTS = (
    Path(r"C:\Program Files\Adobe\Adobe Lightroom Classic\Resources\Settings"),
    Path(r"C:\ProgramData\Adobe\CameraRaw\Settings"),
)

_GENERIC_CREATIVE_PROFILE_GROUPS = frozenset(
    {
        "artistic",
        "modern",
        "vintage",
        "film inspired",
        "film-inspired",
        "film inspired profiles",
    }
)

SOURCE_METADATA: dict[str, dict[str, Any]] = {
    "openfilmstocks": {
        "source": "OpenFilmStocks",
        "license": "MIT",
        "source_url": "https://github.com/eliseomartelli/OpenFilmStocks",
        "experimental": False,
    },
    "lightroom-workflow": {
        "source": "lightroom-workflow",
        "license": "MPL-2.0",
        "source_url": "https://github.com/thejoltjoker/lightroom-workflow",
        "experimental": False,
    },
    "lightroom-presets": {
        "source": "Lightroom-Presets",
        "license": "MIT",
        "source_url": "https://github.com/peva3/Lightroom-Presets",
        "experimental": True,
    },
}

_IDENTITY_FIELDS = {
    "UUID",
    "Name",
    "ShortName",
    "SortName",
    "Group",
    "Description",
    "Cluster",
    "Copyright",
    "ContactInfo",
}
_PRESET_METADATA_FIELDS = _IDENTITY_FIELDS | {
    "Amount",
    "CameraModelRestriction",
    "Cluster",
    "CompatibleVersion",
    "HasSettings",
    "PresetType",
    "RequiresRGBTables",
    "RequiresRenditionBehavior",
    "SupportsAmount",
    "SupportsAmount2",
    "SupportsColor",
    "SupportsHighDynamicRange",
    "SupportsMonochrome",
    "SupportsNormalDynamicRange",
    "SupportsOutputReferred",
    "SupportsSceneReferred",
    "UUID",
    "Version",
}
_STRING_DEVELOP_FIELDS = {
    "CameraProfile",
    "LensProfileFilename",
    "LensProfileName",
    "LensProfileSetup",
    "ProcessVersion",
    "ToneCurveName2012",
    "Treatment",
    "WhiteBalance",
}
_PROFILE_FIELDS = {
    "CameraProfile",
    "Look",
    "LookName",
    "ProfileName",
    "Profile",
    "ProfileID",
}
_LOOK_DESCRIPTOR_EXCLUDED_FIELDS = (
    _PRESET_METADATA_FIELDS
    | _IDENTITY_FIELDS
    | {
        "ShowInPresets",
        "ShowInQuickActions",
    }
)
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("black_and_white", ("b&w", "black and white", "monochrome", "黑白")),
    (
        "film",
        (
            "film",
            "kodak",
            "fuji",
            "portra",
            "cinema",
            "stock",
            "ilford",
            "ektar",
            "velvia",
            "provia",
            "kodachrome",
            "cinestill",
            "polaroid",
            "tri-x",
            "t-max",
            "胶片",
        ),
    ),
    ("landscape", ("landscape", "scenery", "风光", "风景")),
    ("hdr", ("hdr", "dynamic range")),
    ("season", ("spring", "summer", "autumn", "winter", "fall", "四季", "秋", "冬")),
    ("region", ("travel", "asia", "europe", "africa", "america", "tropical", "旅行")),
    ("portrait", ("portrait", "skin", "人像")),
    ("tone", ("warm", "cool", "golden", "blue hour", "matte", "vintage", "冷", "暖")),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bool(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _default_user_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("PHOTO_AI_LIGHTROOM_PRESET_DIRS", "")
    if configured:
        roots.extend(
            Path(item) for item in configured.split(os.pathsep) if item.strip()
        )
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "Adobe" / "CameraRaw" / "Settings")
    return roots


def _default_adobe_roots() -> list[Path]:
    roots: list[Path] = []
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            roots.append(
                Path(base) / "Adobe" / "Adobe Lightroom Classic" / "Resources" / "Settings"
            )
    program_data = os.environ.get("ProgramData")
    if program_data:
        roots.append(Path(program_data) / "Adobe" / "CameraRaw" / "Settings")
    explicit = os.environ.get("PHOTO_AI_LIGHTROOM")
    if explicit:
        install_path = Path(explicit).expanduser()
        install_root = install_path.parent if install_path.suffix.casefold() == ".exe" else install_path
        roots.append(install_root / "Resources" / "Settings")
    try:
        from .lightroom_bridge import _registry_lightroom_paths

        roots.extend(
            executable.parent / "Resources" / "Settings"
            for executable in _registry_lightroom_paths()
        )
    except (ImportError, OSError):
        pass
    # Retain standard Windows fallbacks for callers running without normal
    # ProgramFiles/ProgramData variables, but never add a developer drive.
    roots.extend(DEFAULT_ADOBE_SETTINGS_ROOTS)
    return roots


def discover_adobe_settings_roots(extra_roots: Iterable[Path] = ()) -> list[Path]:
    """Return unique installed/user Lightroom roots without copying their files."""

    configured = os.environ.get("PHOTO_AI_LIGHTROOM_SETTINGS_ROOT", "")
    candidates = [Path(configured)] if configured else []
    candidates.extend(_default_adobe_roots())
    candidates.extend(_default_user_roots())
    candidates.extend(Path(item) for item in extra_roots)
    unique: list[Path] = []
    seen: set[str] = set()
    for root in candidates:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root.absolute()
        key = os.path.normcase(str(resolved))
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _explicit_roots(roots: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in roots:
        try:
            root = Path(candidate).resolve()
        except OSError:
            root = Path(candidate).absolute()
        key = os.path.normcase(str(root))
        if key in seen or not root.is_dir():
            continue
        seen.add(key)
        unique.append(root)
    return unique


def managed_style_root(data_dir: Path) -> Path:
    data_root = Path(data_dir).resolve()
    content_root_value = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    styles_root_value = os.environ.get("PHOTO_AI_STYLES_DIR")
    if content_root_value and styles_root_value:
        content_root = Path(content_root_value).expanduser().resolve()
        styles_root = Path(styles_root_value).expanduser().resolve()
        try:
            data_root.relative_to(content_root)
            styles_root.relative_to(content_root)
        except ValueError as exc:
            raise ValueError(
                "风格库路径必须位于 PHOTO_AI_CONTENT_ROOT 内。"
            ) from exc
        return styles_root / "style-library"
    return data_root / "style-library"


def style_index_path(data_dir: Path) -> Path:
    return managed_style_root(data_dir) / "index.json"


def managed_registry_path(data_dir: Path) -> Path:
    return managed_style_root(data_dir) / "managed-preset-registry.json"


def managed_registry_lua_path(data_dir: Path) -> Path:
    return managed_style_root(data_dir) / "managed-preset-registry.lua"


def registration_state_path(data_dir: Path) -> Path:
    """Return the optional Lightroom-confirmed managed preset state file.

    The Python index writes ``managed-preset-registry.json`` as the desired
    plugin registry.  The Lightroom integration may later publish
    ``managed-preset-registration.json`` after it has actually registered and
    enumerated those presets.  Merely having an XMP in the software bundle is
    deliberately not treated as proof that Lightroom can resolve its UUID.
    """

    return managed_style_root(data_dir) / "managed-preset-registration.json"


def package_checksums_path(data_dir: Path) -> Path:
    return managed_style_root(data_dir) / "PACKAGE-SHA256SUMS"


def load_style_index(data_dir: Path) -> dict[str, Any]:
    path = style_index_path(data_dir)
    if not path.is_file():
        return {
            "schema_version": STYLE_LIBRARY_SCHEMA_VERSION,
            "generated_at": None,
            "entries": [],
            "default_pool": [],
            "visible_pool": [],
            "summary": {
                "total": 0,
                "default_pool": 0,
                "awaiting_registration": 0,
            },
        }
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise TypeError("风格库索引格式无效。")
    return payload


def style_resource_id(entry: dict[str, Any]) -> str:
    """Return an opaque identity for one exact indexed XMP resource.

    A preset UUID alone is not enough because Lightroom and the managed library
    can contain byte-different resources with the same UUID.  The locator binds
    the identity to its source while the file hash prevents a later replacement
    from inheriting an old user visibility choice.
    """

    encoded = "\0".join(
        (
            str(entry.get("source_kind") or ""),
            str(entry.get("locator") or ""),
            str(entry.get("file_hash") or ""),
        )
    ).encode("utf-8")
    return f"xmp-{hashlib.sha256(encoded).hexdigest()[:32]}"


@lru_cache(maxsize=64)
def _read_source_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = read_json(path)
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _source_for_managed(path: Path, managed_root: Path) -> dict[str, Any]:
    for parent in path.parents:
        if parent == managed_root.parent:
            break
        source_manifest = parent / "SOURCE.json"
        if source_manifest.is_file() and (
            payload := _read_source_manifest(source_manifest)
        ):
            tier = str(payload.get("tier") or "")
            return {
                "source": str(payload.get("title") or payload.get("id") or parent.name),
                "source_id": str(payload.get("id") or parent.name),
                "license": str(payload.get("license_spdx") or "unknown"),
                "source_url": payload.get("source_url"),
                "source_commit": payload.get("commit"),
                "source_tier": tier or None,
                "experimental": tier.casefold() == "community_experimental",
            }
    try:
        relative = path.relative_to(managed_root)
    except ValueError:
        relative = path
    lowered = "/".join(relative.parts).casefold()
    for key, metadata in SOURCE_METADATA.items():
        if key in lowered:
            return dict(metadata)
    return {
        "source": relative.parts[0] if len(relative.parts) > 1 else "managed",
        "license": "unknown",
        "source_url": None,
        "experimental": True,
    }


def _category(text: str, *, black_and_white: bool) -> str:
    if black_and_white:
        return "black_and_white"
    lowered = text.casefold()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "creative"


def _is_black_and_white(text: str, attributes: dict[str, str]) -> bool:
    lowered = text.casefold()
    return (
        _bool(attributes.get("ConvertToGrayscale"))
        or attributes.get("SupportsColor", "").casefold() == "false"
        or any(
            token in lowered
            for token in ("b&w", "b/w", "black and white", "monochrome", "黑白")
        )
        or re.search(r"(?:^|[^a-z0-9])bw(?:[^a-z0-9]|$)", lowered) is not None
        or any(token in lowered for token in ("ilford", "tri-x", "t-max"))
    )


def _is_utility_preset(name: str, group: str, relative_path: str) -> bool:
    lowered = f"{name} {group} {relative_path}".casefold().replace("\\", "/")
    folder_parts = {part.strip() for part in lowered.split("/")}
    if "-----" in name:
        return True
    if folder_parts & {
        "utility",
        "defaults",
        "optics",
        "sharpening",
        "grain",
        "vignetting",
        "curve",
    }:
        return True
    utility_terms = (
        "base b/w",
        "base colour",
        "base color",
        "lens correction",
        "reset ",
        "wb ",
        "exposure +",
        "exposure -",
        "noise reduction",
        "sharpening -",
        "grain -",
    )
    return any(term in lowered for term in utility_terms)


def _is_separator_preset(name: str, relative_path: str) -> bool:
    """Identify menu dividers shipped as XMP files, not photographic looks."""

    text = f"{name} {Path(relative_path).stem}"
    return "-----" in text or re.fullmatch(r"\s*[-_=]{3,}\s*", text) is not None


def _is_generic_creative_profile(
    *,
    name: str,
    group: str,
    relative_path: str,
    source_kind: str,
    source_id: str | None,
) -> bool:
    """Keep camera/DCP profiles out of the creative-look candidate pool.

    Adobe's generic Artistic/Modern/Vintage profiles are camera-independent
    LookTable/RGBTable resources.  Camera Matching and Adobe Raw profiles are
    base render profiles, not post-calibration creative choices.  User-created
    Look profiles are accepted unless their path clearly identifies one of
    those base-profile families.
    """

    lowered_path = relative_path.replace("\\", "/").casefold()
    lowered_group = group.strip().casefold()
    base_profile_path = any(
        marker in f"/{lowered_path}/"
        for marker in ("/camera/", "/adobe raw/", "/adaptive/")
    )
    if base_profile_path or lowered_group in {
        "camera matching",
        "adobe raw",
        "adaptive",
    }:
        return False
    adobe_owned = (
        source_kind == "adobe-installed"
        or str(source_id or "").casefold() == "adobe-local-copy"
    )
    if adobe_owned:
        path_parts = {part.strip().casefold() for part in Path(relative_path).parts}
        return bool(
            lowered_group in _GENERIC_CREATIVE_PROFILE_GROUPS
            or path_parts.intersection(_GENERIC_CREATIVE_PROFILE_GROUPS)
        )
    return bool(name.strip())


def _text_value(description: ElementTree.Element, field: str) -> str:
    node = description.find(f"{CRS}{field}")
    if node is None:
        return ""
    default = node.find(
        f".//{RDF}li[@{{http://www.w3.org/XML/1998/namespace}}lang='x-default']"
    )
    if default is not None and default.text:
        return default.text.strip()
    for item in node.findall(f".//{RDF}li"):
        if item.text:
            return item.text.strip()
    return (node.text or "").strip()


def _recipe_hash(description: ElementTree.Element) -> str:
    """Hash develop settings while ignoring display/identity metadata."""

    values: list[tuple[str, str]] = []
    for key, value in description.attrib.items():
        local = _local_name(key)
        if key.startswith(CRS) and local not in _IDENTITY_FIELDS:
            values.append((f"attribute:{local}", str(value).strip()))
    for node in description.iter():
        local = _local_name(node.tag)
        if not str(node.tag).startswith(CRS) or local in _IDENTITY_FIELDS:
            continue
        text = " ".join("".join(node.itertext()).split())
        attrs = sorted(
            (_local_name(key), str(value).strip()) for key, value in node.attrib.items()
        )
        values.append((f"element:{local}:{attrs!r}", text))
    encoded = repr(sorted(values)).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def _complex_parameter_digest(node: ElementTree.Element) -> str:
    """Hash a nested CRS value without persisting its potentially large payload."""

    def normalized(item: ElementTree.Element) -> dict[str, Any]:
        return {
            "tag": _local_name(str(item.tag)),
            "attributes": {
                _local_name(str(key)): " ".join(str(value).split())
                for key, value in sorted(
                    item.attrib.items(), key=lambda pair: str(pair[0])
                )
            },
            "text": " ".join((item.text or "").split()),
            "children": [normalized(child) for child in list(item)],
        }

    return _canonical_json_hash(normalized(node))


def _look_descriptor(
    description: ElementTree.Element,
    *,
    attributes: Mapping[str, str],
    uuid: str | None,
    name: str,
    group: str,
) -> tuple[dict[str, Any], str]:
    """Build a compact, deterministic descriptor for an Adobe Creative Look.

    Creative Look XMP commonly embeds one or more very large ``Table_*``
    payloads.  Lightroom resolves those tables through ``LookTable`` /
    ``RGBTable`` identifiers in the installed profile, so callers only need the
    identifiers, scalar/flat-array parameters, and content digests.  Keeping the
    table bytes out of the descriptor prevents them from being copied into
    queued Lightroom jobs while the digests still invalidate cached identities
    whenever the installed recipe changes.
    """

    parameters: dict[str, Any] = {}
    table_digests: dict[str, dict[str, Any]] = {}
    complex_parameter_digests: dict[str, str] = {}

    for field, raw_value in attributes.items():
        value = str(raw_value).strip()
        if field.startswith("Table_"):
            encoded = value.encode("utf-8", "surrogatepass")
            table_digests[field] = {
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "size": len(encoded),
            }
        elif field not in _LOOK_DESCRIPTOR_EXCLUDED_FIELDS:
            parameters[field] = _typed_develop_value(field, value)

    for node in list(description):
        if not str(node.tag).startswith(CRS):
            continue
        field = _local_name(str(node.tag))
        if field in _LOOK_DESCRIPTOR_EXCLUDED_FIELDS:
            continue
        children = list(node)
        if not children:
            value = (node.text or "").strip()
            if field.startswith("Table_"):
                encoded = value.encode("utf-8", "surrogatepass")
                table_digests[field] = {
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "size": len(encoded),
                }
            elif value and not node.attrib:
                parameters[field] = _typed_develop_value(field, value)
            elif node.attrib:
                complex_parameter_digests[field] = _complex_parameter_digest(node)
            continue

        if len(children) == 1 and children[0].tag in {f"{RDF}Seq", f"{RDF}Bag"}:
            items = list(children[0])
            if all(
                item.tag == f"{RDF}li" and not list(item) and not item.attrib
                for item in items
            ):
                parameters[field] = [
                    _typed_develop_value(field, item.text or "") for item in items
                ]
                continue
        complex_parameter_digests[field] = _complex_parameter_digest(node)

    payload: dict[str, Any] = {
        "SchemaVersion": LOOK_DESCRIPTOR_SCHEMA_VERSION,
        "UUID": uuid,
        "Name": name,
        "Group": group,
        "Cluster": str(attributes.get("Cluster") or ""),
        "SupportsAmount": _bool(attributes.get("SupportsAmount")),
        "Parameters": dict(sorted(parameters.items())),
        "TableDigests": dict(sorted(table_digests.items())),
        "ComplexParameterDigests": dict(sorted(complex_parameter_digests.items())),
    }
    descriptor_hash = _canonical_json_hash(payload)
    return {**payload, "Hash": descriptor_hash}, descriptor_hash


def _typed_develop_value(field: str, value: str) -> bool | int | float | str:
    stripped = value.strip()
    if field in _STRING_DEVELOP_FIELDS:
        return stripped
    lowered = stripped.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"[+-]?\d+", stripped):
        return int(stripped)
    if re.fullmatch(
        r"[+-]?(?:\d+\.\d*|\.\d+)(?:e[+-]?\d+)?",
        stripped,
        re.IGNORECASE,
    ):
        return float(stripped)
    return stripped


def _develop_settings(
    description: ElementTree.Element,
) -> tuple[dict[str, Any], list[str]]:
    """Translate the losslessly representable XMP recipe into SDK values.

    Lightroom's plugin preset provider accepts the same scalar and flat-array
    values returned by ``photo:getDevelopSettings()``.  Nested RDF descriptions
    (profiles/Looks, masks, and similar structures) cannot be represented
    faithfully by this table, so they are rejected instead of silently dropped.
    """

    settings: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in description.attrib.items():
        if not key.startswith(CRS):
            continue
        field = _local_name(key)
        if field in _PRESET_METADATA_FIELDS:
            continue
        settings[field] = _typed_develop_value(field, str(value))

    for node in list(description):
        if not str(node.tag).startswith(CRS):
            continue
        field = _local_name(node.tag)
        if field in _PRESET_METADATA_FIELDS:
            continue
        children = list(node)
        if not children:
            node_attributes = {
                _local_name(key): str(value).strip()
                for key, value in node.attrib.items()
                if str(key).startswith(CRS) and str(value).strip()
            }
            text_value = (node.text or "").strip()
            if node_attributes:
                errors.append(f"{field}: nested CRS attributes are not lossless")
            elif text_value:
                settings[field] = _typed_develop_value(field, text_value)
            continue

        if len(children) == 1 and children[0].tag in {f"{RDF}Seq", f"{RDF}Bag"}:
            sequence = children[0]
            values: list[Any] = []
            valid_sequence = True
            for item in list(sequence):
                if item.tag != f"{RDF}li" or list(item) or item.attrib:
                    valid_sequence = False
                    break
                values.append(_typed_develop_value(field, item.text or ""))
            if valid_sequence:
                settings[field] = values
            else:
                errors.append(f"{field}: complex RDF sequence is not lossless")
            continue

        errors.append(f"{field}: nested RDF structure is not lossless")
    return settings, errors


def inspect_xmp_preset(
    path: Path,
    *,
    source_kind: str,
    source_metadata: dict[str, Any],
    source_root: Path,
) -> dict[str, Any]:
    """Parse one Lightroom preset into a stable, serializable catalog entry."""

    path = Path(path).resolve()
    stat = path.stat()
    file_hash = _sha256(path)
    locator = f"{source_kind}:{os.path.normcase(str(path))}"
    base = {
        "locator": locator,
        "path": str(path),
        "relative_path": str(path.relative_to(source_root))
        if path.is_relative_to(source_root)
        else path.name,
        "file_hash": file_hash,
        "file_size": stat.st_size,
        "file_mtime_ns": stat.st_mtime_ns,
        "source_kind": source_kind,
        "parser_version": STYLE_PRESET_PARSER_VERSION,
        **source_metadata,
    }
    local_only_copy = (
        source_kind == "managed"
        and str(source_metadata.get("source_tier") or "").casefold()
        == "local_only_proprietary"
    )
    runtime_status = (
        "local_copy"
        if local_only_copy
        else "awaiting_registration"
        if source_kind == "managed"
        else "installed"
    )
    preset_scope = (
        "plugin" if source_kind == "managed" and not local_only_copy else "catalog"
    )
    try:
        root = ElementTree.parse(path).getroot()
        description = root.find(f".//{RDF}Description")
        if description is None:
            raise ValueError("missing rdf:Description")
    except (ElementTree.ParseError, OSError, ValueError) as exc:
        return {
            **base,
            "preset_id": f"sha256:{file_hash}",
            "uuid": None,
            "uuid_standard": False,
            "name": path.stem,
            "group": "",
            "preset_type": "invalid",
            "category": "other",
            "supports_amount": False,
            "adaptive": False,
            "black_and_white": False,
            "utility": False,
            "separator": False,
            "hidden": False,
            "profile_dependencies": [],
            "camera_restriction": "",
            "recipe_hash": None,
            "look_descriptor": None,
            "look_descriptor_hash": None,
            "cache_identity": f"xmp:{file_hash}",
            "compatibility": "invalid",
            "compatibility_reasons": [f"XMP 无法解析：{exc}"],
            "registration_status": runtime_status,
            "preset_scope": preset_scope,
            "plugin_registration_supported": False,
            "plugin_registration_reasons": ["XMP 无法解析"],
            "develop_settings": None,
            "ai_eligible": False,
        }

    attributes = {
        _local_name(key): str(value).strip()
        for key, value in description.attrib.items()
    }
    preset_type = attributes.get("PresetType", "").strip() or "Unknown"
    preset_type_key = preset_type.casefold()
    is_develop_preset = preset_type_key == "normal"
    is_creative_profile = preset_type_key == "look"
    raw_uuid = attributes.get("UUID", "").strip()
    uuid = (
        raw_uuid if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", raw_uuid) else None
    )
    compact_uuid = (uuid or "").replace("-", "")
    uuid_standard = bool(re.fullmatch(r"[0-9A-Fa-f]{32}", compact_uuid))
    name = _text_value(description, "Name") or path.stem
    group = _text_value(description, "Group")
    relative_path = str(base["relative_path"])
    combined = f"{name} {group} {Path(relative_path).parent.name}"
    black_and_white = _is_black_and_white(f"{name} {group} {relative_path}", attributes)
    separator = _is_separator_preset(name, relative_path)
    adaptive_names = {
        "correctionmasks",
        "maskgroupbasedcorrections",
        "paintbasedcorrections",
        "retouchareas",
        "adaptivepreset",
    }
    adaptive = (
        "adaptive" in combined.casefold()
        or any(
            key.casefold() in adaptive_names or key.casefold().startswith("maskgroup")
            for key in attributes
        )
        or any(
            _local_name(node.tag).casefold() in adaptive_names
            or _local_name(node.tag).casefold().startswith("maskgroup")
            for node in description.iter()
        )
    )
    utility = separator or _is_utility_preset(name, group, relative_path)
    camera_restriction = attributes.get("CameraModelRestriction", "")
    generic_creative_profile = bool(
        is_creative_profile
        and _is_generic_creative_profile(
            name=name,
            group=group,
            relative_path=relative_path,
            source_kind=source_kind,
            source_id=source_metadata.get("source_id"),
        )
    )
    profile_dependencies = sorted(
        {
            f"{field}:{attributes[field]}"
            for field in _PROFILE_FIELDS
            if attributes.get(field)
            and attributes[field].casefold() not in {"none", "default"}
        }
    )
    reasons: list[str] = []
    if not is_develop_preset and not is_creative_profile:
        reasons.append("不是 Lightroom 调整预设或 Creative Profile")
    if is_creative_profile and not generic_creative_profile:
        reasons.append("不是通用创意外观配置文件")
    if camera_restriction:
        reasons.append("限定相机型号")
    if adaptive:
        reasons.append("包含 Adaptive/AI 蒙版设置")
    compatibility = "compatible" if not reasons else "unsupported"
    if is_develop_preset:
        develop_settings, registration_reasons = _develop_settings(description)
    else:
        develop_settings, registration_reasons = {}, []
    if is_develop_preset and not uuid:
        registration_reasons.append("缺少稳定 UUID")
    plugin_registration_supported = bool(
        is_develop_preset
        and source_kind == "managed"
        and not local_only_copy
        and compatibility == "compatible"
        and not registration_reasons
    )
    if (
        source_kind == "managed"
        and not local_only_copy
        and not plugin_registration_supported
    ):
        runtime_status = "unsupported"
    experimental = bool(source_metadata.get("experimental"))
    license_name = str(source_metadata.get("license") or "unknown")
    runtime_resolvable = bool(
        runtime_status in {"installed", "registered"}
        or (is_creative_profile and local_only_copy)
    )
    eligible = (
        compatibility == "compatible"
        and runtime_resolvable
        and not black_and_white
        and not utility
        and not experimental
        and license_name.casefold() != "unknown"
    )
    preset_id = f"uuid:{uuid.casefold()}" if uuid else f"sha256:{file_hash}"
    if is_creative_profile:
        look_descriptor, look_descriptor_hash = _look_descriptor(
            description,
            attributes=attributes,
            uuid=uuid,
            name=name,
            group=group,
        )
        cache_identity = f"look:{look_descriptor_hash}"
    else:
        look_descriptor = None
        look_descriptor_hash = None
        cache_identity = f"xmp:{file_hash}"
    return {
        **base,
        "preset_id": preset_id,
        "uuid": uuid,
        "uuid_standard": uuid_standard,
        "name": name,
        "group": group,
        "version": attributes.get("Version") or attributes.get("ProcessVersion") or "",
        "preset_type": preset_type,
        "asset_kind": "creative_profile" if is_creative_profile else "develop_preset",
        "look_kind": "lightroom_profile" if is_creative_profile else "develop_preset",
        "profile_name": name if is_creative_profile else None,
        "profile_group": group if is_creative_profile else None,
        "generic_creative_profile": generic_creative_profile,
        "xmp_compatible": bool(
            is_creative_profile and runtime_resolvable and compatibility == "compatible"
        ),
        "category": "utility"
        if utility
        else _category(combined, black_and_white=black_and_white),
        # Preserve the XMP declaration separately from the capability of the
        # exact preset object Lightroom ultimately exposes at runtime.
        "source_supports_amount": _bool(attributes.get("SupportsAmount")),
        "supports_amount": _bool(attributes.get("SupportsAmount")),
        "adaptive": adaptive,
        "black_and_white": black_and_white,
        "utility": utility,
        "separator": separator,
        "hidden": separator,
        "profile_dependencies": profile_dependencies,
        "camera_restriction": camera_restriction,
        "recipe_hash": _recipe_hash(description),
        "look_descriptor": look_descriptor,
        "look_descriptor_hash": look_descriptor_hash,
        "cache_identity": cache_identity,
        "compatibility": compatibility,
        "compatibility_reasons": reasons,
        "registration_status": runtime_status,
        "preset_scope": preset_scope,
        "plugin_registration_supported": plugin_registration_supported,
        "plugin_registration_reasons": registration_reasons,
        "develop_settings": develop_settings if plugin_registration_supported else None,
        "ai_eligible": eligible,
    }


def _iter_xmp(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return sorted(
        (path for path in root.rglob("*.xmp") if path.is_file()),
        key=lambda item: str(item).casefold(),
    )


def _can_enter_ai_pool(entry: dict[str, Any]) -> bool:
    runtime_statuses = {"installed", "registered"}
    if entry.get("look_kind") == "lightroom_profile":
        runtime_statuses.add("local_copy")
    return bool(
        entry.get("compatibility") == "compatible"
        and entry.get("registration_status") in runtime_statuses
        and not entry.get("adaptive")
        and not entry.get("black_and_white")
        and not entry.get("utility")
        and not entry.get("experimental")
        and str(entry.get("license") or "unknown").casefold() != "unknown"
    )


def runtime_supports_amount(entry: Mapping[str, Any]) -> bool:
    """Whether Lightroom can vary this exact runtime preset object.

    ``addDevelopPresetForPlugin`` serializes plugin-owned presets as legacy
    ``.lrtemplate`` files containing only the develop-value table. The source
    XMP's ``SupportsAmount`` metadata is not retained, so Lightroom accepts the
    SDK amount argument but renders the hidden preset at 100%. Amount is only
    advertised for native catalog presets whose XMP capability survives.
    """

    if entry.get("look_kind") == "lightroom_profile":
        return bool(entry.get("source_supports_amount", entry.get("supports_amount")))
    return bool(
        str(entry.get("preset_scope") or "catalog").casefold() == "catalog"
        and entry.get("source_supports_amount", entry.get("supports_amount"))
    )


def _apply_validation(entry: dict[str, Any], validation: dict[str, Any]) -> None:
    key = f"{entry['preset_id']}@{entry['file_hash']}"
    result = validation.get(key)
    if not isinstance(result, dict):
        return
    status = str(result.get("compatibility") or "").strip()
    if status in {"compatible", "unsupported", "invalid"}:
        entry["compatibility"] = status
        entry["compatibility_reasons"] = [
            str(item) for item in result.get("reasons", [])
        ]
    entry["calibration"] = {
        "status": str(result.get("status") or "complete"),
        "features": dict(result.get("features") or {}),
        "rendered_at": result.get("rendered_at"),
    }
    entry["supports_amount"] = runtime_supports_amount(entry)
    entry["ai_eligible"] = _can_enter_ai_pool(entry)


def _load_registration_state(
    managed_root: Path,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load Lightroom-confirmed registrations keyed by preset id and file hash."""

    path = managed_root / "managed-preset-registration.json"
    if not path.is_file():
        return {}
    try:
        payload = read_json(path)
    except (OSError, TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    records = payload.get("entries") or payload.get("presets") or []
    if not isinstance(records, list):
        return {}
    registered: dict[tuple[str, str], dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict) or item.get("status") != "registered":
            continue
        preset_id = str(item.get("preset_id") or "")
        file_hash = str(item.get("file_hash") or "")
        # A UUID alone is insufficient: an upstream preset may change while
        # retaining its UUID.  The plugin must acknowledge the exact bytes.
        if preset_id and file_hash:
            registered[(preset_id, file_hash)] = item
    return registered


def _managed_plugin_name(entry: dict[str, Any]) -> str:
    return (
        f"PhotoAI::{str(entry.get('file_hash') or '')[:16]}::"
        f"{(entry.get('name') or 'Unnamed')!s}"
    )


def _apply_registration(
    entry: dict[str, Any],
    registered: dict[tuple[str, str], dict[str, Any]],
) -> None:
    if entry.get("source_kind") != "managed":
        entry["registration_status"] = "installed"
        entry["preset_scope"] = "catalog"
        entry["plugin_name"] = None
        entry["runtime_preset_uuid"] = entry.get("uuid")
    elif str(entry.get("source_tier") or "").casefold() == "local_only_proprietary":
        # This is a local app-managed mirror of an installed Adobe file, never a plugin
        # preset.  Its installed catalog twin is indexed separately and wins
        # duplicate resolution.
        entry["registration_status"] = "local_copy"
        entry["preset_scope"] = "catalog"
        entry["plugin_name"] = None
        entry["runtime_preset_uuid"] = None
    elif not entry.get("plugin_registration_supported"):
        entry["registration_status"] = "unsupported"
        entry["preset_scope"] = "plugin"
        entry["plugin_name"] = _managed_plugin_name(entry)
        entry["runtime_preset_uuid"] = None
    else:
        key = (str(entry.get("preset_id") or ""), str(entry.get("file_hash") or ""))
        state = registered.get(key)
        plugin_name = _managed_plugin_name(entry)
        plugin_uuid = str((state or {}).get("plugin_uuid") or "")
        state_matches = bool(
            state is not None
            and state.get("plugin_name") == plugin_name
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", plugin_uuid)
        )
        entry["registration_status"] = (
            "registered" if state_matches else "awaiting_registration"
        )
        entry["preset_scope"] = str((state or {}).get("scope") or "plugin")
        entry["plugin_name"] = plugin_name
        entry["runtime_preset_uuid"] = plugin_uuid if state_matches else None
    entry["supports_amount"] = runtime_supports_amount(entry)
    entry["ai_eligible"] = _can_enter_ai_pool(entry)


def _lua_string(value: str) -> str:
    parts = ['"']
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            parts.append("\\\\")
        elif character == '"':
            parts.append('\\"')
        elif character == "\n":
            parts.append("\\n")
        elif character == "\r":
            parts.append("\\r")
        elif character == "\t":
            parts.append("\\t")
        elif codepoint < 32 or codepoint == 127:
            # Exactly three decimal digits prevent the escape from consuming a
            # following numeric character under Lua 5.1's lexer.
            parts.append(f"\\{codepoint:03d}")
        else:
            parts.append(character)
    parts.append('"')
    return "".join(parts)


def _lua_literal(value: Any, *, indent: int = 0) -> str:
    """Serialize JSON-shaped data as deterministic, executable Lua data only."""

    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Lua registry numbers must be finite")
        return repr(value)
    if isinstance(value, str):
        return _lua_string(value)
    next_indent = indent + 2
    padding = " " * next_indent
    closing = " " * indent
    if isinstance(value, list):
        if not value:
            return "{}"
        items = [
            f"{padding}{_lua_literal(item, indent=next_indent)}," for item in value
        ]
        return "{\n" + "\n".join(items) + f"\n{closing}}}"
    if isinstance(value, dict):
        if not value:
            return "{}"
        items: list[str] = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("Lua registry object keys must be strings")
            rendered_key = (
                key
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                else f"[{_lua_string(key)}]"
            )
            rendered_value = _lua_literal(value[key], indent=next_indent)
            items.append(f"{padding}{rendered_key} = {rendered_value},")
        return "{\n" + "\n".join(items) + f"\n{closing}}}"
    raise TypeError(f"Unsupported Lua registry value: {type(value).__name__}")


def _write_managed_registry(
    data_dir: Path,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Publish the exact managed XMP set the Lightroom plugin should register."""

    registry_entries = [
        {
            "preset_id": item.get("preset_id"),
            "plugin_preset_id": item.get("uuid"),
            "plugin_name": _managed_plugin_name(item),
            "uuid": item.get("uuid"),
            "source_uuid_standard": bool(item.get("uuid_standard")),
            "name": item.get("name"),
            "path": item.get("path"),
            "relative_path": item.get("relative_path"),
            "file_hash": item.get("file_hash"),
            "source": item.get("source"),
            "source_id": item.get("source_id"),
            "source_commit": item.get("source_commit"),
            "license": item.get("license"),
            "supports_amount": bool(item.get("supports_amount")),
            "adaptive": bool(item.get("adaptive")),
            "black_and_white": bool(item.get("black_and_white")),
            "utility": bool(item.get("utility")),
            "target_scope": "plugin",
            "develop_settings": item.get("develop_settings"),
        }
        for item in entries
        if item.get("source_kind") == "managed"
        and str(item.get("source_tier") or "").casefold() != "local_only_proprietary"
        and item.get("preset_type") == "Normal"
        and item.get("compatibility") != "invalid"
        and item.get("plugin_registration_supported")
        and isinstance(item.get("develop_settings"), dict)
        and not item.get("hidden")
        and not item.get("duplicate_of")
    ]
    registry_entries.sort(
        key=lambda item: (
            str(item.get("source_id") or "").casefold(),
            str(item.get("name") or "").casefold(),
            str(item.get("preset_id") or ""),
        )
    )
    encoded = json.dumps(
        registry_entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        "schema_version": STYLE_REGISTRY_SCHEMA_VERSION,
        "generated_at": _now(),
        "registry_hash": hashlib.sha256(encoded).hexdigest(),
        "registration_status": "awaiting_lightroom_plugin",
        "entries": registry_entries,
        "summary": {
            "total": len(registry_entries),
            "with_uuid": sum(bool(item.get("uuid")) for item in registry_entries),
        },
    }
    write_json(managed_registry_path(data_dir), payload)
    atomic_write_text(
        managed_registry_lua_path(data_dir),
        "return " + _lua_literal(payload) + "\n",
    )
    return payload


def _write_package_checksums(root: Path) -> int:
    """Hash every packaged resource and metadata file except the checksum itself."""

    checksum_path = root / "PACKAGE-SHA256SUMS"
    lines: list[str] = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file() and item != checksum_path),
        key=lambda item: item.relative_to(root).as_posix().casefold(),
    ):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{_sha256(path)}  {relative}")
    atomic_write_text(checksum_path, "\n".join(lines) + "\n")
    return len(lines)


def sync_style_library(
    data_dir: Path,
    *,
    managed_root: Path | None = None,
    adobe_roots: Iterable[Path] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Incrementally index managed resources and installed Lightroom presets.

    Installed Adobe/user presets are referenced in place and never copied.  An
    unchanged file hash reuses parsed metadata from the previous index; only new
    or changed XMP files are reparsed.
    """

    data_dir = Path(data_dir).resolve()
    managed_root = (
        Path(managed_root).resolve() if managed_root else managed_style_root(data_dir)
    )
    managed_root.mkdir(parents=True, exist_ok=True)
    settings_path = managed_root / "settings.json"
    try:
        settings = read_json(settings_path) if settings_path.is_file() else {}
    except (OSError, TypeError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    include_lightroom_presets = bool(settings.get("include_lightroom_presets", True))
    include_user_uploads = bool(settings.get("include_user_uploads", True))
    include_adobe_local_copy = bool(settings.get("include_adobe_local_copy", True))
    two_source_mode = settings.get("source_mode") == "lightroom_user_only"
    hidden_resource_ids = {
        str(value)
        for value in settings.get("hidden_resource_ids", [])
        if isinstance(value, str) and value.startswith("xmp-")
    }
    _read_source_manifest.cache_clear()
    prior = load_style_index(data_dir)
    reusable_items = list(prior.get("entries", [])) + list(prior.get("_scan_cache", []))
    reusable = {
        str(item.get("locator")): item
        for item in reusable_items
        if isinstance(item, dict) and item.get("locator") and item.get("file_hash")
    }
    validation_path = managed_root / "validation.json"
    validation = read_json(validation_path) if validation_path.is_file() else {}
    if not isinstance(validation, dict):
        validation = {}
    registered = _load_registration_state(managed_root)

    sources: list[tuple[str, Path, dict[str, Any]]] = []
    for path in _iter_xmp(managed_root):
        metadata = _source_for_managed(path, managed_root)
        source_id = str(metadata.get("source_id") or "").casefold()
        if two_source_mode and source_id != "user-upload":
            continue
        if (
            not two_source_mode
            and not include_adobe_local_copy
            and source_id == "adobe-local-copy"
        ):
            continue
        sources.append(("managed", path, metadata))
    installed_roots = (
        discover_adobe_settings_roots()
        if two_source_mode or include_lightroom_presets
        else []
    ) if adobe_roots is None else _explicit_roots(adobe_roots)
    for root in installed_roots:
        lowered_root = str(root).replace("/", "\\").casefold()
        is_adobe = bool(
            ("resources" in lowered_root and "lightroom" in lowered_root)
            or "\\programdata\\adobe\\cameraraw\\settings" in lowered_root
        )
        metadata = {
            "source": "Adobe Lightroom installed"
            if is_adobe
            else "Lightroom user preset",
            "license": "installed-reference",
            "source_url": None,
            "experimental": False,
        }
        for path in _iter_xmp(root):
            sources.append(
                ("adobe-installed" if is_adobe else "user-installed", path, metadata)
            )

    entries: list[dict[str, Any]] = []
    reused = 0
    parsed = 0
    skipped_non_normal = 0
    scan_cache: list[dict[str, Any]] = []
    for index, (kind, path, metadata) in enumerate(sources, start=1):
        root = (
            managed_root
            if kind == "managed"
            else next(
                (
                    candidate
                    for candidate in installed_roots
                    if path.is_relative_to(candidate)
                ),
                path.parent,
            )
        )
        locator = f"{kind}:{os.path.normcase(str(path.resolve()))}"
        digest = _sha256(path)
        cached = reusable.get(locator)
        if (
            cached
            and cached.get("file_hash") == digest
            and cached.get("parser_version") == STYLE_PRESET_PARSER_VERSION
        ):
            entry = dict(cached)
            stat = path.stat()
            entry.update(metadata)
            entry.update(
                file_size=stat.st_size,
                file_mtime_ns=stat.st_mtime_ns,
                path=str(path.resolve()),
            )
            reused += 1
        else:
            entry = inspect_xmp_preset(
                path,
                source_kind=kind,
                source_metadata=metadata,
                source_root=root,
            )
            parsed += 1
        parser_hidden = bool(
            entry.get(
                "parser_hidden",
                bool(entry.get("hidden")) and not bool(entry.get("user_hidden")),
            )
        )
        resource_id = style_resource_id(entry)
        entry["resource_id"] = resource_id
        entry["parser_hidden"] = parser_hidden
        entry["user_hidden"] = resource_id in hidden_resource_ids
        source_enabled = (
            include_user_uploads
            if kind == "managed"
            and str(metadata.get("source_id") or "").casefold() == "user-upload"
            else include_lightroom_presets
            if kind in {"adobe-installed", "user-installed"}
            else True
        )
        entry["source_disabled"] = not source_enabled
        entry["hidden"] = (
            parser_hidden or bool(entry["user_hidden"]) or not source_enabled
        )
        preset_type_key = str(entry.get("preset_type") or "").casefold()
        indexable_installed_style = bool(
            preset_type_key == "normal"
            or (preset_type_key == "look" and entry.get("generic_creative_profile"))
        )
        if kind != "managed" and not indexable_installed_style:
            skipped_non_normal += 1
            scan_cache.append(entry)
            if progress:
                progress(
                    {
                        "phase": "index",
                        "current": index,
                        "total": len(sources),
                        "path": str(path),
                    }
                )
            continue
        _apply_validation(entry, validation)
        _apply_registration(entry, registered)
        entries.append(entry)
        if progress:
            progress(
                {
                    "phase": "index",
                    "current": index,
                    "total": len(sources),
                    "path": str(path),
                }
            )

    # Preserve discoverability while de-duplicating the default AI candidate pool.
    # UUID is the Lightroom identity; recipe_hash catches byte-different copies of
    # the exact same develop recipe.  Components are resolved together so an
    # ineligible utility file cannot accidentally suppress an equivalent, usable
    # full preset solely because its path sorts first.
    parent = list(range(len(entries)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    identities: dict[tuple[str, str], int] = {}
    for index, entry in enumerate(entries):
        for kind, value in (
            ("uuid", str(entry.get("uuid") or "").casefold()),
            ("recipe", str(entry.get("recipe_hash") or "")),
        ):
            if not value:
                continue
            key = (kind, value)
            if key in identities:
                union(index, identities[key])
            else:
                identities[key] = index

    components: dict[int, list[int]] = {}
    for index in range(len(entries)):
        components.setdefault(find(index), []).append(index)

    def canonical_priority(index: int) -> tuple[Any, ...]:
        entry = entries[index]
        return (
            not bool(entry.get("ai_eligible")),
            entry.get("compatibility") != "compatible",
            entry.get("source_kind") != "adobe-installed",
            str(entry.get("name") or "").casefold(),
            str(entry.get("path") or "").casefold(),
        )

    for component in components.values():
        canonical_index = min(component, key=canonical_priority)
        canonical = entries[canonical_index]
        canonical.pop("duplicate_of", None)
        canonical.pop("duplicate_of_locator", None)
        for index in component:
            if index == canonical_index:
                continue
            entries[index]["duplicate_of"] = canonical["preset_id"]
            entries[index]["duplicate_of_locator"] = canonical["locator"]
            entries[index]["ai_eligible"] = False

    for entry in entries:
        if entry.get("hidden"):
            entry["ai_eligible"] = False
            if (
                entry.get("source_kind") == "managed"
                and str(entry.get("source_tier") or "").casefold()
                != "local_only_proprietary"
            ):
                entry["registration_status"] = "filtered"
        elif entry.get("duplicate_of") and entry.get("source_kind") == "managed":
            entry["registration_status"] = "duplicate"
            entry["ai_eligible"] = False

    default_pool = [
        entry["preset_id"]
        for entry in entries
        if entry.get("ai_eligible") and not entry.get("duplicate_of")
    ]
    visible_pool = [
        entry["preset_id"]
        for entry in entries
        if not entry.get("hidden") and not entry.get("duplicate_of")
    ]
    managed_registry = _write_managed_registry(data_dir, entries)
    summary = Counter(str(item.get("compatibility") or "unknown") for item in entries)
    payload = {
        "schema_version": STYLE_LIBRARY_SCHEMA_VERSION,
        "generated_at": _now(),
        "managed_root": str(managed_root),
        "installed_roots": [str(path) for path in installed_roots],
        "entries": entries,
        "_scan_cache": scan_cache,
        "default_pool": default_pool,
        "visible_pool": visible_pool,
        "managed_registry": {
            "path": str(managed_registry_path(data_dir)),
            "lua_path": str(managed_registry_lua_path(data_dir)),
            "registry_hash": managed_registry["registry_hash"],
            "total": managed_registry["summary"]["total"],
        },
        "package_checksums": {
            "path": str(package_checksums_path(data_dir)),
            "excludes": ["PACKAGE-SHA256SUMS"],
        },
        "summary": {
            "total": len(entries),
            "managed": sum(item.get("source_kind") == "managed" for item in entries),
            "installed": sum(item.get("source_kind") != "managed" for item in entries),
            "creative_profiles": sum(
                item.get("look_kind") == "lightroom_profile" for item in entries
            ),
            "develop_presets": sum(
                item.get("look_kind") != "lightroom_profile" for item in entries
            ),
            "default_pool": len(default_pool),
            "parsed": parsed,
            "reused": reused,
            "skipped_non_normal": skipped_non_normal,
            "compatible": summary["compatible"],
            "unsupported": summary["unsupported"],
            "invalid": summary["invalid"],
            "hidden": sum(bool(item.get("hidden")) for item in entries),
            "awaiting_registration": sum(
                item.get("registration_status") == "awaiting_registration"
                for item in entries
            ),
            "registered_managed": sum(
                item.get("registration_status") == "registered" for item in entries
            ),
            "unsupported_registration": sum(
                item.get("registration_status") == "unsupported" for item in entries
            ),
            "filtered_registration": sum(
                item.get("registration_status") == "filtered" for item in entries
            ),
            "duplicate_registration": sum(
                item.get("registration_status") == "duplicate" for item in entries
            ),
            "local_copy": sum(
                item.get("registration_status") == "local_copy" for item in entries
            ),
        },
    }
    write_json(style_index_path(data_dir), payload)
    _write_package_checksums(managed_root)
    return payload
