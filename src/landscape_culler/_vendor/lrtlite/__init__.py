"""LRT-lite: a small, readable reimplementation of the LRTimelapse core workflow.

The workflow reimplemented here was reconstructed from the installed
LRTimelapse 7 package (XMP templates, localisation strings and the ratings that
mark keyframes). It writes ordinary Lightroom-compatible sidecars, so Lightroom
and LRTimelapse itself can both read the result.
"""

from __future__ import annotations

__version__ = "0.2.0"

from .analysis import (  # noqa: F401
    auto_keyframes,
    deflicker_offsets,
    holy_grail_offsets,
    read_luminance,
)
from .sequence import Frame, Sequence, scan, sidecar_for  # noqa: F401
from .xmp import XmpSidecar  # noqa: F401
