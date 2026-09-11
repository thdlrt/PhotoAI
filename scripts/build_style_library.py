from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    title: str
    source_url: str
    branch: str
    commit: str
    license_spdx: str
    tier: str
    checkout_root: Path
    xmp_root: Path
    metadata_files: tuple[str, ...]
    excluded_paths: tuple[dict[str, str], ...] = ()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_package_checksums(output_root: Path) -> int:
    checksum_path = output_root / "PACKAGE-SHA256SUMS"
    lines = [
        f"{_file_sha256(path)}  {path.relative_to(output_root).as_posix()}"
        for path in sorted(
            (
                item
                for item in output_root.rglob("*")
                if item.is_file() and item != checksum_path
            ),
            key=lambda item: item.relative_to(output_root).as_posix().casefold(),
        )
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _validate_xmp(data: bytes) -> tuple[bool, str | None]:
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError) as exc:
        return False, f"XML parse error: {exc}"
    if not root.tag.endswith("xmpmeta"):
        return False, f"unexpected root element: {root.tag}"
    tags = {element.tag.rsplit("}", 1)[-1] for element in root.iter()}
    missing = sorted({"RDF", "Description"} - tags)
    if missing:
        return False, f"missing XMP structure: {', '.join(missing)}"
    return True, None


def _git_head(path: Path) -> str | None:
    if not (path / ".git").is_dir():
        return None
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _find_workflow_checkout(download_root: Path, commit: str) -> Path:
    archive_root = download_root / "lightroom-workflow-archive"
    candidates = sorted(archive_root.glob(f"lightroom-workflow-{commit}"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one extracted lightroom-workflow checkout for {commit}, found {len(candidates)}"
        )
    return candidates[0]


def _source_specs(project_root: Path) -> list[SourceSpec]:
    download_root = project_root / ".runtime" / "downloads" / "style-library-src"
    openfilm_root = download_root / "openfilmstocks"
    peva_root = download_root / "peva3-lightroom-presets"
    workflow_commit = "217d8eebf057ed71174b053f604b80e1dff481d8"
    workflow_root = _find_workflow_checkout(download_root, workflow_commit)
    return [
        SourceSpec(
            source_id="openfilmstocks",
            title="OpenFilmStocks",
            source_url="https://github.com/eliseomartelli/OpenFilmStocks",
            branch="master",
            commit="fb0862514e3379ce3a38ccef422d83404ce14afb",
            license_spdx="MIT",
            tier="curated_candidate",
            checkout_root=openfilm_root,
            xmp_root=openfilm_root / "OpenFilmStocks",
            metadata_files=("LICENSE",),
        ),
        SourceSpec(
            source_id="lightroom-workflow",
            title="lightroom-workflow",
            source_url="https://github.com/thejoltjoker/lightroom-workflow",
            branch="master",
            commit=workflow_commit,
            license_spdx="MPL-2.0",
            tier="curated_candidate",
            checkout_root=workflow_root,
            xmp_root=workflow_root / "Settings",
            metadata_files=("LICENSE", "README.md"),
            excluded_paths=(
                {
                    "path": "Lightroom/Develop Presets",
                    "reason": "legacy .lrtemplate files are outside the XMP-only library",
                },
            ),
        ),
        SourceSpec(
            source_id="peva3-lightroom-presets",
            title="Lightroom-Presets",
            source_url="https://github.com/peva3/Lightroom-Presets",
            branch="main",
            commit="f22f4d8057aaaada6df3f0ed7fbe6b952a30db77",
            license_spdx="MIT",
            tier="community_experimental",
            checkout_root=peva_root,
            xmp_root=peva_root / "Presets",
            metadata_files=("LICENSE", "README.md", "CHANGELOG.md"),
            excluded_paths=(
                {
                    "path": "research",
                    "reason": "research artifacts are not release presets and include incomplete XMP files",
                },
            ),
        ),
    ]


def _verify_sources(sources: list[SourceSpec]) -> None:
    for source in sources:
        if not source.xmp_root.is_dir():
            raise RuntimeError(f"Missing XMP root: {source.xmp_root}")
        actual_head = _git_head(source.checkout_root)
        if actual_head is not None and actual_head != source.commit:
            raise RuntimeError(
                f"{source.source_id} checkout is {actual_head}, expected pinned commit {source.commit}"
            )
        for metadata_file in source.metadata_files:
            if not (source.checkout_root / metadata_file).is_file():
                raise RuntimeError(f"Missing {source.source_id} metadata file: {metadata_file}")


def build_library(project_root: Path, output_root: Path) -> dict[str, Any]:
    sources = _source_specs(project_root)
    _verify_sources(sources)
    output_root.mkdir(parents=True, exist_ok=False)

    seen_hashes: dict[str, str] = {}
    duplicate_groups: dict[str, list[str]] = {}
    source_manifests: list[dict[str, Any]] = []
    all_files: list[dict[str, Any]] = []
    invalid_files: list[dict[str, str]] = []

    for source in sources:
        destination = output_root / "sources" / source.source_id
        xmp_destination = destination / "xmp"
        xmp_destination.mkdir(parents=True)

        copied_metadata: list[str] = []
        for metadata_file in source.metadata_files:
            source_path = source.checkout_root / metadata_file
            destination_name = (
                "README.upstream.md" if metadata_file == "README.md" else metadata_file
            )
            shutil.copy2(source_path, destination / destination_name)
            copied_metadata.append(destination_name)

        source_files: list[dict[str, Any]] = []
        copied_count = 0
        duplicate_count = 0
        valid_count = 0
        total_bytes = 0
        copied_bytes = 0
        for path in sorted(source.xmp_root.rglob("*.xmp")):
            relative = path.relative_to(source.xmp_root).as_posix()
            data = path.read_bytes()
            digest = _sha256(data)
            total_bytes += len(data)
            valid, error = _validate_xmp(data)
            source_key = f"{source.source_id}:{relative}"
            record: dict[str, Any] = {
                "source_relative_path": relative,
                "sha256": digest,
                "bytes": len(data),
                "xml_xmp_valid": valid,
            }
            if error:
                record["validation_error"] = error
                invalid_files.append({"file": source_key, "error": error})
            else:
                valid_count += 1
                canonical = seen_hashes.get(digest)
                if canonical is not None:
                    record["duplicate_of"] = canonical
                    record["library_relative_path"] = None
                    duplicate_count += 1
                    duplicate_groups.setdefault(digest, [canonical]).append(source_key)
                else:
                    library_relative = (
                        Path("sources") / source.source_id / "xmp" / relative
                    ).as_posix()
                    target = output_root / library_relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    record["library_relative_path"] = library_relative
                    seen_hashes[digest] = source_key
                    copied_count += 1
                    copied_bytes += len(data)
            source_files.append(record)
            all_files.append({"source_id": source.source_id, **record})

        summary = {
            "discovered_xmp": len(source_files),
            "valid_xmp": valid_count,
            "invalid_xmp": len(source_files) - valid_count,
            "unique_xmp_copied": copied_count,
            "exact_duplicates_omitted": duplicate_count,
            "source_bytes": total_bytes,
            "copied_bytes": copied_bytes,
        }
        source_manifest = {
            "id": source.source_id,
            "title": source.title,
            "tier": source.tier,
            "source_url": source.source_url,
            "branch": source.branch,
            "commit": source.commit,
            "license_spdx": source.license_spdx,
            "metadata_files": copied_metadata,
            "selected_source_path": source.xmp_root.relative_to(source.checkout_root).as_posix(),
            "excluded_paths": list(source.excluded_paths),
            "summary": summary,
            "files": source_files,
        }
        (destination / "SOURCE.json").write_text(
            json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        source_manifests.append(source_manifest)

    totals = {
        "discovered_xmp": len(all_files),
        "valid_xmp": sum(item["xml_xmp_valid"] for item in all_files),
        "invalid_xmp": len(invalid_files),
        "unique_xmp_copied": len(seen_hashes),
        "exact_duplicates_omitted": len(all_files) - len(seen_hashes) - len(invalid_files),
        "exact_duplicate_groups": len(duplicate_groups),
    }
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "validation_semantics": {
            "valid_xmp": "XML/XMP structural validity only; not Lightroom runtime compatibility",
            "lightroom_compatibility_source": "index.json after style-library sync and Lightroom calibration",
        },
        "deduplication": {
            "method": "exact SHA-256 of original file bytes",
            "scope": "all selected sources",
            "original_bytes_preserved": True,
        },
        "adobe_resources": {
            "copied": False,
            "policy": "Added only by the second, machine-local import phase and never redistributable.",
        },
        "release_boundary": {
            "redistributable_manifest": "redistributable-manifest.json",
            "always_exclude": [
                "sources/adobe-local-copy/**",
                "adobe-local-copy.json",
            ],
        },
        "totals": totals,
        "sources": source_manifests,
        "duplicate_groups": [
            {"sha256": digest, "files": files}
            for digest, files in sorted(duplicate_groups.items())
        ],
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_root / "validation-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "validation": (
                    "XML parse + x:xmpmeta root + RDF/Description presence only; "
                    "this is not Lightroom semantic/runtime validation"
                ),
                "totals": totals,
                "invalid_files": invalid_files,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    checksum_lines = [
        f"{item['sha256']}  {item['library_relative_path']}"
        for item in all_files
        if item.get("library_relative_path")
    ]
    (output_root / "SHA256SUMS").write_text(
        "\n".join(sorted(checksum_lines)) + "\n", encoding="utf-8"
    )
    (output_root / "README.md").write_text(
        "# Style library source bundle\n\n"
        "Original upstream XMP bytes are stored under `sources/<id>/xmp`. "
        "See `manifest.json` for source URLs, pinned commits, licenses, validation, "
        "SHA-256 values, and exact-duplicate mappings. `valid_xmp` means XML/XMP "
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
        "Only files listed by `redistributable-manifest.json` may be published. "
        "The Adobe local copy is private to this machine and is always excluded.\n",
        encoding="utf-8",
    )
    redistributable_files = [
        {
            "path": path.relative_to(output_root).as_posix(),
            "sha256": _file_sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(
            (item for item in (output_root / "sources").rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(output_root).as_posix().casefold(),
        )
    ]
    (output_root / "redistributable-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": manifest["generated_at"],
                "policy": "Allow-list only; no unlisted file may enter a redistributable package.",
                "included_source_ids": [source.source_id for source in sources],
                "always_exclude": [
                    "sources/adobe-local-copy/**",
                    "adobe-local-copy.json",
                    "index.json",
                    "managed-preset-registry.json",
                    "managed-preset-registry.lua",
                    "managed-preset-registration.json",
                ],
                "files": redistributable_files,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_package_checksums(output_root)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the pinned third-party XMP style library")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    output = (
        args.output.resolve()
        if args.output
        else project_root / ".runtime" / "data" / "style-library"
    )
    manifest = build_library(project_root, output)
    print(json.dumps({"output": str(output), "totals": manifest["totals"]}, indent=2))


if __name__ == "__main__":
    main()
