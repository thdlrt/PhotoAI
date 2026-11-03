# -*- mode: python ; coding: utf-8 -*-
"""Build lightweight Service, CLI and CoreWorker executables.

AI packages stay in the managed environment. The Service also needs Pillow
and NumPy for image review and timelapse frames; CoreWorker remains dependency-light.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).parent
package_root = project_root / "src"
service_version_file = project_root / "packaging" / "version_info.txt"
worker_version_file = project_root / "packaging" / "version_info_worker.txt"

common_datas = [
    (
        str(package_root / "landscape_culler" / "static"),
        "landscape_culler/static",
    ),
    (
        str(package_root / "landscape_culler" / "templates"),
        "landscape_culler/templates",
    ),
]

# Uvicorn chooses protocol implementations at runtime. Collect only its
# lightweight implementation modules; never recursively collect the PhotoAI
# package or any optional AI dependency.
common_hiddenimports = sorted(
    set(
        collect_submodules("uvicorn")
        + [
            "jinja2",
            "pydantic",
            "pydantic_core",
            "starlette",
        ]
    )
)

core_excludes = [
    "IPython",
    "OpenColorIO",
    "PIL",
    "PyOpenColorIO",
    "cv2",
    "huggingface_hub",
    "jupyter",
    "jupyterlab",
    "matplotlib",
    "numpy",
    "ollama",
    "open_clip",
    "pandas",
    "pyarrow",
    "pyiqa",
    "pytest",
    "pyvips",
    "rawpy",
    "safetensors",
    "scipy",
    "sklearn",
    "timm",
    "tokenizers",
    "torch",
    "torchaudio",
    "torchvision",
    "transformers",
]


def core_analysis(entrypoint, extra_hiddenimports=None, preview_dependencies=False):
    return Analysis(
        [str(project_root / "packaging" / entrypoint)],
        pathex=[str(package_root)],
        binaries=[],
        datas=list(common_datas),
        hiddenimports=list(common_hiddenimports) + list(extra_hiddenimports or []),
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[name for name in core_excludes
                  if not (preview_dependencies and name in {"PIL", "numpy"})],
        noarchive=False,
        optimize=1,
    )


service_analysis = core_analysis(
    "service_entry.py",
    [
        "PIL.Image", "PIL.ImageEnhance", "PIL.ImageOps", "numpy",
        "landscape_culler.timelapse",
        "landscape_culler.timelapse_api",
        "landscape_culler.framepick",
        "landscape_culler._vendor.lrtlite.analysis",
        "landscape_culler._vendor.lrtlite.sequence",
        "landscape_culler._vendor.lrtlite.exif",
        "landscape_culler._vendor.lrtlite.xmp",
        "landscape_culler.collaborative_review",
        "landscape_culler.collaborative_review_api",
        "landscape_culler.collaborative_review_vision",
        "landscape_culler.collaborative_review_proxy",
        "landscape_culler.selection_export",
        "landscape_culler.toolbox_cli",
        "landscape_culler.toolbox_legacy_schemas",
        "landscape_culler.agent_connection",
    ],
    preview_dependencies=True,
)
service_pyz = PYZ(service_analysis.pure)
service_exe = EXE(
    service_pyz,
    service_analysis.scripts,
    [],
    exclude_binaries=True,
    name="PhotoAI.Service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version=str(service_version_file),
)
# The CLI uses the same PYZ and _internal directory as the service. Replace
# only the application entry script; keep PyInstaller's required runtime hooks.
# This avoids a second Python/Pillow/NumPy runtime while remaining a real
# console executable for machines that do not have Python installed.
cli_scripts = [
    ("toolbox_cli_entry", str(project_root / "packaging" / "toolbox_cli_entry.py"), kind)
    if name == "service_entry" else (name, path, kind)
    for name, path, kind in service_analysis.scripts
]
if not any(name == "toolbox_cli_entry" for name, _path, _kind in cli_scripts):
    raise RuntimeError("The CLI application entrypoint could not replace service_entry")
cli_exe = EXE(
    service_pyz,
    cli_scripts,
    [],
    exclude_binaries=True,
    name="PhotoToolbox.CLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version=str(project_root / "packaging" / "version_info_cli.txt"),
)
service_collect = COLLECT(
    service_exe,
    cli_exe,
    service_analysis.binaries,
    service_analysis.datas,
    strip=False,
    upx=False,
    name="PhotoAI.Service",
)

worker_analysis = core_analysis(
    "core_worker_entry.py",
    [
        # CLI commands use importlib, so PyInstaller cannot discover these.
        # Keep all non-AI commands here, not only the environment installer.
        "landscape_culler.xmp_cleanup",
        "landscape_culler.toolbox",
        "landscape_culler.xmp",
        "landscape_culler.lightroom_apply",
        "landscape_culler.lightroom_export",
        "landscape_culler.ai_runtime",
        "landscape_culler.model_resources",
        "landscape_culler.offline_bundle",
    ],
)
worker_pyz = PYZ(worker_analysis.pure)
worker_exe = EXE(
    worker_pyz,
    worker_analysis.scripts,
    [],
    exclude_binaries=True,
    name="PhotoAI.CoreWorker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version=str(worker_version_file),
)
worker_collect = COLLECT(
    worker_exe,
    worker_analysis.binaries,
    worker_analysis.datas,
    strip=False,
    upx=False,
    name="PhotoAI.CoreWorker",
)
