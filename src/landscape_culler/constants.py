from __future__ import annotations

import os

PROPRIETARY_RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".pef"}
READABLE_RAW_EXTENSIONS = PROPRIETARY_RAW_EXTENSIONS | {".dng"}

MODEL_ID = "facebook/dinov2-base"
PREVIEW_VERSION = "raw-orientation-v2"
FEATURE_VERSION = "dinov2-base+technical-v2"

# DINO remains the similarity-grouping encoder. These models produce separate,
# versioned signals and must never be appended to the DINO vector.
AESTHETIC_MODEL_ID = "qrealign"
AESTHETIC_PIPELINE_VERSION = "qrealign-mini-v1"
VLM_MODEL_ID = os.environ.get("PHOTO_AI_VLM_MODEL", "qwen3-vl:8b-instruct")
VLM_PROMPT_VERSION = "landscape-group-critic-v1"
VLM_IMAGE_VERSION = "preview-jpeg768-q86-v1"
OLLAMA_ENDPOINT = "http://127.0.0.1:11435"
SCORING_PIPELINE_VERSION = "landscape-ai-v5-general"
