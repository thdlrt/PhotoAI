# Changelog

## 0.9.0-beta.4

- Stage update installers outside the installed program directory, with an
  external helper working directory and TEMP/TMP, so nested data directories
  cannot make the updater block its own installation.
- Wait briefly for directory locks to clear and report a nonzero installer
  exit when the previous version cannot be staged. Existing data stays intact.
- Reuse the repaired beta.3 AI Worker for desktop-only updates by checking its
  visual-critique revision rather than requiring every desktop version to match.

## 0.9.0-beta.3

- Clarified confidence as 0–1 separately from 0–100 photo scores. Invalid model
  replies now receive the specific validation error on retry and leave bounded
  local diagnostics containing the failed reply and received value.
- Updated the visual-critique cache version and made older installed AI Workers
  request reconfiguration before running the old review implementation.
- Added confirmed source-file deletion for individual and batch photo selection,
  using a same-volume recycle directory and preserving manual review records.

## 0.9.0-beta.1 — first public release

- Windows desktop shell with a local service and isolated background workers.
- Group-first photo selection, editable groups, AI scoring and optional crop,
  basic-color and creative-look steps followed by unified export.
- NVIDIA 8GB/16GB resource profiles, post-install model download and offline
  resource import support; no model weights in the base installer.
- Optional Lightroom bridge for preview, processing and JPEG/XMP export.
- RAW/rendered-photo management and explicitly confirmed XMP cleanup.
- Fixed missing lazy-imported modules in packaged CoreWorker.
- Removed the accidental CLIP requirement from non-AI file tools.
- Added packaged XMP cleanup and file-preservation regression checks.

This is a beta, not a claim of full compatibility across every GPU, Lightroom
version or clean Windows installation. The 16GB setup was exercised on an RTX
5080; additional 8GB and clean-machine testing is welcome.
