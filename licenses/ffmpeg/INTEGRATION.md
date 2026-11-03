# FFmpeg portable tools for local photography toolbox candidates

The local optional tools are unmodified Gyan FFmpeg 8.0.1 full-build static
Windows x64 binaries, copied from the already installed Scoop distribution.
Their license is GPL v3; the accompanying LICENSE, README.txt and Scoop
manifest.json retain the original license text, configuration, source revision,
download URL and archive hash. The toolbox invokes these separate executables.

- Build provider: https://www.gyan.dev/ffmpeg/builds/
- Original package: https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.0.1-full_build.7z
- FFmpeg source revision recorded by provider: https://github.com/FFmpeg/FFmpeg/commit/894da5ca7d
- Original archive SHA-256: 42edab4b9241ff1184b26d5f4545322227589713a7e56cf4459c4c4e30ccaaac
- ffmpeg.exe: 211091968 bytes, SHA-256 74db6c184a03dba2bdfe23e1a1f41cf5a8385bc1de6a7a1b26db1dc541abef93
- ffprobe.exe: 210895360 bytes, SHA-256 55bb6c6289367ae2383efa86b26bf2596f8adb72ac747360eb13df162354161c

Together the executables add about 402.44 MiB before installer compression.
PE import inspection found Windows system DLLs only. Isolated PATH execution,
H.264 encoding, metadata inspection and full decoding are checked by
`packaging/verify-media-tools.py`; its local report is under `output/`.

This turn prepares local development/install candidates only; no public
distribution or claim of completed source distribution is made. Any public
release must preserve these notices and arrange the corresponding-source
delivery required by the bundled component licenses. Provider source links
are provenance information and are not a substitute for that release work.

The executable cache `packaging/vendor/ffmpeg-8.0.1/` and staged desktop tools
are already excluded from Git. Build with `-WithMediaTools`; optionally set
`-MediaToolsSource <directory>`. The build verifies the pinned hashes and
requires the original notice files. No download is performed.

The first complete 0.10 candidate measured 374.5 MiB as an NSIS installer.
Only explicit `-WithMediaTools` builds receive an extra installer allowance of
at most 32 MiB, additionally capped by the media bundle's actual byte size;
the original 350 MiB base-package limit and 700 MiB installed-payload limit
remain. The resulting candidate limit is 382 MiB. Each completed installer
has a `.budget.json` recording the actual values and limits.
