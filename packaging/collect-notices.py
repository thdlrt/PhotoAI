"""Collect release license texts from the reviewed local build dependencies."""
from __future__ import annotations

import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import urllib.parse
import zipfile

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "licenses"


def main() -> None:
    inventory = []
    missing = []

    def record(name, version, license_name, candidates, section):
        folder = DEST / section / f"{name}-{version}"
        copied = []
        for source in candidates:
            source = Path(source)
            if not source.is_file():
                continue
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / source.name
            if target.exists() and target.read_bytes() != source.read_bytes():
                target = folder / (source.parent.name + "-" + source.name)
            shutil.copyfile(source, target)
            copied.append(target.relative_to(ROOT).as_posix())
        inventory.append({"name": name, "version": version, "license": license_name,
                          "notices": sorted(set(copied))})
        if not copied:
            missing.append(f"{section}/{name}-{version}")

    package_map = metadata.packages_distributions()
    installed = set()
    for name in ("PhotoAI.Service", "PhotoAI.CoreWorker"):
        exe = ROOT / "packaging/dist/core" / name / f"{name}.exe"
        archive = CArchiveReader(str(exe)).open_embedded_archive("PYZ.pyz")
        for module in archive.toc:
            installed.update(package_map.get(module.split(".", 1)[0], []))
    installed.add("pyinstaller")
    for name in sorted(installed):
        if name.lower().replace("_", "-") == "landscape-ai-culler":
            continue
        dist = metadata.distribution(name)
        files = [dist.locate_file(file) for file in dist.files or []
                 if any(part.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))
                        for part in file.parts)]
        record(dist.metadata["Name"], dist.version,
               dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See license text",
               files, "python")
    record("Python", ".".join(map(str, sys.version_info[:3])), "PSF-2.0 and included notices",
           list(Path(sys.base_prefix).glob("LICENSE*")), "python")

    completed = subprocess.run([
        "cargo", "metadata", "--locked", "--offline", "--format-version", "1",
        "--filter-platform", "x86_64-pc-windows-msvc", "--manifest-path",
        str(ROOT / "desktop/src-tauri/Cargo.toml"),
    ], capture_output=True, encoding="utf-8", check=True, timeout=60)
    cargo = json.loads(completed.stdout)
    for package in cargo["packages"]:
        if package["name"] == "photoai-desktop":
            continue
        folder = Path(package["manifest_path"]).parent
        files = [path for pattern in ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*")
                 for path in folder.glob(pattern) if path.is_file()]
        if package.get("license_file"):
            files.append(folder / package["license_file"])
        vcs_file = folder / ".cargo_vcs_info.json"
        vcs = json.loads(vcs_file.read_text(encoding="utf-8")) if vcs_file.is_file() else {}
        revision = vcs.get("git", {}).get("sha1")
        repository = (package.get("repository") or "").rstrip("/").removesuffix(".git")
        if not files and repository.startswith("https://github.com/") and revision:
            repo = repository.removeprefix("https://github.com/")
            cache = ROOT / ".runtime/publish/license-cache" / (repo.replace("/", "-") + "-" + revision)
            cache.mkdir(parents=True, exist_ok=True)
            files = list(cache.glob("*"))
            if not files:
                request = urllib.request.Request(
                    f"https://api.github.com/repos/{repo}/contents/?ref={revision}",
                    headers={"User-Agent": "PhotoAI-license-collector"},
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    entries = json.load(response)
                for entry in entries:
                    if entry["type"] == "file" and entry["name"].upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
                        with urllib.request.urlopen(entry["download_url"], timeout=30) as response:
                            target = cache / entry["name"]
                            target.write_bytes(response.read())
                            files.append(target)
        if not files and package.get("license") == "MPL-2.0":
            # selectors has only the standard MPL header in its source files.
            # cssparser ships the identical, unmodified standard MPL 2.0 text.
            standard_mpl = DEST / "rust/cssparser-0.36.0/LICENSE"
            if standard_mpl.is_file():
                files.append(standard_mpl)
        record(package["name"], package["version"], package.get("license"), files, "rust")
        inventory[-1].update(upstream=repository, source_revision=revision,
                             source_archive=f"https://crates.io/api/v1/crates/{package['name']}/{package['version']}/download")

    urls = {
        "uv/LICENSE-MIT": "https://raw.githubusercontent.com/astral-sh/uv/0.11.2/LICENSE-MIT",
        "uv/LICENSE-APACHE": "https://raw.githubusercontent.com/astral-sh/uv/0.11.2/LICENSE-APACHE",
        "wheels/openai-clip-LICENSE": "https://raw.githubusercontent.com/openai/CLIP/main/LICENSE",
    }
    for relative, url in urls.items():
        target = DEST / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=30) as response:
            target.write_bytes(response.read())
    wheel = ROOT / "packaging/resources/wheels/pyvips-3.2.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        text = archive.read("pyvips-3.2.0.dist-info/licenses/LICENSE.txt")
    (DEST / "wheels/pyvips-LICENSE.txt").write_bytes(text)
    exif = ROOT / "packaging/vendor/exiftool-13.59/exiftool-13.59_64"
    record("ExifTool", "13.59", "Artistic-1.0-Perl OR GPL-1.0-or-later; runtime notices included",
           [exif / "README.txt", exif / "exiftool_files/LICENSE",
            exif / "exiftool_files/Licenses_Strawberry_Perl.zip",
            exif / "exiftool_files/windows_exiftool.txt"], "exiftool")
    inventory.extend([
        {"name": "uv", "version": "0.11.2", "license": "MIT OR Apache-2.0",
         "notices": ["licenses/uv/LICENSE-MIT", "licenses/uv/LICENSE-APACHE"]},
        {"name": "openai-clip", "version": "1.0.1", "license": "MIT", "notices": ["licenses/wheels/openai-clip-LICENSE"]},
        {"name": "pyvips", "version": "3.2.0", "license": "MIT", "notices": ["licenses/wheels/pyvips-LICENSE.txt"]},
    ])
    (DEST / "inventory.json").write_text(json.dumps({"components": inventory}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"components": len(inventory), "missing_notice_files": missing}))


if __name__ == "__main__":
    main()
