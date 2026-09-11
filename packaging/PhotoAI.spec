# -*- mode: python ; coding: utf-8 -*-
"""Build the two lightweight console-enabled PhotoAI sidecars.

This spec is deliberately a *core* build. AI/image packages are installed
later into the user-selected CONTENT_ROOT and must never leak into either
PyInstaller onedir tree.
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


def core_analysis(entrypoint, extra_hiddenimports=None):
    return Analysis(
        [str(project_root / "packaging" / entrypoint)],
        pathex=[str(package_root)],
        binaries=[],
        datas=list(common_datas),
        hiddenimports=list(common_hiddenimports) + list(extra_hiddenimports or []),
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=list(core_excludes),
        noarchive=False,
        optimize=1,
    )


service_analysis = core_analysis("service_entry.py")
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
service_collect = COLLECT(
    service_exe,
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
