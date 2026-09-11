from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_root_name(path: Path) -> str:
    value = str(path.resolve()).replace(":", "").replace("\\", "-").replace("/", "-")
    cleaned = "-".join(part for part in value.split("-") if part)
    digest = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()[:10]
    return f"{cleaned[-80:]}-{digest}" if cleaned else digest


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return payload


def _write_package_checksums(library_root: Path) -> int:
    checksum_path = library_root / "PACKAGE-SHA256SUMS"
    lines = [
        f"{_sha256(path)}  {path.relative_to(library_root).as_posix()}"
        for path in sorted(
            (
                item
                for item in library_root.rglob("*")
                if item.is_file() and item != checksum_path
            ),
            key=lambda item: item.relative_to(library_root).as_posix().casefold(),
        )
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _write_redistributable_manifest(library_root: Path, generated_at: str) -> None:
    source_root = library_root / "sources"
    allowed_roots = [
        path
        for path in source_root.iterdir()
        if path.is_dir() and path.name != "adobe-local-copy"
    ]
    files = [
        {
            "path": path.relative_to(library_root).as_posix(),
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        }
        for root in allowed_roots
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(library_root).as_posix().casefold(),
        )
    ]
    source_ids = []
    for root in allowed_roots:
        source_manifest = root / "SOURCE.json"
        if source_manifest.is_file():
            source_ids.append(str(_read_json(source_manifest).get("id") or root.name))
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "policy": "Allow-list only; no unlisted file may enter a redistributable package.",
        "included_source_ids": sorted(source_ids),
        "always_exclude": [
            "sources/adobe-local-copy/**",
            "adobe-local-copy.json",
            "index.json",
            "managed-preset-registry.json",
            "managed-preset-registry.lua",
            "managed-preset-registration.json",
        ],
        "files": sorted(files, key=lambda item: item["path"].casefold()),
    }
    (library_root / "redistributable-manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def import_local_adobe_presets(data_dir: Path) -> dict[str, Any]:
    """Copy indexed Adobe presets and generic Creative Profiles to E:.

    The copied files are explicitly machine-local proprietary resources.  They
    are not re-licensed and must not be included in a redistributable build.
    The Lightroom installation is read-only and every copied byte is verified.
    """

    data_dir = data_dir.resolve()
    if os.name == "nt" and data_dir.drive.casefold() != "e:":
        raise ValueError(f"Adobe preset resources must be stored on E:, got: {data_dir}")
    library_root = data_dir / "style-library"
    index_path = library_root / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"Style index is missing: {index_path}")
    index = _read_json(index_path)
    installed_roots = [Path(value).resolve() for value in index.get("installed_roots", [])]
    adobe_entries = [
        item
        for item in index.get("entries", [])
        if isinstance(item, dict)
        and item.get("source_kind") == "adobe-installed"
        and (
            str(item.get("preset_type") or "").casefold() == "normal"
            or (
                str(item.get("preset_type") or "").casefold() == "look"
                and item.get("generic_creative_profile") is True
            )
        )
    ]
    destination = library_root / "sources" / "adobe-local-copy"
    xmp_root = destination / "xmp"
    xmp_root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    used_targets: set[str] = set()
    for entry in sorted(adobe_entries, key=lambda item: str(item.get("path", "")).casefold()):
        source = Path(str(entry.get("path") or "")).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Indexed Adobe preset is missing: {source}")
        source_root = next((root for root in installed_roots if source.is_relative_to(root)), None)
        if source_root is None:
            raise ValueError(f"Adobe preset escaped the indexed roots: {source}")
        relative = source.relative_to(source_root)
        target_relative = Path(_safe_root_name(source_root)) / relative
        target = xmp_root / target_relative
        target_key = os.path.normcase(str(target.resolve()))
        if target_key in used_targets:
            raise ValueError(f"Adobe preset target collision: {target}")
        used_targets.add(target_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        source_hash = _sha256(source)
        if source_hash != str(entry.get("file_hash") or ""):
            raise ValueError(f"Adobe preset changed after indexing; sync the library first: {source}")
        copied = False
        if not target.is_file() or _sha256(target) != source_hash:
            shutil.copy2(source, target)
            copied = True
        if _sha256(target) != source_hash:
            raise OSError(f"Copied Adobe preset failed SHA-256 verification: {target}")
        records.append(
            {
                "preset_id": entry.get("preset_id"),
                "uuid": entry.get("uuid"),
                "name": entry.get("name"),
                "group": entry.get("group"),
                "preset_type": entry.get("preset_type"),
                "look_kind": entry.get("look_kind"),
                "profile_name": entry.get("profile_name"),
                "source_path": str(source),
                "source_root": str(source_root),
                "library_relative_path": str(target.relative_to(library_root)).replace("\\", "/"),
                "sha256": source_hash,
                "bytes": target.stat().st_size,
                "copied_now": copied,
            }
        )
    now = datetime.now(UTC).isoformat()
    normal_count = sum(
        str(record.get("preset_type") or "").casefold() == "normal" for record in records
    )
    creative_profile_count = sum(
        str(record.get("preset_type") or "").casefold() == "look" for record in records
    )
    source_manifest = {
        "schema_version": 1,
        "id": "adobe-local-copy",
        "title": "Adobe Lightroom installed presets (local copy)",
        "tier": "local_only_proprietary",
        "license_spdx": "LicenseRef-Adobe-Proprietary-Local-Install",
        "redistributable": False,
        "machine_local": True,
        "copied_from_existing_local_installation": True,
        "generated_at": now,
        "source_roots": sorted({record["source_root"] for record in records}),
        "summary": {
            "normal_presets": normal_count,
            "creative_profiles": creative_profile_count,
            "total_resources": len(records),
            "bytes": sum(int(record["bytes"]) for record in records),
            "copied_now": sum(bool(record["copied_now"]) for record in records),
        },
        "files": records,
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "SOURCE.json").write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (destination / "LOCAL-ONLY.txt").write_text(
        "These Adobe Lightroom preset files were copied from this machine's licensed local "
        "installation for private local use. They are not part of the redistributable open-source "
        "preset bundle and must not be published or packaged for another machine.\n",
        encoding="utf-8",
    )
    (destination / "SHA256SUMS").write_text(
        "".join(
            f"{record['sha256']}  {record['library_relative_path']}\n"
            for record in sorted(records, key=lambda item: item["library_relative_path"].casefold())
        ),
        encoding="utf-8",
    )
    summary = {
        "schema_version": 1,
        "generated_at": now,
        "resource_root": str(destination),
        "normal_presets": normal_count,
        "creative_profiles": creative_profile_count,
        "total_resources": len(records),
        "bytes": source_manifest["summary"]["bytes"],
        "copied_now": source_manifest["summary"]["copied_now"],
        "redistributable": False,
        "source_manifest": str(destination / "SOURCE.json"),
    }
    (library_root / "adobe-local-copy.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    bundle_manifest_path = library_root / "manifest.json"
    if bundle_manifest_path.is_file():
        bundle_manifest = _read_json(bundle_manifest_path)
        bundle_manifest["adobe_resources"] = {
            "copied": True,
            "scope": "Lightroom presets and generic Creative Profiles indexed from this machine",
            "normal_presets": normal_count,
            "creative_profiles": creative_profile_count,
            "bytes": source_manifest["summary"]["bytes"],
            "resource_root": str(destination.relative_to(library_root)).replace("\\", "/"),
            "source_manifest": str((destination / "SOURCE.json").relative_to(library_root)).replace("\\", "/"),
            "license": "LicenseRef-Adobe-Proprietary-Local-Install",
            "redistributable": False,
            "machine_local": True,
        }
        bundle_manifest["generated_at"] = now
        bundle_manifest["validation_semantics"] = {
            "valid_xmp": "XML/XMP structural validity only; not Lightroom runtime compatibility",
            "lightroom_compatibility_source": (
                "index.json after style-library sync and Lightroom calibration"
            ),
        }
        bundle_manifest["release_boundary"] = {
            "redistributable_manifest": "redistributable-manifest.json",
            "always_exclude": [
                "sources/adobe-local-copy/**",
                "adobe-local-copy.json",
            ],
        }
        open_unique = int((bundle_manifest.get("totals") or {}).get("unique_xmp_copied") or 0)
        bundle_manifest["local_resource_totals"] = {
            "open_source_unique_xmp": open_unique,
            "adobe_local_normal_xmp": normal_count,
            "adobe_local_creative_profile_xmp": creative_profile_count,
            "adobe_local_xmp": len(records),
            "all_local_xmp_resources": open_unique + len(records),
        }
        bundle_manifest_path.write_text(
            json.dumps(bundle_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    root_checksums = library_root / "SHA256SUMS"
    existing_lines = root_checksums.read_text(encoding="utf-8").splitlines() if root_checksums.is_file() else []
    kept_lines = [
        line
        for line in existing_lines
        if "  sources/adobe-local-copy/" not in line.replace("\\", "/")
    ]
    adobe_lines = [
        f"{record['sha256']}  {record['library_relative_path']}"
        for record in records
    ]
    root_checksums.write_text(
        "\n".join(sorted([*kept_lines, *adobe_lines], key=str.casefold)) + "\n",
        encoding="utf-8",
    )
    validation_report = library_root / "validation-report.json"
    if validation_report.is_file():
        validation_payload = _read_json(validation_report)
        validation_payload["validation"] = (
            "XML parse + x:xmpmeta root + RDF/Description presence only; "
            "this is not Lightroom semantic/runtime validation"
        )
        validation_report.write_text(
            json.dumps(validation_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    _write_redistributable_manifest(library_root, now)
    readme_path = library_root / "README.md"
    readme_path.write_text(
        "# Style library source bundle\n\n"
        "Redistributable upstream XMP bytes are stored under `sources/<id>/xmp`; "
        "their pinned commits and licenses are recorded in `manifest.json`. This machine's "
        "Adobe Lightroom Normal presets and generic Creative Profiles are copied under "
        "`sources/adobe-local-copy/xmp` "
        "and are explicitly marked local-only and non-redistributable. `SHA256SUMS` covers "
        "all local XMP resources; `PACKAGE-SHA256SUMS` also covers metadata. No source file "
        "in the Lightroom installation is modified or removed. `valid_xmp` means XML/XMP "
        "structure only, not proven Lightroom compatibility.\n\n"
        "Rebuild on this E: installation (the target must not already exist):\n\n"
        "```powershell\n"
        "$env:PYTHONPATH = 'src'\n"
        ".venv\\Scripts\\python.exe scripts\\build_style_library.py\n"
        ".venv\\Scripts\\python.exe -c \"from pathlib import Path; from "
        "landscape_culler.style_library import sync_style_library; "
        "sync_style_library(Path(r'.runtime/data'))\"\n"
        ".venv\\Scripts\\python.exe scripts\\import_local_adobe_presets.py "
        "--data-dir .runtime\\data\n"
        ".venv\\Scripts\\python.exe -c \"from pathlib import Path; from "
        "landscape_culler.style_library import sync_style_library; "
        "sync_style_library(Path(r'.runtime/data'))\"\n"
        "```\n\n"
        "Publishing is allow-list only: package exactly the files named by "
        "`redistributable-manifest.json`. Never publish `sources/adobe-local-copy`.\n",
        encoding="utf-8",
    )
    _write_package_checksums(library_root)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy locally installed Adobe Lightroom presets and Creative Profiles into the E: resource bundle."
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    result = import_local_adobe_presets(args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
