# Windows desktop release scaffold

`desktop/` is the Tauri 2 shell for the installed product. It owns the visible `PhotoAI.exe`,
single-instance behavior, `photoai://open`, WebView2 storage, and startup of the hidden core
service. The current browser entry point remains a development-only compatibility path.

## Required staged inputs

Build the two lightweight PyInstaller `onedir` sidecars first. The default layout consumed by
`build-desktop.ps1` is:

```text
packaging/dist/core/
├─ PhotoAI.Service/
│  ├─ PhotoAI.Service.exe
│  └─ _internal/...
└─ PhotoAI.CoreWorker/
   ├─ PhotoAI.CoreWorker.exe
   └─ _internal/...
```

Audited base resources are staged below `packaging/stage/tools`,
`packaging/stage/integrations`, and `packaging/stage/manifests`. Empty or absent directories fail
the release build. `build-windows.ps1` owns the exact pinned uv, ExifTool, clean Lightroom template,
AI Worker wheel, hashes, dependency lock, local wheelhouse, model manifest, and release inventory.

The desktop build deliberately rejects Torch, Transformers, OpenCV, pyvips, rawpy, OpenColorIO,
Ollama, and model folders inside either core sidecar. Large AI dependencies belong to the
post-install managed environment below `CONTENT_ROOT`.

## Service contract

The shell starts:

```text
PhotoAI.Service.exe --port 0 --content-root <CONTENT_ROOT>
```

It also supplies `PHOTO_AI_CORE_WORKER_EXE`, `PHOTO_AI_RESOURCE_ROOT`, and
`PHOTO_AI_PARENT_PID` to the service process; these are per-launch values and must never be
persisted into projects or portable settings. Tauri removes inherited Ollama endpoint/binary
variables before spawning the Service, and the Service repeats that scrub after Content Root
initialization so an older system or developer Ollama can never become the managed instance.

The first complete stdout line must be UTF-8 JSON with this exact envelope:

```text
PHOTO_AI_SERVICE/1 {"port":49152,"token":"<at-least-32-characters>","control_token":"<at-least-32-characters>","pid":1234,"origin":"http://127.0.0.1:49152"}
```

The PID, random port, HTTP loopback origin, and both tokens are validated. The token-bearing
handshake line is never written to the desktop log. The WebView opens `/?token=...`; the service
must exchange that bootstrap token once for an HttpOnly session cookie and redirect to a token-free
URL. The independent control token remains only in Tauri memory and authenticates graceful
shutdown; it is never exposed to the WebView or persisted.

Smart crop, scoring, style recommendation/preview, and final `.cube` rendering are dispatched as
versioned `PHOTO_AI_WORKER/1` jobs through the managed environment. Neither lightweight sidecar may
import their Torch, Transformers, OpenCV, rawpy, pyvips, OpenColorIO, or Ollama implementation.

Lightroom is not part of AI environment installation. The bundled plug-in declares SDK minimum
14.3, so desktop detection accepts Lightroom Classic 14.3 and newer without a future-version upper
bound. Version 15.3 is the fully validated baseline; other accepted versions remain usable with an
untested notice until a live plug-in heartbeat confirms the running version.

## Build

From PowerShell on the Windows x64 release machine, use the full orchestrator:

```powershell
packaging\build-windows.ps1
```

Use `-NoBundle` for a compile-only check. `build-desktop.ps1` remains the lower-level Tauri stage
and accepts explicit `-ServiceOnedir`, `-CoreWorkerOnedir`, and `-ResourceStage` paths. The release
build uses current-user NSIS,
bundles the offline WebView2 bootstrapper, enforces the 350MB installer limit, and writes a
side-by-side `.sha256` file. The final artifact is named `PhotoAI-Setup-x64.exe`.

The first launch creates and owns `<install>\data` before opening the WebView. The Settings page can
switch to another empty or existing marked writable local NTFS/ReFS folder; UNC paths, drive roots,
the install directory itself, and ancestors of the install directory are rejected. A descendant
such as `<install>\data` is valid. `HKCU\Software\PhotoAI` stores only the selected path and installed
version. `marker.json` uses the shared `application/layout_version/root_id/created_utc` contract;
WebView2 data is rooted at `CONTENT_ROOT\cache\webview2`.

## Deliberate limitations of this scaffold

- This scaffold has no updater or signing step. Manual overwrite installation and private-build
  SHA-256 verification are the release policy.
- Only `photoai://open` is accepted in the first version; payload-bearing project deep links are
  intentionally not implemented.
- Existing developer `.runtime` data is deliberately left untouched and is not adopted by an
  installed release. The portable migration format transfers settings only, as documented.
- Private unsigned beta installers can trigger Windows SmartScreen. Signing is not a `0.9` release
  gate, while the side-by-side SHA-256 file remains mandatory.
