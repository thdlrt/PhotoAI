"""Verify portable FFmpeg tools without inheriting a development environment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def verify(tools: Path) -> dict:
    tools = tools.resolve(strict=True)
    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg, ffprobe = tools / f"ffmpeg{suffix}", tools / f"ffprobe{suffix}"
    environment = dict(os.environ)
    environment["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32") if os.name == "nt" else "/usr/bin:/bin"
    for name in tuple(environment):
        if name.startswith(("CONDA_", "PYTHON", "LD_LIBRARY_")):
            environment.pop(name, None)
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with tempfile.TemporaryDirectory(prefix="photo-toolbox-media-check-") as temporary:
        work = Path(temporary)
        def run(command):
            result = subprocess.run(command, cwd=work, env=environment, text=True, encoding="utf-8",
                                    errors="replace", capture_output=True, timeout=40, **kwargs)
            if result.returncode:
                raise RuntimeError(result.stderr[-4000:])
            return result.stdout
        versions = {tool.name: run([str(tool), "-version"]).splitlines()[0] for tool in (ffmpeg, ffprobe)}
        video = work / "test.mp4"
        run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
             "testsrc=size=160x90:rate=12", "-frames:v", "12", "-c:v", "libx264", "-threads", "2",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video)])
        metadata = json.loads(run([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height,nb_frames:format=duration", "-of", "json", str(video)]))
        stream = metadata["streams"][0]
        if (stream["width"], stream["height"], int(stream["nb_frames"])) != (160, 90, 12):
            raise RuntimeError(f"Portable encoder output mismatch: {metadata}")
        run([str(ffmpeg), "-v", "error", "-i", str(video), "-f", "null", "-"])
    files = {}
    for tool in (ffmpeg, ffprobe):
        with tool.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        files[tool.name] = {"bytes": tool.stat().st_size, "sha256": digest}
    return {"status": "passed", "isolated_path": environment["PATH"], "versions": versions,
            "files": files,
            "video": metadata, "full_decode": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tools", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = verify(args.tools)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
