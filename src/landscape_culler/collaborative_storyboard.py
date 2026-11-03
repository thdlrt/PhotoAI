"""Versioned edit proposals constrained by the user's saved review decisions.

Proposals are a separate artifact: neither generation nor approval changes a
media file, a user review, or an existing photo-selection project.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import re
import uuid
from pathlib import Path
from typing import Any

from .collaborative_review import ReviewConflict, ReviewCorrupt, ReviewStore, _natural_order, _number, _text, default_user, now
from .util import atomic_create_text, read_json, write_json

PROPOSAL_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_PROPOSAL_CLIPS = 2000
MAX_MODEL_ITEMS = 150
EVIDENCE = "仅根据素材索引、已有视觉评价与人工选择编排；本步骤未重新观看图片、连续视频或听取声音。"


class StoryboardStore:
    def __init__(self, review_store: ReviewStore):
        self.review_store = review_store
        self.lock = review_store.lock

    def _root(self, workspace_id: str, proposal_id: str | None = None) -> Path:
        root = self.review_store.path(workspace_id).parent / "storyboards"
        if proposal_id is not None:
            if not PROPOSAL_ID.fullmatch(proposal_id):
                raise ValueError("剪辑草案编号无效。")
            root /= proposal_id
        return root

    def _load(self, workspace_id: str, proposal_id: str, revision: int | None = None) -> dict:
        root = self._root(workspace_id, proposal_id)
        if revision is not None and (type(revision) is not int or revision < 0):
            raise ValueError("草案版本必须是非负整数。")
        path = root / "latest.json" if revision is None else root / "versions" / f"{revision:06d}.json"
        if not path.is_file():
            raise FileNotFoundError("剪辑草案或指定历史版本不存在。")
        try:
            proposal = read_json(path)
            if (not isinstance(proposal, dict) or proposal.get("schema") != 1
                    or proposal.get("proposal_id") != proposal_id or proposal.get("workspace_id") != workspace_id
                    or type(proposal.get("revision")) is not int
                    or type(proposal.get("workspace_revision")) is not int
                    or proposal.get("status") not in {"draft", "approved", "rejected"}
                    or not isinstance(proposal.get("chapters"), list)
                    or not isinstance(proposal.get("source_snapshot"), dict)):
                raise ValueError("schema")
            if revision is not None and proposal["revision"] != revision:
                raise ValueError("revision")
            return proposal
        except (ValueError, TypeError) as exc:
            raise ReviewCorrupt("剪辑草案记录损坏；已保留原文件，可从 versions 目录恢复历史版本。") from exc

    def _persist(self, proposal: dict) -> None:
        root = self._root(proposal["workspace_id"], proposal["proposal_id"])
        version_path = root / "versions" / f"{proposal['revision']:06d}.json"
        # History is append-only. A failed publication must never overwrite a
        # previous version while trying to recover the current pointer.
        atomic_create_text(version_path, json.dumps(proposal, ensure_ascii=False, indent=2))
        write_json(root / "latest.json", proposal)

    def _public(self, proposal: dict, workspace: dict | None = None) -> dict:
        workspace = workspace or self.review_store._load(proposal["workspace_id"])
        result = copy.deepcopy(proposal)
        result["stale"] = workspace["revision"] != proposal["workspace_revision"]
        result["current_workspace_revision"] = workspace["revision"]
        result["latest_revision"] = self._load(proposal["workspace_id"], proposal["proposal_id"])["revision"]
        result["is_latest"] = proposal["revision"] == result["latest_revision"]
        result["approval_valid"] = result["status"] == "approved" and not result["stale"] and result["is_latest"]
        result["clip_count"] = sum(len(chapter["clips"]) for chapter in result["chapters"])
        result["ready_for_editing"] = result["approval_valid"] and not result["warnings"]
        return result

    def get(self, workspace_id: str, proposal_id: str, revision: int | None = None) -> dict:
        with self.lock:
            return self._public(self._load(workspace_id, proposal_id, revision))

    def list(self, workspace_id: str) -> list[dict]:
        with self.lock:
            workspace = self.review_store._load(workspace_id)
            result = []
            for path in self._root(workspace_id).glob("*/latest.json"):
                if not PROPOSAL_ID.fullmatch(path.parent.name):
                    continue
                try:
                    proposal = self._public(self._load(workspace_id, path.parent.name), workspace)
                    result.append({key: proposal[key] for key in (
                        "proposal_id", "revision", "workspace_revision", "title", "status", "source", "model",
                        "stale", "approval_valid", "clip_count", "updated_at", "warnings")})
                except (ReviewCorrupt, OSError, KeyError) as exc:
                    result.append({"proposal_id": path.parent.name, "error": str(exc)})
            return sorted(result, key=lambda item: item.get("updated_at", ""), reverse=True)

    def history(self, workspace_id: str, proposal_id: str) -> list[dict]:
        with self.lock:
            self._load(workspace_id, proposal_id)
            result = []
            for path in sorted((self._root(workspace_id, proposal_id) / "versions").glob("*.json"), reverse=True):
                if not path.stem.isdigit():
                    continue
                proposal = self._load(workspace_id, proposal_id, int(path.stem))
                result.append({key: proposal[key] for key in ("revision", "workspace_revision", "title", "status", "updated_at")})
            return result

    @staticmethod
    def _selected(workspace: dict, include_unreviewed: bool) -> list[tuple[str, dict, dict, dict]]:
        selected = []
        for asset_id, asset in workspace["assets"].items():
            user = default_user() | workspace["user_reviews"].get(asset_id, {})
            assistant = workspace["assistant_reviews"].get(asset_id, {})
            if user["status"] == "reject" or user["status"] == "unreviewed" and not include_unreviewed:
                continue
            selected.append((asset_id, asset, user, assistant))
        return selected

    def context(self, workspace_id: str, workspace_revision: int, *, include_unreviewed: bool = False) -> dict:
        """A metadata-only brief safe to give a semantic-planning model/agent."""
        with self.lock:
            workspace = self.review_store._load(workspace_id)
            self.review_store._expect(workspace["revision"], workspace_revision)
            selected = self._selected(workspace, include_unreviewed)
            return {"workspace_id": workspace_id, "workspace_revision": workspace_revision,
                    "evidence": EVIDENCE, "include_unreviewed": include_unreviewed,
                    "excluded_asset_ids": [key for key, user in workspace["user_reviews"].items() if user.get("status") == "reject"],
                    "items": [{"asset_id": asset_id, "name": asset["name"], "relative_path": asset["relative_path"],
                               "kind": asset["kind"], "duration": asset.get("duration"), "available": asset.get("available", True),
                               "user": copy.deepcopy(user), "assistant": copy.deepcopy(assistant)}
                              for asset_id, asset, user, assistant in selected]}

    def _validate(self, workspace: dict, chapters: Any, *, source: str, include_unreviewed: bool) -> tuple[list, list, dict]:
        if not isinstance(chapters, list) or not 1 <= len(chapters) <= 100:
            raise ValueError("草案需要 1–100 个章节。")
        cleaned, warnings, seen_clips, seen_assets, manual_order = [], [], set(), set(), []
        kept_segments: dict[str, set[tuple]] = {}
        total = 0
        chapter_ids = set()
        for index, chapter in enumerate(chapters):
            if not isinstance(chapter, dict) or set(chapter) - {"id", "title", "summary", "clips"}:
                raise ValueError("章节字段仅支持 id、title、summary、clips。")
            chapter_id = _text(chapter.get("id", f"chapter-{index + 1}"), "章节编号", 80)
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", chapter_id) or chapter_id in chapter_ids:
                raise ValueError("章节编号须为不重复的英文字母、数字、下划线或短横线。")
            chapter_ids.add(chapter_id)
            title = _text(chapter.get("title", ""), "章节标题", 200).strip()
            if not title:
                raise ValueError("章节标题不能为空。")
            clips = chapter.get("clips")
            if not isinstance(clips, list) or not clips:
                raise ValueError("每个章节至少包含一个素材片段。")
            clean_clips = []
            for clip in clips:
                total += 1
                if total > MAX_PROPOSAL_CLIPS:
                    raise ValueError(f"单份草案最多支持 {MAX_PROPOSAL_CLIPS} 个片段。")
                if not isinstance(clip, dict) or set(clip) - {"asset_id", "source_in", "source_out", "photo_duration", "reason", "notes"}:
                    raise ValueError("片段字段无效。")
                asset_id = clip.get("asset_id")
                if not isinstance(asset_id, str) or asset_id not in workspace["assets"]:
                    raise ValueError("草案引用了未知素材编号。")
                asset = workspace["assets"][asset_id]
                user = default_user() | workspace["user_reviews"].get(asset_id, {})
                if user["status"] == "reject":
                    raise ValueError(f"草案不能使用人工标记为不用的素材：{asset['name']}")
                if user["status"] == "unreviewed" and not include_unreviewed:
                    raise ValueError(f"草案包含未审素材；请先选择候选，或显式允许未审素材：{asset['name']}")
                if user["chapter"] and user["chapter"] != title:
                    raise ValueError(f"请保留人工章节「{user['chapter']}」：{asset['name']}")
                if asset_id not in seen_assets and user["order"] is not None:
                    manual_order.append(user["order"])
                seen_assets.add(asset_id)
                if user["status"] == "unreviewed":
                    warnings.append(f"未审素材待确认：{asset['name']}")
                if not asset.get("available", True):
                    warnings.append(f"原片暂不可用：{asset['name']}")
                if asset.get("source_changed"):
                    warnings.append(f"原片已发生变化，请重新核对区间：{asset['name']}")
                start, end = clip.get("source_in"), clip.get("source_out")
                if (start is None) != (end is None):
                    raise ValueError("源入点与源出点必须同时填写或同时留空。")
                clean = {"asset_id": asset_id, "source_in": None, "source_out": None,
                         "reason": _text(clip.get("reason", ""), "编排理由", 4000),
                         "notes": _text(clip.get("notes", ""), "片段备注", 10000)}
                if asset["kind"] == "photo":
                    if start is not None:
                        raise ValueError("照片不能填写视频源入出点。")
                    clean["photo_duration"] = _number(clip.get("photo_duration", 4), "照片展示秒数", minimum=0.1, maximum=120)
                elif start is None:
                    if user["segments"]:
                        raise ValueError(f"请保留人工已选源片段，不能改为未定区间：{asset['name']}")
                    warnings.append(f"视频源区间待标注：{asset['name']}")
                else:
                    validated = self.review_store._segments(workspace, asset_id, [{"in": start, "out": end}])[0]
                    start, end = validated["in"], validated["out"]
                    pair = (round(start, 6), round(end, 6))
                    manual = {(round(v["in"], 6), round(v["out"], 6)) for v in user["segments"]}
                    if manual and pair not in manual:
                        raise ValueError(f"不能改写已确认的人工源区间：{asset['name']}")
                    if source == "configured-model" and not manual:
                        assistant = workspace["assistant_reviews"].get(asset_id, {})
                        allowed = {(round(v["in"], 6), round(v["out"], 6)) for v in assistant.get("segments", [])}
                        if pair not in allowed:
                            raise ValueError(f"语义编排模型不能编造未经审看确认的源区间：{asset['name']}")
                    kept_segments.setdefault(asset_id, set()).add(pair)
                    clean.update(source_in=start, source_out=end)
                signature = (asset_id, clean["source_in"], clean["source_out"])
                if signature in seen_clips:
                    raise ValueError(f"同一素材片段重复出现：{asset['name']}")
                seen_clips.add(signature)
                clean_clips.append(clean)
            cleaned.append({"id": chapter_id, "title": title,
                            "summary": _text(chapter.get("summary", ""), "章节说明", 4000), "clips": clean_clips})
        if manual_order != sorted(manual_order):
            raise ValueError("草案顺序与已保存的人工顺序冲突，请保留人工顺序。")
        required = {asset_id for asset_id, user in workspace["user_reviews"].items() if user.get("status") == "keep"}
        omitted = required - seen_assets
        if omitted:
            names = "、".join(workspace["assets"][key]["name"] for key in sorted(omitted))
            raise ValueError(f"草案遗漏人工必用素材：{names[:400]}")
        for asset_id in required:
            manual = {(round(v["in"], 6), round(v["out"], 6)) for v in workspace["user_reviews"][asset_id].get("segments", [])}
            if manual - kept_segments.get(asset_id, set()):
                raise ValueError(f"草案遗漏必用素材的人工片段：{workspace['assets'][asset_id]['name']}")
        snapshot = {asset_id: {"name": workspace["assets"][asset_id]["name"], "path": workspace["assets"][asset_id]["path"],
                               "kind": workspace["assets"][asset_id]["kind"], "size": workspace["assets"][asset_id]["size"],
                               "mtime_ns": workspace["assets"][asset_id]["mtime_ns"],
                               "user": default_user() | copy.deepcopy(workspace["user_reviews"].get(asset_id, {}))}
                    for asset_id in seen_assets}
        return cleaned, list(dict.fromkeys(warnings)), snapshot

    def import_proposal(self, workspace_id: str, workspace_revision: int, chapters: list, *, title: str = "", brief: str = "",
                        evidence: str = "", include_unreviewed: bool = False, source: str = "external-agent", model: str | None = None) -> dict:
        if source not in {"rules", "configured-model", "external-agent"}:
            raise ValueError("草案来源无效。")
        with self.lock:
            workspace = self.review_store._load(workspace_id)
            self.review_store._expect(workspace["revision"], workspace_revision)
            cleaned, warnings, snapshot = self._validate(workspace, chapters, source=source, include_unreviewed=include_unreviewed)
            proposal = {"schema": 1, "proposal_id": uuid.uuid4().hex, "workspace_id": workspace_id, "revision": 0,
                        "workspace_revision": workspace_revision, "title": _text(title or workspace["name"] + " · 剪辑草案", "草案标题", 300),
                        "brief": _text(brief, "编排要求", 10000), "status": "draft", "source": source,
                        "model": _text(model, "模型名称", 200) if model is not None else None,
                        "evidence": _text(evidence or EVIDENCE, "编排依据", 4000), "include_unreviewed": bool(include_unreviewed),
                        "chapters": cleaned, "warnings": warnings, "source_snapshot": snapshot,
                        "created_at": now(), "updated_at": now()}
            self._persist(proposal)
            return self._public(proposal, workspace)

    def create_rules(self, workspace_id: str, workspace_revision: int, *, title: str = "", brief: str = "",
                     include_unreviewed: bool = False) -> dict:
        with self.lock:
            workspace = self.review_store._load(workspace_id)
            self.review_store._expect(workspace["revision"], workspace_revision)
            selected = self._selected(workspace, include_unreviewed)
            selected.sort(key=lambda row: (row[2]["order"] is None, row[2]["order"] or 0,
                                          row[3].get("order", 1000000), _natural_order(row[1]["relative_path"])))
            if not selected:
                raise ValueError("还没有人工必用/候选素材；请先审片，或显式包含未审素材生成待确认初稿。")
            chapters = []
            for asset_id, asset, user, assistant in selected:
                folder = Path(asset["relative_path"]).parent.as_posix()
                chapter_title = user["chapter"] or assistant.get("chapter") or ("待分章" if folder == "." else folder[:200])
                if not chapters or chapters[-1]["title"] != chapter_title:
                    chapters.append({"title": chapter_title, "summary": "按人工章节、顺序与已有类别整理，等待审核。", "clips": []})
                for segment in user["segments"] or [{"in": None, "out": None, "note": ""}]:
                    clip = {"asset_id": asset_id, "source_in": segment["in"], "source_out": segment["out"],
                            "reason": "人工必用，保留已选片段。" if user["status"] == "keep" else "人工候选，待确认取舍。" if user["status"] == "maybe" else "尚未审片，作为待确认候选。",
                            "notes": segment.get("note", "")}
                    if asset["kind"] == "photo":
                        clip["photo_duration"] = 4
                    chapters[-1]["clips"].append(clip)
            return self.import_proposal(workspace_id, workspace_revision, chapters, title=title, brief=brief, source="rules",
                                        evidence="规则初稿：按人工选择、人工章节/顺序和已有类别组织；没有调用模型或重新审看素材。",
                                        include_unreviewed=include_unreviewed)

    def update(self, workspace_id: str, proposal_id: str, revision: int, workspace_revision: int, changes: dict) -> dict:
        if not isinstance(changes, dict) or set(changes) - {"title", "brief", "chapters", "status"}:
            raise ValueError("草案编辑仅支持 title、brief、chapters、status。")
        with self.lock:
            workspace = self.review_store._load(workspace_id)
            self.review_store._expect(workspace["revision"], workspace_revision)
            proposal = self._load(workspace_id, proposal_id)
            self.review_store._expect(proposal["revision"], revision)
            requested_status = changes.get("status")
            if requested_status is not None and requested_status not in {"draft", "approved", "rejected"}:
                raise ValueError("草案状态无效。")
            if requested_status == "approved" and proposal["workspace_revision"] != workspace_revision:
                raise ReviewConflict("草案对应的人工选择已变化。请先重新生成或保存并复核新版本，再批准。")
            chapters = changes.get("chapters", proposal["chapters"])
            clean, warnings, snapshot = self._validate(workspace, chapters, source="external-agent",
                                                       include_unreviewed=proposal["include_unreviewed"])
            if "title" in changes:
                proposal["title"] = _text(changes["title"], "草案标题", 300)
            if "brief" in changes:
                proposal["brief"] = _text(changes["brief"], "编排要求", 10000)
            proposal.update(chapters=clean, warnings=warnings, source_snapshot=snapshot, workspace_revision=workspace_revision,
                            revision=revision + 1, updated_at=now())
            proposal["status"] = requested_status or "draft"
            self._persist(proposal)
            return self._public(proposal, workspace)

    def export(self, workspace_id: str, proposal_id: str, revision: int | None = None) -> dict:
        proposal = self.get(workspace_id, proposal_id, revision=revision)
        clips = []
        for chapter_index, chapter in enumerate(proposal["chapters"]):
            for clip in chapter["clips"]:
                original = proposal["source_snapshot"][clip["asset_id"]]
                clips.append({"order": len(clips), "chapter": chapter["title"], "chapter_index": chapter_index,
                              "asset_id": clip["asset_id"], "path": original["path"], "kind": original["kind"],
                              "source_in": clip["source_in"], "source_out": clip["source_out"],
                              "photo_duration": clip.get("photo_duration"), "reason": clip["reason"], "notes": clip["notes"],
                              "user_status": original["user"]["status"], "user_notes": original["user"]["notes"]})
        return proposal | {"exported_at": now(), "time_basis": "original-source-seconds", "clips": clips}


def storyboard_csv(proposal: dict) -> str:
    stream = io.StringIO(newline="")
    fields = ["order", "chapter", "chapter_index", "asset_id", "path", "kind", "source_in", "source_out", "photo_duration",
              "reason", "notes", "user_status", "user_notes", "proposal_id", "proposal_revision", "proposal_status", "stale"]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for clip in proposal["clips"]:
        row = clip | {"proposal_id": proposal["proposal_id"], "proposal_revision": proposal["revision"],
                      "proposal_status": proposal["status"], "stale": proposal["stale"]}
        row = {key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
               for key, value in row.items()}
        writer.writerow(row)
    return "\ufeff" + stream.getvalue()
