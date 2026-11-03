# LRT Lite core

Imported from the user's local `E:/code/rea/lrt-lite/lrtlite` v0.2.0 on
2026-10-25. Its pyproject.toml declares MIT. This is the Python reimplementation,
not the LRTimelapse application, binary, GUI, or installer.

The toolbox uses the sequence model, XMP model, EXIF reader and numerical
algorithms. It computes edits in memory and writes new output sidecars; the
folder-writing workflow functions are not called against source photographs.
The original source project remains unchanged.

Integration changes: ExifTool discovery uses the toolbox configuration and
bundled resources; invalid source XMP is rejected by the adapter rather than
silently reset. Video rendering and persisted asynchronous jobs are implemented
in `landscape_culler/timelapse.py` and `timelapse_api.py`.
