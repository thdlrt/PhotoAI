# Third-party notices

PhotoAI's own source code is licensed under MIT. This does not relicense its
dependencies, models, imported presets or third-party applications.

## Base Windows distribution

- Tauri and its Rust dependencies: individual crate licenses are preserved in
  `licenses/rust/` with package versions in `licenses/inventory.json`.
- Python and the lightweight service dependencies (including FastAPI, Pydantic,
  Starlette, Uvicorn and Jinja2): notices are preserved in `licenses/python/`.
- PyInstaller: GPL with its bootloader exception; its applicable texts are in
  `licenses/python/`.
- uv 0.11.2: MIT OR Apache-2.0; notices in `licenses/uv/`.
  Upstream: https://github.com/astral-sh/uv/tree/0.11.2
- ExifTool 13.59: distributed under the terms of Perl (Artistic License or GPL).
  Its unmodified Windows distribution retains its Perl/runtime notices;
  copies are also in `licenses/exiftool/`.
  Upstream: https://exiftool.org/ and https://github.com/exiftool/exiftool
- Microsoft WebView2 Runtime: Microsoft terms, not MIT. The official offline
  installer is redistributed with this application and may show Microsoft's
  own installation/terms interface. https://developer.microsoft.com/microsoft-edge/webview2/

## Small Python wheels used during AI installation

`packaging/resources/wheels/` contains the pinned pure-Python `openai-clip`
1.0.1 and `pyvips` 3.2.0 wheels used by the installer. Their license texts are
preserved in `licenses/wheels/`. CLIP's upstream MIT notice is included
separately because the published wheel does not carry a LICENSE file.

- CLIP: https://github.com/openai/CLIP (MIT).
- pyvips: https://github.com/libvips/pyvips (MIT).
- libvips itself is a separate LGPL library downloaded with the optional AI
  environment; pyvips's MIT license does not replace libvips's license.

## Downloaded resources and optional integrations

Torch, Transformers, rawpy/LibRaw, OpenColorIO, OpenCV, pyiqa, Ollama and all
model weights are installed separately. Consult their upstream licenses and
model cards before redistribution or commercial use. Model sources and pinned
revisions are recorded in `packaging/resources/model-manifest.json`.

Adobe Lightroom, Camera Raw profiles and Adobe presets are **not** distributed
under this repository's license. Lightroom must be installed and licensed
separately. Any local preset extraction/import is for resources the user is
authorized to use; no Adobe preset files are included in this repository or
the base installer. Uploaded presets remain subject to their own licenses.

Optional community preset sources referenced by helper scripts retain their
upstream licenses: OpenFilmStocks (MIT), lightroom-workflow (MPL-2.0), and
Lightroom-Presets (MIT). Those preset libraries are not bundled here.

## License inventory

`licenses/inventory.json` describes the component versions and notice files
collected for this release. It is a notice inventory, not a claim that every
optional component is installed or a substitute for upstream license terms.
`ThirdPartyNotices.zip` in the Release contains the same notices for convenience.
