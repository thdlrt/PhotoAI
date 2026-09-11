# Windows release pipeline

## Preparing a public source checkout

The Git repository intentionally excludes installed tools, build caches and model
weights. Only the small pinned Python wheels and text manifests are included.
Before building the desktop installer:

1. Install Python 3.12, Node.js, Rust's x86_64 MSVC toolchain and the Visual Studio
   C++ Build Tools. These are **builder-only**, not end-user dependencies.
2. Obtain portable uv **0.11.2** from
   <https://github.com/astral-sh/uv/releases/tag/0.11.2> and put `uv.exe` on the
   current shell's PATH. The Windows builder verifies its pinned binary digest.
3. Download the official ExifTool **13.59 Windows 64-bit** ZIP from
   <https://sourceforge.net/projects/exiftool/files/> and extract it under
   `packaging/vendor/exiftool-13.59/`, preserving the `exiftool-13.59_64` folder.
   The executable must be named `exiftool.exe` (rename the upstream `exiftool(-k).exe`
   if necessary). The build checks the reviewed executable, not its filename alone.
4. From the repository root, run `. ./scripts/env.ps1` and
   `uv sync --extra dev` to prepare the lightweight builder environment.
5. Run `./packaging/build-windows.ps1 -Quick`. The desktop builder installs its
   locked npm dependencies; WebView2's official offline installer is downloaded
   by Tauri when preparing the NSIS package.

Build tools are cached in `desktop/src-tauri/target/.tauri/`, not the system
profile. If Tauri times out downloading WebView2 over a slow connection, run
`./.venv/Scripts/python.exe packaging/prefetch-webview2.py` and retry the build.
It uses Microsoft's official URL and supports resuming into the same cache.

The checked-in `licenses/` folder must accompany the installer. When dependencies
change, regenerate it with `./.venv/Scripts/python.exe packaging/collect-notices.py`
after building the core sidecars and populating Cargo's locked Windows dependencies.
The inventory records upstream sources for unmodified third-party code.

## Release stages

Run `packaging/build-windows.ps1` on the reviewed Windows x64 build machine. It is the single
release entry point and performs these stages in order:

1. Verify the pinned builder-local `uv.exe` and ExifTool SHA-256 values and all product versions.
2. Build console-enabled PyInstaller `onedir` sidecars at
   `packaging/dist/core/PhotoAI.Service` and `PhotoAI.CoreWorker`.
3. Build the product wheel used by the post-install managed AI engine and write its SHA-256
   sidecar.
4. Stage only reviewed base resources below `packaging/stage`: pinned uv/ExifTool, a clean
   Lightroom plug-in template, dependency/model manifests, local pure-Python wheels, and the AI
   Worker wheel.
5. Run `verify-release.py`, then invoke `build-desktop.ps1` to produce the Tauri NSIS installer and
   installer SHA-256.

Useful build-only switches are `-Quick`, `-SkipDesktop`, `-SkipNpmInstall`, and `-NoBundle`.
`-Quick` preserves PyInstaller/Tauri analysis caches and skips repeated npm installation while
still running the resource release gate, self-test, installer size limit, and installer hash.
Use it for reviewed incremental builds; run a non-Quick clean build before a formal release.
`-SkipDesktop` still executes every Python/resource release gate.

The core sidecars intentionally exclude Torch, Transformers, OpenCV, OpenColorIO, Pillow, NumPy,
pyvips, rawpy, pyiqa, Hugging Face, Ollama, and all model weights. They include only the service,
CoreWorker, Web assets, and light file/state dependencies. Tauri starts them without a visible
console; they remain console-enabled so stdout can carry the versioned handshake/NDJSON protocols.

The base installer never copies the development `.runtime` tree. It carries no model weights and
does not need a machine Python, Conda, Ollama, or CUDA Toolkit. On first launch the desktop owns
`<install>\data` as its default `CONTENT_ROOT`; the user may change it later from Settings without
being forced through a picker. The reviewed `uv.exe` creates a managed Python 3.12 AI environment
below that root; all downloads, caches, Ollama files, and models stay there. Progress is rendered
inline below the active runtime/model row and reports transfer speed, ETA, resume bytes, elapsed
time, and heartbeats for quiet subprocesses. Hugging Face source selection probes the mainland
mirror and official origin concurrently, chooses the fastest reachable source, and fails over per
file without discarding partial downloads. Only a compatible NVIDIA display driver is an external
compute prerequisite. Lightroom remains optional.

`packaging/stage/manifests/release-manifest.json` records every staged resource with size and
SHA-256. The Lightroom template deliberately excludes `bridge-path.txt`, so no development-machine
absolute path can enter an installer.
