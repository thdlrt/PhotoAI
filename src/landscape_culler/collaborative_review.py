"""Portable, non-destructive photo/video review workspaces.

The media index, assistant suggestions and human decisions are separate records.
Reading a workspace never creates or migrates it; only explicit edits persist.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .constants import READABLE_RAW_EXTENSIONS
from .util import atomic_write_text, read_json, runs_root, write_json

PHOTO_EXTENSIONS = READABLE_RAW_EXTENSIONS | {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".webm"}
STATUSES = {"keep", "maybe", "reject", "unreviewed"}
ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


class ReviewConflict(ValueError):
    """The caller edited an older revision."""


class ReviewCorrupt(ValueError):
    """A saved workspace cannot be read without losing user data."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _canonical(path: Path) -> str:
    value = str(path.resolve())
    return value.casefold() if os.name == "nt" else value


def _natural_order(value: str) -> tuple:
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", value))


def _number(value: Any, label: str, *, minimum: float = 0, maximum: float = 1e9) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}必须是有限数值。")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label}超出允许范围。")
    return float(value)


def _text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}必须是至多 {maximum} 字的文本。")
    return value


def default_user() -> dict[str, Any]:
    return {"revision": 0, "status": "unreviewed", "quality": 0, "beauty": 0, "memory": 0,
            "chapter": "", "notes": "", "issues": [], "segments": [], "rotation": 0, "order": None}


def probe_video(path: Path) -> dict[str, Any]:
    # Imported lazily: photo review does not depend on FFmpeg or the AI runtime.
    from .timelapse import find_media_tool
    executable = find_media_tool("ffprobe")
    if not executable:
        raise ValueError("未找到 FFprobe；请在延时摄影设置中配置 FFmpeg 工具后再标注视频区间。")
    try:
        result = subprocess.run([executable, "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "format=duration:stream=width,height,duration,avg_frame_rate",
                                 "-of", "json", str(path)], capture_output=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("视频信息读取失败或超时。") from exc
    if result.returncode:
        raise ValueError("无法读取视频时长，文件可能损坏或格式不受支持。")
    try:
        payload = json.loads(result.stdout)
        stream = payload.get("streams", [{}])[0]
        duration = float(stream.get("duration") or payload.get("format", {}).get("duration"))
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration")
        return {"duration": duration, "width": stream.get("width"), "height": stream.get("height"),
                "frame_rate": stream.get("avg_frame_rate")}
    except (ValueError, TypeError, IndexError, KeyError) as exc:
        raise ValueError("视频未提供可靠时长，暂不能保存源入出点。") from exc


class ReviewStore:
    def __init__(self, data_dir: Path, *, video_probe: Callable[[Path], dict] | None = None):
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / "collaborative-review"
        self.video_probe = video_probe or probe_video
        with _LOCKS_LOCK:
            self.lock = _LOCKS.setdefault(_canonical(self.root), threading.RLock())

    def path(self, workspace_id: str) -> Path:
        if not ID_PATTERN.fullmatch(workspace_id):
            raise ValueError("审片工程编号无效。")
        return self.root / workspace_id / "workspace.json"

    def _load(self, workspace_id: str) -> dict:
        path = self.path(workspace_id)
        if not path.is_file():
            raise FileNotFoundError("审片工程不存在。")
        try:
            state = read_json(path)
            if (not isinstance(state, dict) or state.get("schema") != 1
                    or state.get("workspace_id") != workspace_id
                    or type(state.get("revision")) is not int or state["revision"] < 0
                    or any(not isinstance(state.get(k), str) for k in ("input_root", "name", "created_at", "updated_at"))
                    or any(not isinstance(state.get(k), dict) for k in ("assets", "assistant_reviews", "user_reviews"))):
                raise ValueError("schema")
            for asset_id, asset in state["assets"].items():
                if (not ID_PATTERN.fullmatch(asset_id) or not isinstance(asset, dict) or asset.get("id") != asset_id
                        or any(not isinstance(asset.get(k), str) for k in ("path", "relative_path", "name"))
                        or asset.get("kind") not in {"photo", "video"}
                        or any(type(asset.get(k)) is not int for k in ("size", "mtime_ns"))):
                    raise ValueError("asset")
            for key in ("assistant_reviews", "user_reviews"):
                if set(state[key]) - set(state["assets"]) or any(not isinstance(v, dict) for v in state[key].values()):
                    raise ValueError(key)
            for user in state["user_reviews"].values():
                if (type(user.get("revision")) is not int or user["revision"] < 0
                        or user.get("status") not in STATUSES or not isinstance(user.get("segments"), list)):
                    raise ValueError("user review")
            return state
        except (ValueError, TypeError, KeyError) as exc:
            raise ReviewCorrupt("审片记录损坏或版本不支持；已保留原文件，请恢复 workspace.previous.json。") from exc

    def _save(self, state: dict, *, new: bool = False) -> None:
        path = self.path(state["workspace_id"])
        if not new:
            # Validate the old file before backing it up; never turn corruption into an empty project.
            self._load(state["workspace_id"])
            atomic_write_text(path.with_name("workspace.previous.json"), path.read_text(encoding="utf-8"))
            state["revision"] += 1
        state["updated_at"] = now()
        write_json(path, state)

    @staticmethod
    def _expect(actual: int, expected: int) -> None:
        if isinstance(expected, bool) or not isinstance(expected, int) or actual != expected:
            raise ReviewConflict("记录已在另一窗口更新。请重新读取后合并修改。")

    def _public(self, state: dict) -> dict:
        result = {k: copy.deepcopy(v) for k, v in state.items() if k not in {"assets", "assistant_reviews", "user_reviews"}}
        result["items"] = [copy.deepcopy(asset) | {
            "assistant": copy.deepcopy(state["assistant_reviews"].get(asset_id)),
            "user": default_user() | copy.deepcopy(state["user_reviews"].get(asset_id, {}))}
            for asset_id, asset in state["assets"].items()]
        return result

    def get(self, workspace_id: str) -> dict:
        with self.lock:
            return self._public(self._load(workspace_id))

    def list(self) -> list[dict]:
        result = []
        if not self.root.exists():
            return result
        for path in self.root.glob("*/workspace.json"):
            if not ID_PATTERN.fullmatch(path.parent.name):
                continue
            try:
                state = self._load(path.parent.name)
                result.append({k: state[k] for k in ("workspace_id", "name", "input_root", "revision", "updated_at")} | {
                    "count": len(state["assets"]), "reviewed": sum(v.get("status") in {"keep", "maybe", "reject"}
                                                                               for v in state["user_reviews"].values())})
            except (ReviewCorrupt, OSError) as exc:
                result.append({"workspace_id": path.parent.name, "name": path.parent.name, "error": str(exc)})
        return sorted(result, key=lambda item: item.get("updated_at", ""), reverse=True)

    def _scan(self, root: Path) -> dict:
        assets = {}
        def scan_error(error: OSError) -> None:
            raise error
        for current, directories, files in os.walk(root, onerror=scan_error):
            parent = Path(current)
            directories[:] = sorted(name for name in directories if not name.startswith(".")
                                    and not (parent / name).is_symlink()
                                    and not (parent / name).is_junction()
                                    and not (parent / name).resolve().is_relative_to(self.root))
            for filename in sorted(files):
                path = parent / filename
                suffix = path.suffix.lower()
                if suffix not in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS or path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    continue
                relative = path.relative_to(root).as_posix()
                asset_id = _identity(_canonical(root) + "\n" + (relative.casefold() if os.name == "nt" else relative))
                stat = path.stat()
                assets[asset_id] = {"id": asset_id, "path": str(resolved), "relative_path": relative,
                                    "name": filename, "kind": "video" if suffix in VIDEO_EXTENSIONS else "photo",
                                    "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                    "duration": None, "available": True, "source_changed": False}
                if len(assets) > 50000:
                    raise ValueError("单个审片工程最多支持 50000 个素材，请按拍摄批次分目录。")
        return assets

    def create(self, input_root: str, name: str | None = None) -> dict:
        root = Path(input_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("素材路径必须是文件夹。")
        workspace_id = _identity(_canonical(root))
        with self.lock:
            if self.path(workspace_id).exists():
                return self._public(self._load(workspace_id))
            state = {"schema": 1, "workspace_id": workspace_id, "name": _text(name or root.name or str(root), "工程名称", 200),
                     "input_root": str(root), "revision": 0, "created_at": now(), "updated_at": now(),
                     "assets": self._scan(root), "assistant_reviews": {}, "user_reviews": {}}
            self._save(state, new=True)
            return self._public(state)

    def rescan(self, workspace_id: str, revision: int) -> dict:
        with self.lock:
            state = self._load(workspace_id)
            self._expect(state["revision"], revision)
            root = Path(state["input_root"])
            if not root.is_dir():
                raise ValueError("原片目录不可访问；保留当前索引与选择。")
            scanned = self._scan(root)
            for asset_id, old in state["assets"].items():
                if asset_id not in scanned:
                    scanned[asset_id] = old | {"available": False}
                elif any(old.get(k) != scanned[asset_id].get(k) for k in ("size", "mtime_ns")):
                    scanned[asset_id]["source_changed"] = True
                else:
                    scanned[asset_id] = old | {"available": True}
            state["assets"] = scanned
            self._save(state)
            return self._public(state)

    def media_path(self, workspace_id: str, asset_id: str, *, state: dict | None = None) -> Path:
        state = state or self._load(workspace_id)
        asset = state["assets"].get(asset_id)
        if not asset:
            raise FileNotFoundError("素材编号不存在。")
        root = Path(state["input_root"]).resolve()
        path = Path(asset["path"])
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("素材路径超出当前工程。")
        if resolved.suffix.lower() not in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
            raise ValueError("不支持的素材类型。")
        stat = resolved.stat()
        if stat.st_size != asset["size"] or stat.st_mtime_ns != asset["mtime_ns"]:
            raise ReviewConflict("原片已发生变化，请重新扫描后核对已有选择。")
        return resolved

    def _duration(self, state: dict, asset_id: str) -> float:
        asset = state["assets"][asset_id]
        if asset["kind"] != "video":
            raise ValueError("只有视频素材支持源入出点。")
        if not asset.get("duration"):
            asset.update(self.video_probe(self.media_path(state["workspace_id"], asset_id, state=state)))
        return _number(asset["duration"], "视频时长", minimum=0.001)

    def _segments(self, state: dict, asset_id: str, value: Any) -> list:
        if not isinstance(value, list) or len(value) > 100:
            raise ValueError("视频片段必须是至多 100 项的列表。")
        if not value:
            return []
        duration = self._duration(state, asset_id)
        result = []
        for segment in value:
            if not isinstance(segment, dict) or set(segment) - {"in", "out", "note"}:
                raise ValueError("视频片段字段无效。")
            start = _number(segment.get("in"), "源入点", maximum=duration)
            end = _number(segment.get("out"), "源出点", maximum=duration)
            if end <= start:
                raise ValueError("源出点必须晚于源入点。")
            result.append({"in": start, "out": end, "note": _text(segment.get("note", ""), "片段备注", 2000)})
        return result

    def update_item(self, workspace_id: str, asset_id: str, revision: int, changes: dict) -> dict:
        with self.lock:
            state = self._load(workspace_id)
            if asset_id not in state["assets"]:
                raise FileNotFoundError("素材编号不存在。")
            user = default_user() | state["user_reviews"].get(asset_id, {})
            self._expect(user["revision"], revision)
            if set(changes) - (set(default_user()) - {"revision"}):
                raise ValueError("包含未知用户选择字段。")
            for key, value in changes.items():
                if key == "status":
                    value = "keep" if value == "must" else value
                    if value not in STATUSES:
                        raise ValueError("选择必须为必用、候选、不用或未审。")
                elif key in {"quality", "beauty", "memory"}:
                    _number(value, "评分", maximum=3)
                    if int(value) != value:
                        raise ValueError("评分必须为 0–3 的整数，0 表示未评分。")
                elif key in {"chapter", "notes"}:
                    value = _text(value, "章节" if key == "chapter" else "备注", 200 if key == "chapter" else 10000)
                elif key == "issues":
                    if not isinstance(value, list) or len(value) > 30:
                        raise ValueError("问题标签至多 30 项。")
                    value = [_text(v, "问题标签", 200) for v in value]
                elif key == "segments":
                    value = self._segments(state, asset_id, value)
                elif key == "rotation":
                    if isinstance(value, bool) or value not in {0, 90, 180, 270}:
                        raise ValueError("预览旋转只支持 0、90、180、270 度。")
                elif key == "order" and value is not None:
                    _number(value, "顺序", maximum=1000000)
                    if int(value) != value:
                        raise ValueError("顺序必须为整数。")
                user[key] = value
            user["revision"] += 1
            state["user_reviews"][asset_id] = user
            self._save(state)
            return self._public(state)

    def _suggestion(self, state: dict, asset_id: str, value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("助手建议必须为对象。")
        status = value.get("status", "maybe")
        if status == "must":
            status = "keep"
        if status not in STATUSES:
            raise ValueError("助手建议状态无效。")
        result = {"status": status, "reason": _text(value.get("reason", ""), "建议理由", 4000),
                  "evidence": _text(value.get("evidence", ""), "审片依据", 4000),
                  "source": _text(value.get("source", "import"), "建议来源", 200),
                  "chapter": _text(value.get("chapter", ""), "建议章节", 200),
                  "updated_at": now()}
        if "order" in value:
            result["order"] = int(_number(value["order"], "建议顺序", maximum=1000000))
        if "score" in value and value["score"] is not None:
            result["score"] = _number(value["score"], "建议分数", minimum=-10000, maximum=10000)
        if "segments" in value:
            result["segments"] = self._segments(state, asset_id, value["segments"])
        if "model" in value:
            result["model"] = _text(value["model"], "模型名称", 200)
        if "sample_times" in value:
            times = value["sample_times"]
            if not isinstance(times, list) or len(times) > 8:
                raise ValueError("采样时间数量无效。")
            result["sample_times"] = [_number(t, "采样时间") for t in times]
        return result

    def suggestions(self, workspace_id: str, revision: int, items: dict) -> dict:
        with self.lock:
            state = self._load(workspace_id)
            self._expect(state["revision"], revision)
            if not isinstance(items, dict) or set(items) - set(state["assets"]):
                raise ValueError("建议包含未知素材编号。")
            cleaned = {asset_id: self._suggestion(state, asset_id, value) for asset_id, value in items.items()}
            state["assistant_reviews"].update(cleaned)
            self._save(state)
            return self._public(state)

    def organize(self, workspace_id: str, revision: int, *, existing_ai: bool = False) -> dict:
        with self.lock:
            state = self._load(workspace_id)
            self._expect(state["revision"], revision)
            scores = {}
            if existing_ai:
                for result_path in sorted(runs_root(self.data_dir).glob("*/results.json")):
                    try:
                        run = read_json(result_path)
                        for row in run.get("results", []):
                            if isinstance(row, dict) and row.get("path"):
                                scores[_canonical(Path(row["path"]))] = row | {"run_id": run.get("run_id", result_path.parent.name)}
                    except (OSError, ValueError, TypeError, AttributeError):
                        continue
            items = {}
            for order, (asset_id, asset) in enumerate(sorted(state["assets"].items(), key=lambda pair: _natural_order(pair[1]["relative_path"]))):
                parent = Path(asset["relative_path"]).parent.as_posix()
                suggestion = {"source": "folder-order", "status": "maybe", "order": order,
                              "chapter": "" if parent == "." else parent[:200],
                              "reason": "按素材所在目录和文件名组织，等待你确认取舍。",
                              "evidence": "依据文件目录与自然顺序；未作视觉或声音评审。"}
                row = scores.get(_canonical(Path(asset["path"])))
                if row:
                    rating = row.get("rating", 0)
                    try:
                        score = _number(row.get("score"), "既有 AI 评分", minimum=-10000, maximum=10000)
                    except ValueError:
                        score = None
                    suggestion.update(source="existing-photo-ai", status="keep" if isinstance(rating, (int, float)) and rating >= 3 else "maybe",
                                      reason=f"沿用选片工程 {row['run_id']} 的 AI 评分；请结合本次用途复核。",
                                      evidence="既有选片结果；本次没有重新读取图像或判断视频。", score=score)
                items[asset_id] = self._suggestion(state, asset_id, suggestion)
            state["assistant_reviews"].update(items)
            self._save(state)
            return self._public(state)

    def merge_vision(self, workspace_id: str, asset_id: str, fingerprint: tuple, suggestion: dict) -> None:
        """Merge a completed model result without touching concurrent human edits."""
        with self.lock:
            state = self._load(workspace_id)
            asset = state["assets"].get(asset_id)
            if not asset or (asset["size"], asset["mtime_ns"]) != fingerprint:
                raise ReviewConflict("素材在视觉评审期间发生变化，未保存过期建议。")
            self.media_path(workspace_id, asset_id, state=state)
            state["assistant_reviews"][asset_id] = self._suggestion(state, asset_id, suggestion)
            self._save(state)

    def export(self, workspace_id: str, *, selected_only: bool = True) -> dict:
        workspace = self.get(workspace_id)
        clips = []
        for item in workspace["items"]:
            user = item["user"]
            if selected_only and user["status"] not in {"keep", "maybe"}:
                continue
            segments = user["segments"] or [{"in": None, "out": None, "note": ""}]
            for index, segment in enumerate(segments):
                clips.append({"asset_id": item["id"], "path": item["path"], "kind": item["kind"],
                              "status": user["status"], "chapter": user["chapter"], "order": user["order"],
                              "segment_index": index, "source_in": segment["in"], "source_out": segment["out"],
                              "notes": user["notes"], "segment_note": segment["note"],
                              "quality": user["quality"], "beauty": user["beauty"], "memory": user["memory"],
                              "issues": user["issues"], "preview_rotation": user["rotation"],
                              "available": item["available"], "source_changed": item.get("source_changed", False)})
        clips.sort(key=lambda c: (c["order"] is None, c["order"] or 0, c["chapter"], c["path"], c["segment_index"]))
        return {"schema": 1, "workspace_id": workspace_id, "revision": workspace["revision"],
                "name": workspace["name"], "exported_at": now(), "time_basis": "original-source-seconds",
                "selected_only": selected_only, "clips": clips}


def export_csv(payload: dict) -> str:
    stream = io.StringIO(newline="")
    fields = ["asset_id", "path", "kind", "status", "chapter", "order", "segment_index", "source_in", "source_out",
              "notes", "segment_note", "quality", "beauty", "memory", "issues", "preview_rotation", "available", "source_changed"]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in payload["clips"]:
        # Neutralize spreadsheet formulas in user notes, filenames and imported text.
        safe = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v for k, v in row.items()}
        safe = {k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in safe.items()}
        writer.writerow(safe)
    return "\ufeff" + stream.getvalue()
