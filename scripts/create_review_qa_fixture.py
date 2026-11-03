"""Create an explicitly synthetic review fixture for local UI acceptance.

Usage: python scripts/create_review_qa_fixture.py --root output/review-qa-unique
The root must not exist. No user media or existing workspace is read or changed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from landscape_culler.collaborative_review import ReviewStore
from landscape_culler.timelapse import find_media_tool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists():
        parser.error("Choose a fresh --root; fixtures never overwrite an existing directory.")
    media = root / "synthetic-media"
    media.mkdir(parents=True)
    for index, (name, color, portrait) in enumerate([
        ("01_architecture.jpg", "#bfd9ea", False), ("02_landscape.jpg", "#b9d3b7", False),
        ("03_portrait.jpg", "#e4cfba", True),
    ]):
        size = (640, 900) if portrait else (1000, 650)
        with Image.new("RGB", size, color) as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle((40, 100, size[0] - 40, size[1] - 80), outline="#43515d", width=8)
            draw.text((60, 50), f"SYNTHETIC REVIEW FIXTURE {index + 1}", fill="#243141")
            draw.ellipse((100, 150, min(430, size[0] - 60), 450), fill="#e5ad54")
            image.save(media / name, "JPEG", quality=90)
    ffmpeg = find_media_tool("ffmpeg")
    if not ffmpeg:
        raise SystemExit("FFmpeg unavailable; only synthetic photo files were created.")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
                    "testsrc2=s=640x360:r=25:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(media / "04_synthetic_video.mp4")], check=True, capture_output=True, timeout=30)
    store = ReviewStore(args.data_dir or root / "data")
    workspace = store.create(str(media), "界面验收 · 合成素材")
    wid = workspace["workspace_id"]
    workspace = store.organize(wid, workspace["revision"])
    photo = next(item for item in workspace["items"] if item["name"] == "01_architecture.jpg")
    workspace = store.update_item(wid, photo["id"], 0, {"status": "keep", "quality": 3, "beauty": 2,
                                                      "chapter": "开场", "notes": "仅用于验收，不是真实照片。", "order": 0})
    video = next(item for item in workspace["items"] if item["kind"] == "video")
    workspace = store.update_item(wid, video["id"], 0, {"status": "maybe", "chapter": "过渡", "order": 1,
                                                      "segments": [{"in": 0.2, "out": 1.5, "note": "合成测试片段"}]})
    print(json.dumps({"data_dir": str(store.data_dir), "input_root": str(media), "workspace_id": wid,
                      "revision": workspace["revision"], "route": f"#toolbox/review/{wid}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
