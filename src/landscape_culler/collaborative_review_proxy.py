"""On-demand, source-mapped H.264 previews for camera video codecs."""
from __future__ import annotations

import copy
import hashlib
import math
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .collaborative_review import ReviewConflict, ReviewStore, now, probe_video


class ReviewProxyJobs:
    def __init__(self, store: ReviewStore):
        self.store = store
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-proxy")
        self.closed = threading.Event()

    def start(self, workspace_id: str, asset_id: str, start: float = 0, duration: float = 60) -> dict:
        if self.closed.is_set():
            raise ReviewConflict("审片服务正在关闭。")
        if (isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0
                or isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration)
                or not 0 < duration <= 60):
            raise ValueError("预览起点必须为非负秒数，长度必须大于 0 且不超过 60 秒。")
        state = self.store._load(workspace_id)
        path = self.store.media_path(workspace_id, asset_id, state=state)
        item = state["assets"][asset_id]
        if item["kind"] != "video":
            raise ValueError("只有视频需要生成兼容预览。")
        metadata = self.store.video_probe(path)
        if start >= metadata["duration"]:
            raise ValueError("预览起点必须早于原视频结束。")
        duration = min(float(duration), metadata["duration"] - start)
        signature = f"proxy-h264-v1|{asset_id}|{item['size']}|{item['mtime_ns']}|{start:.6f}|{duration:.6f}"
        key = hashlib.sha256(signature.encode()).hexdigest()[:24]
        cache = self.store.root / workspace_id / "proxies" / f"proxy-{key}.mp4"
        with self.lock:
            if any(job["status"] in {"queued", "running", "cancelling"} for job in self.jobs.values()):
                raise ReviewConflict("已有兼容预览正在生成，请等待或取消。")
            job_id = uuid.uuid4().hex
            job = {"job_id": job_id, "workspace_id": workspace_id, "asset_id": asset_id, "kind": "proxy",
                   "status": "queued", "progress": 0, "source_start": float(start), "duration": duration,
                   "source_duration": metadata["duration"], "preview_only": True, "error": None,
                   "preview_url": f"/api/review-workspaces/{workspace_id}/proxies/{job_id}",
                   "created_at": now(), "name": item["name"], "cached": False,
                   "_path": str(cache), "_fingerprint": (item["size"], item["mtime_ns"])}
            self.jobs[job_id] = job
            self.cancel_events[job_id] = threading.Event()
            if cache.is_file() and cache.stat().st_size > 0:
                try:
                    cached_metadata = probe_video(cache)
                    job.update(status="done", progress=100, cached=True, duration=cached_metadata["duration"], finished_at=now())
                except (ValueError, OSError):
                    pass  # Rebuild a damaged derived cache without touching originals.
            if job["status"] != "done":
                self.pool.submit(self._run, job_id)
            return self._public(job)

    @staticmethod
    def _public(job: dict) -> dict:
        return copy.deepcopy({key: value for key, value in job.items() if not key.startswith("_")})

    def _update(self, job_id: str, **changes) -> None:
        with self.lock:
            self.jobs[job_id].update(changes)

    def _source(self, job: dict) -> Path:
        state = self.store._load(job["workspace_id"])
        asset = state["assets"].get(job["asset_id"])
        if not asset or (asset["size"], asset["mtime_ns"]) != job["_fingerprint"]:
            raise ReviewConflict("原片已变化，请重新生成对应的兼容预览。")
        return self.store.media_path(job["workspace_id"], job["asset_id"], state=state)

    def _run(self, job_id: str) -> None:
        from .timelapse import find_media_tool
        job = self.jobs[job_id]
        cancel = self.cancel_events[job_id]
        target = Path(job["_path"])
        token = uuid.uuid4().hex[:12]
        temporary = target.parent / f"{token}.mp4"
        progress = target.parent / f"{token}.progress"
        error_log = target.parent / f"{token}.error"
        process = None
        try:
            if cancel.is_set():
                self._update(job_id, status="cancelled", finished_at=now())
                return
            executable = find_media_tool("ffmpeg")
            if not executable:
                raise ValueError("未找到 FFmpeg，无法生成兼容预览。")
            source = self._source(job)
            target.parent.mkdir(parents=True, exist_ok=True)
            command = [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(job["source_start"]),
                       "-i", str(source), "-t", str(job["duration"]), "-map", "0:v:0", "-map", "0:a:0?",
                       "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                       "-r", "25", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
                       "-threads", "2", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                       "-progress", progress.name, "-nostats", temporary.name]
            self._update(job_id, status="running")
            with error_log.open("wb") as errors:
                process = subprocess.Popen(command, cwd=target.parent, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=errors,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                started = time.monotonic()
                while process.poll() is None:
                    if cancel.is_set() or time.monotonic() - started > 600:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                        if cancel.is_set():
                            self._update(job_id, status="cancelled", finished_at=now())
                            return
                        raise ValueError("生成兼容预览超过 10 分钟，已停止；请缩短片段。")
                    if progress.is_file():
                        with progress.open("rb") as handle:
                            handle.seek(max(0, progress.stat().st_size - 4096))
                            text = handle.read().decode("utf-8", "replace")
                        values = re.findall(r"out_time_us=(\d+)", text)
                        if values:
                            self._update(job_id, progress=min(99, round(int(values[-1]) / 1e6 / job["duration"] * 100)))
                    cancel.wait(0.25)
            if cancel.is_set():
                self._update(job_id, status="cancelled", finished_at=now())
                return
            if process.returncode or not temporary.is_file() or temporary.stat().st_size == 0:
                raise ValueError("FFmpeg 无法生成此视频的兼容预览；源文件可能损坏或编码不受支持。")
            metadata = probe_video(temporary)
            if metadata["duration"] > job["duration"] + 0.5:
                raise ValueError("预览时长与请求区间不一致，未发布此缓存。")
            self._source(job)
            temporary.replace(target)
            self._update(job_id, status="done", progress=100, duration=metadata["duration"], finished_at=now())
        except Exception as exc:
            self._update(job_id, status="cancelled" if cancel.is_set() else "failed", error=str(exc)[:600], finished_at=now())
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            for temporary_path in (temporary, progress, error_log):
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def get(self, workspace_id: str, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["workspace_id"] != workspace_id:
                raise FileNotFoundError("兼容预览任务不存在。")
            return self._public(job)

    def latest(self, workspace_id: str) -> dict | None:
        with self.lock:
            return next((self._public(job) for job in reversed(list(self.jobs.values())) if job["workspace_id"] == workspace_id), None)

    def cancel(self, workspace_id: str, job_id: str) -> dict:
        with self.lock:
            job = self.get(workspace_id, job_id)
            if job["status"] in {"queued", "running", "cancelling"}:
                self.cancel_events[job_id].set()
                self.jobs[job_id]["status"] = "cancelling"
            return self.get(workspace_id, job_id)

    def media_path(self, workspace_id: str, job_id: str) -> Path:
        with self.lock:
            job = self.get(workspace_id, job_id)
            if job["status"] != "done":
                raise ReviewConflict("兼容预览尚未完成。")
            private_job = self.jobs[job_id]
            self._source(private_job)
            path = Path(private_job["_path"])
            if not path.resolve().is_relative_to(self.store.root.resolve()) or not path.is_file():
                raise FileNotFoundError("兼容预览缓存不存在。")
            return path

    def shutdown(self) -> None:
        self.closed.set()
        for event in self.cancel_events.values():
            event.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
