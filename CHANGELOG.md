# Changelog

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
