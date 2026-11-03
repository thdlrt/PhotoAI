"""Selection-only handoff without loading or applying develop recipes."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .constants import PROPRIETARY_RAW_EXTENSIONS
from .util import full_fingerprint, read_json, write_json
from .xmp import assert_exiftool_available, assert_lightroom_not_running


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def selection_manifest(payload: dict[str, Any], min_rating: int = 3) -> dict[str, Any]:
    """Use an allow-list so legacy develop settings never cross this boundary."""
    source_root = Path(str(payload.get("input_root") or ""))
    if not source_root.is_absolute() or not source_root.is_dir():
        raise ValueError("原片目录不存在或不是绝对路径。")
    source_root = source_root.resolve()
    items: list[dict[str, Any]] = []
    paths_seen: set[str] = set()
    for index, item in enumerate(payload.get("results", [])):
        rating = int(item.get("effective_rating", item.get("rating", 0)))
        if item.get("excluded") or item.get("deleted") or rating < min_rating:
            continue
        source = Path(str(item.get("path") or ""))
        if not source.is_absolute() or not _inside(source, source_root):
            raise ValueError(f"照片路径超出原片目录：{source.name}")
        source = source.resolve()
        key = os.path.normcase(str(source))
        if key in paths_seen:
            continue
        paths_seen.add(key)
        sidecar = source.with_suffix(".xmp")
        can_write = source.suffix.casefold() in PROPRIETARY_RAW_EXTENSIONS
        items.append({
            "index": int(item.get("index", index)),
            "path": str(source),
            "relative_path": source.relative_to(source_root).as_posix(),
            "filename": source.name,
            "rating": rating,
            "ai_rating": int(item.get("ai_rating", item.get("rating", 0))),
            "manual_override": bool(item.get("manual_override")),
            "score": item.get("score"),
            "group_id": item.get("group_id"),
            "keywords": [str(value) for value in item.get("keywords", [])],
            "exists": source.is_file(),
            "existing_xmp": sidecar.is_file(),
            "can_create_xmp": can_write and not sidecar.exists() and source.is_file(),
        })
    return {
        "schema_version": 1,
        "kind": "photoai-selection",
        "run_id": str(payload.get("run_id") or ""),
        "input_root": str(source_root),
        "review_revision": int(payload.get("review_revision", 0)),
        "min_rating": min_rating,
        "count": len(items),
        "missing_count": sum(not item["exists"] for item in items),
        "xmp_create_count": sum(item["can_create_xmp"] for item in items),
        "xmp_existing_count": sum(item["existing_xmp"] for item in items),
        "items": items,
    }


def _csv_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    # CSV files are commonly opened by Excel; preserve the literal filename.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def selection_download(manifest: dict[str, Any], format: str) -> Response:
    run_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(manifest["run_id"]))
    if format == "json":
        content = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        media_type = "application/json"
    else:
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["relative_path", "rating", "ai_rating", "manual_override", "group_id", "score", "keywords", "path"])
        for item in manifest["items"]:
            writer.writerow([
                _csv_cell(item["relative_path"]), item["rating"], item["ai_rating"],
                item["manual_override"], _csv_cell(item["group_id"]), _csv_cell(item["score"]),
                _csv_cell("; ".join(item["keywords"])), _csv_cell(item["path"]),
            ])
        content = buffer.getvalue().encode("utf-8-sig")
        media_type = "text/csv; charset=utf-8"
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="selection-{run_id}.{format}"',
        "Cache-Control": "no-store",
    })


class _CopyCancelled(Exception):
    pass


class SelectionCopyManager:
    """One background copy at a time, with durable progress and cancellation."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "selection-copies"
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.stops: dict[str, threading.Event] = {}
        self.workers: dict[str, threading.Thread] = {}

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("复制任务不存在。")
        return self.root / f"{job_id}.json"

    def _save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = _now()
        write_json(self._path(job["id"]), job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                path = self._path(job_id)
                if not path.is_file():
                    raise ValueError("复制任务不存在。")
                job = read_json(path)
                if job.get("status") in {"queued", "running", "cancelling"}:
                    job.update(status="interrupted", message="上次运行已结束；已完成的副本保留，请选择新的输出目录重试。")
                    self._save(job)
                self.jobs[job_id] = job
            return json.loads(json.dumps(job))

    def latest(self, run_id: str) -> dict[str, Any] | None:
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in paths:
            try:
                job = self.get(path.stem)
            except (OSError, ValueError):
                continue
            if job.get("run_id") == run_id:
                return job
        return None

    def start(self, manifest: dict[str, Any], output_dir: str, include_xmp: bool) -> dict[str, Any]:
        with self.lock:
            if any(worker.is_alive() for worker in self.workers.values()):
                raise ValueError("已有原片复制任务，请等待完成或取消。")
            target = Path(output_dir).expanduser()
            if not target.is_absolute() or not target.parent.is_dir():
                raise ValueError("请填写绝对输出路径，并确保它的上级目录已存在。")
            if target.exists() or target.is_symlink():
                raise ValueError("输出目录已存在，请填写一个新的目录名；不会覆盖已有文件。")
            target = target.resolve()
            source_root = Path(manifest["input_root"])
            if target == source_root or _inside(source_root, target):
                raise ValueError("输出目录不能是原片目录或其上级目录。")
            files: list[dict[str, Any]] = []
            names_seen: set[str] = set()
            for item in manifest["items"]:
                source = Path(item["path"])
                sources = [source]
                if include_xmp and source.with_suffix(".xmp").is_file():
                    sources.append(source.with_suffix(".xmp"))
                for entry in sources:
                    if not entry.is_file() or not _inside(entry, source_root):
                        raise ValueError(f"源文件不存在或路径超出原片目录：{entry.name}")
                    relative = entry.resolve().relative_to(source_root)
                    key = str(relative).casefold()
                    if key in names_seen:
                        continue
                    names_seen.add(key)
                    stat = entry.stat()
                    files.append({"source": str(entry.resolve()), "relative_path": relative.as_posix(),
                                  "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "status": "pending"})
            if not files:
                raise ValueError("没有达到所选星级的照片。")
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "kind": "selection_copy", "run_id": manifest["run_id"],
                   "review_revision": manifest["review_revision"], "min_rating": manifest["min_rating"],
                   "status": "queued", "created_at": _now(), "output_dir": str(target),
                   "photo_count": manifest["count"], "file_count": len(files), "completed_files": 0,
                   "total_bytes": sum(item["size"] for item in files), "copied_bytes": 0,
                   "percent": 0, "current_file": "", "message": "等待复制。", "files": files}
            # Reserve the new directory atomically before reporting a started job.
            target.mkdir(exist_ok=False)
            try:
                self._save(job)
            except Exception:
                target.rmdir()
                raise
            self.jobs[job_id] = job
            stop = self.stops[job_id] = threading.Event()
            worker = self.workers[job_id] = threading.Thread(target=self._run, args=(job, stop), daemon=True)
            worker.start()
            return self.get(job_id)

    def _copy_one(self, job: dict[str, Any], item: dict[str, Any], stop: threading.Event) -> None:
        source = Path(item["source"])
        stat = source.stat()
        if (stat.st_size, stat.st_mtime_ns) != (item["size"], item["mtime_ns"]):
            raise ValueError(f"源文件在复制前发生变化：{source.name}")
        target = Path(job["output_dir"]) / item["relative_path"]
        if not _inside(target, Path(job["output_dir"])):
            raise ValueError("输出路径超出本次新建目录。")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f"目标文件已存在：{target.name}")
        temporary = target.with_name(f".{target.name}.{job['id']}.partial")
        source_digest = hashlib.sha256()
        last_report = time.monotonic()
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                while block := reader.read(4 * 1024 * 1024):
                    if stop.is_set():
                        raise _CopyCancelled()
                    writer.write(block)
                    source_digest.update(block)
                    with self.lock:
                        job["copied_bytes"] += len(block)
                        job["percent"] = round(100 * job["copied_bytes"] / max(1, job["total_bytes"]), 1)
                        if time.monotonic() - last_report >= 0.5:
                            self._save(job)
                            last_report = time.monotonic()
                writer.flush()
                os.fsync(writer.fileno())
            if stop.is_set():
                raise _CopyCancelled()
            after = full_fingerprint(source)
            copied = full_fingerprint(temporary)
            expected_hash = source_digest.hexdigest()
            if (after["size"], after["mtime_ns"]) != (item["size"], item["mtime_ns"]) or after["sha256"] != expected_hash:
                raise ValueError(f"源文件在复制时发生变化：{source.name}")
            if copied["sha256"] != expected_hash or copied["size"] != item["size"]:
                raise ValueError(f"副本校验失败：{source.name}")
            if stop.is_set():
                raise _CopyCancelled()
            shutil.copystat(source, temporary)
            # A hard-link publication is atomic and fails if a target appears.
            # Both names reside in this same new output directory/filesystem.
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise ValueError(f"目标文件已存在：{target.name}") from None
            except OSError:
                # Windows rename is also no-replace; other platforms retain the
                # hard-link requirement to prevent a race overwriting a file.
                if os.name != "nt":
                    raise
                temporary.rename(target)
            item.update(status="copied", sha256=expected_hash, output_path=str(target))
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self, job: dict[str, Any], stop: threading.Event) -> None:
        try:
            with self.lock:
                job.update(status="running", message="正在复制原片并校验副本。")
                self._save(job)
            for item in job["files"]:
                if stop.is_set():
                    raise _CopyCancelled()
                with self.lock:
                    job["current_file"] = item["relative_path"]
                    self._save(job)
                self._copy_one(job, item, stop)
                with self.lock:
                    job["completed_files"] += 1
                    self._save(job)
            with self.lock:
                job.update(status="completed", percent=100, message="原片复制完成，全部副本已通过 SHA-256 校验。")
                self._save(job)
        except _CopyCancelled:
            with self.lock:
                job.update(status="cancelled", message="复制已取消；已完成的副本保留，当前未完成文件已清理。")
                self._save(job)
        except Exception as exc:
            with self.lock:
                job.update(status="failed", message=str(exc))
                self._save(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.get(job_id)
            if job["status"] not in {"queued", "running", "cancelling"}:
                return job
            self.stops[job_id].set()
            self.jobs[job_id].update(status="cancelling", message="正在取消复制。")
            self._save(self.jobs[job_id])
            return self.get(job_id)

    def shutdown(self) -> None:
        for stop in list(self.stops.values()):
            stop.set()
        for worker in list(self.workers.values()):
            worker.join(timeout=1)


class SelectionExportBody(BaseModel):
    base_revision: int = Field(ge=0)
    min_rating: int = Field(default=3, ge=1, le=5)


class SelectionCopyBody(SelectionExportBody):
    output_dir: str = Field(min_length=1)
    include_xmp: bool = True


def make_selection_export_router(
    *, data_dir: Path, mutate_token: Callable, load_run: Callable[[str], dict[str, Any]],
    run_file: Callable[[str], Path], jobs: Any, review_lock: Any, start_job: Callable,
) -> APIRouter:
    copies = SelectionCopyManager(data_dir)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        copies.shutdown()

    router = APIRouter(lifespan=lifespan)
    # Exposed for app-level shutdown/tests without introducing a second daemon.
    router.selection_copies = copies

    def snapshot(run_id: str, minimum: int, revision: int | None = None) -> tuple[dict, dict]:
        payload = load_run(run_id)
        if revision is not None and int(payload.get("review_revision", 0)) != revision:
            raise HTTPException(409, "审片选择已在别处更新，请刷新后重新导出。")
        try:
            return payload, selection_manifest(payload, minimum)
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/api/runs/{run_id}/selection-export")
    def get_selection(run_id: str, min_rating: int = Query(default=3, ge=1, le=5)):
        with review_lock:
            _payload, manifest = snapshot(run_id, min_rating)
        manifest["copy_job"] = copies.latest(run_id)
        return manifest

    @router.get("/api/runs/{run_id}/selection-export/download")
    def download(run_id: str, format: Literal["json", "csv"] = "json", min_rating: int = Query(default=3, ge=1, le=5)):
        with review_lock:
            _payload, manifest = snapshot(run_id, min_rating)
        return selection_download(manifest, format)

    @router.post("/api/runs/{run_id}/selection-export/copy", dependencies=[Depends(mutate_token)])
    def copy(run_id: str, body: SelectionCopyBody):
        with review_lock, jobs.lock:
            payload, manifest = snapshot(run_id, body.min_rating, body.base_revision)
            if jobs.blocks_photos(payload):
                raise HTTPException(409, "这个工程正在处理照片，请等待当前任务完成。")
            try:
                return copies.start(manifest, body.output_dir, body.include_xmp)
            except (ValueError, OSError) as exc:
                raise HTTPException(409, str(exc)) from exc

    @router.get("/api/selection-copies/{job_id}")
    def copy_status(job_id: str):
        try:
            return copies.get(job_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/selection-copies/{job_id}/cancel", dependencies=[Depends(mutate_token)])
    def cancel_copy(job_id: str):
        try:
            return copies.cancel(job_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/api/runs/{run_id}/selection-export/xmp", dependencies=[Depends(mutate_token)])
    def create_xmp(run_id: str, body: SelectionExportBody):
        with review_lock, jobs.lock:
            payload, manifest = snapshot(run_id, body.min_rating, body.base_revision)
            if payload.get("workflow_state") != "scored" or payload.get("needs_rescore"):
                raise HTTPException(409, "请先完成当前分组评分。")
            if jobs.blocks_photos(payload):
                raise HTTPException(409, "这个工程正在处理照片，请等待当前任务完成。")
            selected = [item for item in manifest["items"] if item["can_create_xmp"]]
            if not selected:
                raise HTTPException(409, "没有可新建的 XMP；已有旁车会保留，仅支持专有 RAW。")
            try:
                assert_lightroom_not_running()
                assert_exiftool_available()
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
            frozen = {
                "run_id": run_id, "input_root": manifest["input_root"],
                "review_revision": manifest["review_revision"], "selection_only": True,
                "results": [{"path": item["path"], "rating": item["rating"],
                             "score": item["score"] or 0.0, "group_id": item["group_id"],
                             "keywords": item["keywords"],
                             "source_fingerprint": full_fingerprint(Path(item["path"]))} for item in selected],
            }
            path = run_file(run_id).parent / f"selection-export-{uuid.uuid4().hex}.json"
            write_json(path, frozen)
            try:
                job = start_job("xmp_commit", ["write-xmp", "--results", str(path), "--commit", "--min-rating", str(body.min_rating)],
                                {"title": f"保存选片星级 · {len(selected)} 张", "run_id": run_id,
                                 "results_path": str(path), "selection_only": True, "direct": True})
            except Exception:
                path.unlink(missing_ok=True)
                raise
            return {"job": job, "create_count": len(selected),
                    "skipped_existing_count": manifest["xmp_existing_count"], "selection_only": True}

    return router
