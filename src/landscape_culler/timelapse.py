"""LRT Lite integration with isolated outputs and resumable job history.

Source photographs and source sidecars are read only. A new process operation
always starts from the scanned originals, so repeating it never stacks edits.
"""
from __future__ import annotations

import copy
import contextlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .util import read_json, write_json

ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_FRAMES = 20000
RENDER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


def find_media_tool(name: str) -> str | None:
    if name not in {"ffmpeg", "ffprobe", "exiftool"}:
        raise ValueError("Unsupported media tool")
    suffix = ".exe" if os.name == "nt" else ""
    filename = name + suffix
    package_name = "ffmpeg" if name in {"ffmpeg", "ffprobe"} else name
    candidates: list[Path] = []
    for key in (f"PHOTO_TOOLBOX_{name.upper()}", f"PHOTO_AI_{name.upper()}",
                "LRT_EXIFTOOL" if name == "exiftool" else ""):
        if key and os.environ.get(key):
            candidates.append(Path(os.environ[key]).expanduser())
    project_root = Path(__file__).resolve().parents[2]
    roots = [Path(sys.executable).resolve().parent, project_root,
             project_root / "desktop" / "src-tauri" / "resources"]
    for key in ("PHOTO_AI_RESOURCE_ROOT", "PHOTO_AI_RESOURCES_DIR", "PHOTO_AI_RESOURCE_DIR"):
        if os.environ.get(key):
            roots.insert(0, Path(os.environ[key]))
    if getattr(sys, "_MEIPASS", None):
        roots.insert(0, Path(sys._MEIPASS))
    for root in roots:
        candidates.extend((root / "tools" / filename,
                           root / "tools" / package_name / filename,
                           root / "packaging" / "resources" / "tools" / filename,
                           root / ".runtime" / "tools" / filename,
                           root / filename))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    # The existing ExifTool package has a versioned, nested directory. Search
    # only the tool bundles, never the user's media tree or entire drive.
    for root in roots:
        for base in (root / ".runtime" / "tools", root / "tools", root / "packaging" / "resources" / "tools",
                     root / "packaging" / "vendor"):
            if base.is_dir():
                for pattern in (f"{package_name}*/{filename}", f"{package_name}*/*/{filename}"):
                    found = sorted(base.glob(pattern), reverse=True)
                    for candidate in found:
                        if candidate.is_file():
                            return str(candidate.resolve())
    return shutil.which(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(value: str) -> str:
    if not ID_RE.fullmatch(value):
        raise ValueError("任务或序列编号无效。")
    return value


def _snapshot(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hidden() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class Cancelled(RuntimeError):
    pass


class TimelapseManager:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "toolbox" / "timelapse"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="timelapse")
        self.events: dict[str, threading.Event] = {}
        self.processes: dict[str, subprocess.Popen] = {}
        self.jobs: dict[str, dict] = {}
        for path in (self.root / "jobs").glob("*.json"):
            try:
                job = read_json(path)
                if not isinstance(job, dict) or job.get("id") != path.stem:
                    continue
                _id(path.stem)
                if job.get("status") in {"queued", "running"}:
                    job.update(status="interrupted", error="上次运行已中断，可重新执行；原片未修改。", finished_at=_now())
                    write_json(path, job)
                self.jobs[job["id"]] = job
            except (OSError, ValueError, TypeError):
                continue

    def close(self) -> None:
        with self.lock:
            active = [key for key, job in self.jobs.items() if job.get("status") in {"queued", "running"}]
        for key in active:
            self.cancel(key)
        self.executor.shutdown(wait=False, cancel_futures=True)

    def capabilities(self) -> dict:
        from ._vendor.lrtlite.exif import find_exiftool
        from ._vendor.lrtlite.sequence import SUPPORTED_EXTENSIONS
        try:
            import PIL
            pillow = PIL.__version__
        except ImportError:
            pillow = None
        ffmpeg, ffprobe = find_media_tool("ffmpeg"), find_media_tool("ffprobe")
        return {"engine": "LRT Lite", "engine_version": "0.2.0", "ffmpeg": ffmpeg,
                "ffprobe": ffprobe, "exiftool": find_exiftool(), "pillow": pillow,
                "can_render": bool(ffmpeg and ffprobe and pillow),
                "scan_extensions": sorted(SUPPORTED_EXTENSIONS),
                "render_extensions": sorted(RENDER_EXTENSIONS),
                "raw_render": False, "max_frames": MAX_FRAMES}

    def _save_job(self, job: dict) -> None:
        write_json(self.root / "jobs" / f"{job['id']}.json", job)

    def _check(self, job_id: str) -> None:
        if self.events[job_id].is_set():
            raise Cancelled("任务已取消；原片未修改。")

    def _progress(self, job_id: str, phase: str, progress: float) -> None:
        self._check(job_id)
        with self.lock:
            job = self.jobs[job_id]
            job.update(phase=phase, progress=round(max(0, min(1, progress)), 4))

    def submit(self, action: str, params: dict, session_id: str | None = None) -> dict:
        if action not in {"scan", "process", "render"}:
            raise ValueError("未知延时摄影操作。")
        if session_id:
            self.session(session_id)
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "action": action, "session_id": session_id, "status": "queued",
               "phase": "等待处理", "progress": 0, "created_at": _now(), "params": params,
               "result": None, "error": None}
        with self.lock:
            if session_id and params.get("base_revision") is not None:
                current = self.session(session_id)
                if int(current.get("revision", 0)) != params["base_revision"]:
                    raise ValueError("序列已更新，请读取最新修订号后重试。")
            if session_id and any(j.get("session_id") == session_id and j.get("status") in {"queued", "running"} for j in self.jobs.values()):
                raise ValueError("该序列已有任务正在处理，请等待完成或取消后重试。")
            self.jobs[job_id] = job
            self.events[job_id] = threading.Event()
            self._save_job(job)
            self.executor.submit(self._run, job_id)
        return self.job(job_id)

    def _run(self, job_id: str) -> None:
        try:
            with self.lock:
                job = self.jobs[job_id]
                self._check(job_id)
                job.update(status="running", started_at=_now())
                self._save_job(job)
            method = getattr(self, f"_{job['action']}")
            result = method(job_id, job["params"], job.get("session_id"))
            with self.lock:
                job.update(status="completed", progress=1, phase="已完成", result=result)
        except Cancelled as exc:
            with self.lock:
                job.update(status="cancelled", phase="已取消", error=str(exc))
        except Exception as exc:
            with self.lock:
                job.update(status="failed", phase="处理失败", error=str(exc))
        finally:
            with self.lock:
                job["finished_at"] = _now()
                self._save_job(job)
                self.processes.pop(job_id, None)

    def job(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(_id(job_id))
            if job is None:
                raise FileNotFoundError("任务不存在。")
            return copy.deepcopy(job)

    def list_jobs(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)[:100])

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            self.job(job_id)
            event = self.events.get(job_id)
            if event:
                event.set()
            process = self.processes.get(job_id)
            if process and process.poll() is None:
                process.terminate()
        return self.job(job_id)

    def _session_path(self, session_id: str) -> Path:
        return self.root / "sessions" / _id(session_id) / "session.json"

    def session(self, session_id: str) -> dict:
        path = self._session_path(session_id)
        if not path.is_file():
            raise FileNotFoundError("延时摄影序列不存在。")
        with self.lock:
            return read_json(path)

    def list_sessions(self) -> list[dict]:
        out = []
        for path in (self.root / "sessions").glob("*/session.json"):
            try:
                item = self.session(path.parent.name)
                out.append({k: v for k, v in item.items() if k not in {"frames", "timeline"}})
            except (OSError, ValueError, TypeError):
                continue
        return sorted(out, key=lambda i: i["created_at"], reverse=True)[:100]

    def _scan(self, job_id: str, params: dict, _session_id: str | None) -> dict:
        from PIL import Image, ImageOps
        from ._vendor.lrtlite.sequence import SUPPORTED_EXTENSIONS, Frame, Sequence, natural_key, sidecar_for
        from ._vendor.lrtlite.xmp import XmpSidecar
        root = Path(params["input_root"]).expanduser().resolve(strict=True)
        if not root.is_dir() or root.parent == root:
            raise ValueError("请选择具体的序列文件夹。")
        extensions = set(params.get("extensions") or SUPPORTED_EXTENSIONS)
        if not extensions <= SUPPORTED_EXTENSIONS:
            raise ValueError("包含不支持的图片格式。")
        paths = sorted((p for p in root.iterdir() if p.is_file() and not p.is_symlink()
                        and p.suffix.lower() in extensions and not p.name.startswith(".")), key=natural_key)
        if not 2 <= len(paths) <= MAX_FRAMES:
            raise ValueError(f"序列需包含 2 至 {MAX_FRAMES} 张图片，当前为 {len(paths)} 张。")
        if len({p.stem.casefold() for p in paths}) != len(paths):
            raise ValueError("目录内存在同名不同格式照片（例如 RAW+JPEG），请按一种格式扫描，避免重复帧和 XMP 冲突。")
        frames = []
        sidecar_states = []
        for index, path in enumerate(paths):
            self._progress(job_id, "读取序列", 0.05 + 0.1 * index / len(paths))
            sc = sidecar_for(path)
            sidecar = XmpSidecar.load(sc) if sc.exists() else XmpSidecar.new()
            if not math.isfinite(sidecar.exposure()):
                raise ValueError(f"{sc.name} 的曝光值无效。")
            frames.append(Frame(path=path, index=index, sidecar=sidecar, sidecar_missing=not sc.exists()))
            sidecar_states.append(_snapshot(sc) if sc.exists() else None)
        seq = Sequence(root=root, frames=frames)
        seq.load_exif(progress=lambda done, total: self._progress(job_id, "读取曝光信息", 0.15 + 0.55 * done / max(1, total)))
        items = []
        sizes: set[tuple[int, int]] = set()
        warnings = []
        for i, frame in enumerate(frames):
            self._progress(job_id, "检查帧尺寸", 0.7 + 0.25 * i / len(frames))
            size = None
            if frame.path.suffix.lower() in RENDER_EXTENSIONS:
                try:
                    with Image.open(frame.path) as image:
                        width, height = image.size
                        if image.getexif().get(274) in {5, 6, 7, 8}:
                            width, height = height, width
                        size = [width, height]
                        sizes.add((width, height))
                except (OSError, ValueError) as exc:
                    raise ValueError(f"无法读取 {frame.name}: {exc}") from exc
            items.append({"index": i, "name": frame.name, "path": str(frame.path),
                          "snapshot": _snapshot(frame.path), "sidecar_snapshot": sidecar_states[i],
                          "rating": frame.rating, "keyframe": frame.is_keyframe,
                          "ev": _finite(frame.exif.ev100 if frame.exif else None),
                          "exposure": frame.exposure, "original_exposure": frame.exposure,
                          "luminance": None, "size": size})
        raw_count = sum(p.suffix.lower() not in RENDER_EXTENSIONS for p in paths)
        if raw_count:
            warnings.append("RAW 序列可分析并导出 XMP；正式视频请使用 Lightroom 导出的 JPEG/TIFF 序列。")
        if len(sizes) > 1:
            warnings.append("序列尺寸不一致；合成前需选择允许混合尺寸，画面将等比缩放并补黑边。")
        if not any(f["ev"] is not None for f in items):
            warnings.append("没有可用的相机曝光信息，圣杯校正将跳过；去闪仍可使用图片亮度。")
        session_id = uuid.uuid4().hex
        payload = {"schema_version": 1, "revision": 0, "id": session_id, "name": root.name, "input_root": str(root),
                   "created_at": _now(), "frame_count": len(frames), "raw_count": raw_count,
                   "renderable": raw_count == 0, "mixed_dimensions": len(sizes) > 1,
                   "dimensions": [list(s) for s in sorted(sizes)], "warnings": warnings,
                   "frames": items, "timeline": self._timeline(items), "last_process": None, "last_render": None}
        self._check(job_id)
        with self.lock:
            write_json(self._session_path(session_id), payload)
        return {"session_id": session_id, "frame_count": len(frames)}

    @staticmethod
    def _timeline(frames: list[dict]) -> list[dict]:
        return [{"index": f["index"], "name": f["name"], "ev": f["ev"],
                 "corrected": None if f["ev"] is None else f["ev"] + f["exposure"],
                 "exposure": f["exposure"], "luminance": f["luminance"], "keyframe": f["keyframe"]} for f in frames]

    def _source(self, session: dict, index: int) -> Path:
        if not 0 <= index < len(session["frames"]):
            raise ValueError("帧编号超出范围。")
        item = session["frames"][index]
        path = Path(item["path"])
        if path.is_symlink() or path.resolve().parent != Path(session["input_root"]).resolve():
            raise ValueError("序列路径已变化，请重新扫描。")
        if not path.is_file() or _snapshot(path) != item["snapshot"]:
            raise ValueError(f"原片已变化，请重新扫描：{path.name}")
        return path

    def _load_frames(self, session: dict, job_id: str):
        from ._vendor.lrtlite.sequence import Frame, Sequence, sidecar_for
        from ._vendor.lrtlite.xmp import XmpSidecar
        frames = []
        for item in session["frames"]:
            self._check(job_id)
            path = self._source(session, item["index"])
            sc = sidecar_for(path)
            if (_snapshot(sc) if sc.exists() else None) != item["sidecar_snapshot"]:
                raise ValueError(f"XMP 已被修改，请重新扫描以保留最新调整：{sc.name}")
            frames.append(Frame(path=path, index=item["index"],
                                sidecar=XmpSidecar.load(sc) if sc.exists() else XmpSidecar.new()))
        seq = Sequence(root=Path(session["input_root"]), frames=frames)
        seq.load_exif(progress=lambda d, n: self._progress(job_id, "读取曝光信息", 0.1 * d / max(1, n)))
        return frames

    def _process(self, job_id: str, params: dict, session_id: str) -> dict:
        from ._vendor.lrtlite import analysis
        session = self.session(session_id)
        frames = self._load_frames(session, job_id)
        lums = analysis.sample_luminance(frames, should_stop=lambda: self.events[job_id].is_set(),
                                         progress=lambda d, n: self._progress(job_id, "采样亮度", 0.1 + 0.4 * d / max(1, n)))
        self._check(job_id)
        mode = params.get("keyframe_mode", "auto")
        if mode == "auto":
            picks = analysis.auto_keyframes(frames, sensitivity=params.get("sensitivity", 1.0))
        elif mode == "even":
            picks = analysis.evenly_spaced_keyframes(len(frames), params.get("keyframe_count", 8))
        elif mode == "manual":
            picks = sorted(set(params.get("keyframe_indices", [])))
            if any(i < 0 or i >= len(frames) for i in picks):
                raise ValueError("关键帧编号超出序列范围。")
        else:
            picks = [f.index for f in frames if f.is_keyframe]
        if mode != "existing":
            for frame in frames:
                frame.sidecar.rating = 4 if frame.index in picks else 0
        overrides = params.get("keyframe_exposures", {})
        for index, exposure in overrides.items():
            i = int(index)
            if i not in picks:
                raise ValueError("曝光调整只能应用到已选择的关键帧。")
            frames[i].sidecar.set_exposure(float(exposure))
        if params.get("transition"):
            if len(picks) < 2:
                raise ValueError("曝光过渡至少需要两个关键帧。")
            for left, right in zip(picks, picks[1:]):
                a, b = frames[left].exposure, frames[right].exposure
                for i in range(left + 1, right):
                    frames[i].sidecar.set_exposure(a + (b - a) * (i - left) / (right - left))
        strength = params.get("strength", 1.0)
        window = params.get("smooth_window", 11)
        warnings = []
        if params.get("holy_grail", True):
            offsets = analysis.holy_grail_offsets(frames, strength=strength, smooth_window=window)
            if not any(f.exif and f.exif.ev100 is not None for f in frames):
                warnings.append("缺少 EXIF 曝光信息，已跳过圣杯校正。")
            for frame, offset in zip(frames, offsets):
                frame.sidecar.set_exposure(frame.exposure + offset)
        if params.get("deflicker", True):
            # Use brightness after the preceding exposure correction, avoiding a
            # second correction of the same exposure step.
            adjusted = [None if lum is None else lum * 2 ** (f.exposure - old["original_exposure"])
                        for lum, f, old in zip(lums, frames, session["frames"])]
            if sum(v is not None for v in adjusted) < 3:
                warnings.append("可用亮度样本不足 3 帧，已跳过去闪。")
            offsets = analysis.deflicker_offsets(adjusted, smooth_window=window, strength=strength)
            for frame, offset in zip(frames, offsets):
                frame.sidecar.set_exposure(frame.exposure + offset)
        output = self.root / "sessions" / session_id / "outputs" / job_id
        sidecars = output / "sidecars"
        sidecars.mkdir(parents=True, exist_ok=False)
        output_frames = copy.deepcopy(session["frames"])
        mapping = []
        for i, frame in enumerate(frames):
            self._progress(job_id, "导出 XMP", 0.55 + 0.35 * (i + 1) / len(frames))
            frame.sidecar.save(sidecars / f"{frame.stem}.xmp")
            output_frames[i].update(exposure=frame.exposure, rating=frame.rating,
                                    keyframe=frame.is_keyframe, luminance=_finite(frame.luminance))
            mapping.append({"source": str(frame.path), "sidecar": f"sidecars/{frame.stem}.xmp",
                            "exposure": frame.exposure, "original_exposure": session["frames"][i]["original_exposure"]})
        write_json(output / "manifest.json", {"session_id": session_id, "job_id": job_id, "params": params, "frames": mapping})
        archive = output / "timelapse-xmp.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(output / "manifest.json", "manifest.json")
            for path in sorted(sidecars.glob("*.xmp")):
                bundle.write(path, f"sidecars/{path.name}")
        before = [None if f["ev"] is None else f["ev"] + f["original_exposure"] for f in output_frames]
        after = [None if f["ev"] is None else f["ev"] + f["exposure"] for f in output_frames]
        metrics = {"before_max_jump": max(analysis.jumps(before), default=None),
                   "after_max_jump": max(analysis.jumps(after), default=None),
                   "max_correction": max(abs(f["exposure"] - f["original_exposure"]) for f in output_frames)}
        result = {"session_id": session_id, "output_dir": str(output), "sidecar_dir": str(sidecars),
                  "archive_path": str(archive), "keyframes": len(picks), "frames": len(frames),
                  "metrics": metrics, "warnings": warnings, "job_id": job_id}
        self._check(job_id)
        with self.lock:
            session["revision"] = int(session.get("revision", 0)) + 1
            result["revision"] = session["revision"]
            session.update(frames=output_frames, timeline=self._timeline(output_frames), last_process=result)
            write_json(self._session_path(session_id), session)
        return result

    def preview(self, session_id: str, index: int) -> bytes:
        from PIL import Image, ImageOps
        session = self.session(session_id)
        path = self._source(session, index)
        if path.suffix.lower() in RENDER_EXTENSIONS:
            with Image.open(path) as original:
                image = ImageOps.exif_transpose(original).convert("RGB")
        else:
            from ._vendor.lrtlite.exif import extract_previews
            import tempfile
            with tempfile.TemporaryDirectory(prefix="photo-toolbox-preview-") as tmp:
                previews = extract_previews([path], Path(tmp))
                if path not in previews:
                    previews = extract_previews([path], Path(tmp), tag="JpgFromRaw")
                if path not in previews:
                    raise ValueError("未找到 RAW 内嵌预览，请配置 ExifTool 或使用导出的 JPEG。")
                with Image.open(previews[path]) as original:
                    image = ImageOps.exif_transpose(original).convert("RGB")
        try:
            image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=86)
            return buffer.getvalue()
        finally:
            image.close()

    def _render(self, job_id: str, params: dict, session_id: str) -> dict:
        from PIL import Image, ImageOps
        session = self.session(session_id)
        if not session["renderable"]:
            raise ValueError("请先在 Lightroom 将 RAW 导出为 JPEG/TIFF 序列，再扫描并合成。")
        if session["mixed_dimensions"] and not params.get("allow_mixed_dimensions", False):
            raise ValueError("序列包含不同尺寸；确认允许混合尺寸后可等比缩放补黑边合成。")
        ffmpeg, ffprobe = find_media_tool("ffmpeg"), find_media_tool("ffprobe")
        if not ffmpeg or not ffprobe:
            raise ValueError("缺少 FFmpeg 或 FFprobe，请配置 PHOTO_TOOLBOX_FFMPEG / PHOTO_TOOLBOX_FFPROBE。")
        width, height, fps = params.get("width", 1920), params.get("height", 1080), params.get("fps", 25)
        if width % 2 or height % 2:
            raise ValueError("H.264 输出宽高需要为偶数。")
        # Validate every snapshot before creating a video, including later frames.
        for item in session["frames"]:
            self._check(job_id)
            self._source(session, item["index"])
        output = self.root / "sessions" / session_id / "outputs" / job_id
        output.mkdir(parents=True, exist_ok=False)
        partial = output / "timelapse.partial.mp4"
        video = output / "timelapse.mp4"
        log_path = output / "ffmpeg.log"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                   "-f", "image2pipe", "-framerate", str(fps), "-vcodec", "mjpeg", "-i", "pipe:0",
                   "-an", "-c:v", "libx264", "-preset", "medium", "-crf", str(params.get("crf", 18)),
                   "-pix_fmt", "yuv420p", "-threads", "2", "-movflags", "+faststart", str(partial)]
        process = None
        try:
            with log_path.open("wb") as log:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, **_hidden())
                with self.lock:
                    self.processes[job_id] = process
                for position, item in enumerate(session["frames"]):
                    self._progress(job_id, "合成视频", 0.05 + 0.85 * position / session["frame_count"])
                    source = self._source(session, item["index"])
                    with Image.open(source) as original:
                        image = ImageOps.exif_transpose(original).convert("RGB")
                    try:
                        if params.get("apply_corrections", False):
                            delta = item["exposure"] - item["original_exposure"]
                            if abs(delta) > 0.001:
                                lut = _exposure_lut(delta)
                                corrected = image.point(lut * 3)
                                image.close()
                                image = corrected
                        image.thumbnail((width, height), Image.Resampling.LANCZOS)
                        canvas = Image.new("RGB", (width, height), "black")
                        try:
                            canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
                            buffer = io.BytesIO()
                            canvas.save(buffer, "JPEG", quality=95)
                            process.stdin.write(buffer.getvalue())
                        finally:
                            canvas.close()
                    finally:
                        image.close()
                process.stdin.close()
                self._progress(job_id, "完成编码", 0.91)
                deadline = time.monotonic() + 300
                while process.poll() is None:
                    self._check(job_id)
                    if time.monotonic() > deadline:
                        raise RuntimeError("编码收尾超时，请降低输出分辨率后重试。")
                    time.sleep(0.1)
                if process.returncode:
                    detail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                    raise RuntimeError(f"FFmpeg 合成失败：{detail}")
            self._check(job_id)
            self._progress(job_id, "验证视频", 0.96)
            probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=width,height,nb_frames,r_frame_rate:format=duration", "-of", "json", str(partial)],
                                   capture_output=True, text=True, timeout=60, **_hidden())
            metadata = json.loads(probe.stdout) if probe.returncode == 0 else {}
            stream = next(iter(metadata.get("streams", [])), {})
            count = int(stream.get("nb_frames", 0))
            if count != session["frame_count"] or stream.get("width") != width or stream.get("height") != height:
                raise RuntimeError("输出视频帧数或尺寸校验失败，未标记为完成。")
            self._check(job_id)
            partial.rename(video)
            result = {"session_id": session_id, "job_id": job_id, "video_path": str(video),
                      "output_dir": str(output), "frames": count, "fps": fps, "width": width, "height": height,
                      "duration": float(metadata["format"]["duration"]), "bytes": video.stat().st_size,
                      "apply_corrections": bool(params.get("apply_corrections", False)), "verified": True}
            write_json(output / "manifest.json", {**result, "params": params, "source_root": session["input_root"]})
            with self.lock:
                session["revision"] = int(session.get("revision", 0)) + 1
                result["revision"] = session["revision"]
                session["last_render"] = result
                write_json(self._session_path(session_id), session)
            return result
        except Exception:
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process and process.stdin and not process.stdin.closed:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            if self.events[job_id].is_set():
                raise Cancelled("任务已取消；未生成完成视频。")
            raise

    def artifact(self, job_id: str, kind: str) -> Path:
        job = self.job(job_id)
        if job["status"] != "completed":
            raise ValueError("任务尚未完成。")
        field = {"video": "video_path", "xmp": "archive_path"}.get(kind)
        if not field or not job.get("result", {}).get(field):
            raise FileNotFoundError("该任务没有此产物。")
        path = Path(job["result"][field]).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise FileNotFoundError("产物不存在。")
        return path


def _exposure_lut(delta: float) -> list[int]:
    gain = 2 ** max(-10.0, min(10.0, delta))
    table = []
    for value in range(256):
        srgb = value / 255
        linear = srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4
        linear = min(1.0, max(0.0, linear * gain))
        output = linear * 12.92 if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055
        table.append(round(output * 255))
    return table
