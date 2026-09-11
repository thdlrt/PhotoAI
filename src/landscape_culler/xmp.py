from __future__ import annotations

import hashlib
import html
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .constants import PROPRIETARY_RAW_EXTENSIONS
from .develop import xmp_settings_for_recipe
from .util import atomic_create_text, quick_fingerprint, read_json, write_json


def assert_lightroom_not_running() -> None:
    """Fail closed if Lightroom's state cannot be established."""

    if os.name != "nt":
        return
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Lightroom.exe", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法确认 Lightroom 是否关闭，已拒绝写入 XMP。") from exc
    if completed.returncode != 0:
        raise RuntimeError("无法确认 Lightroom 是否关闭，已拒绝写入 XMP。")
    if '"lightroom.exe"' in completed.stdout.casefold():
        raise RuntimeError("Lightroom Classic 正在运行。请先退出 Lightroom，再提交 XMP。")


def assert_exiftool_available() -> Path:
    executable = Path(os.environ.get("PHOTO_AI_EXIFTOOL", ""))
    if not executable.is_file():
        raise RuntimeError("ExifTool 不可用，已拒绝写入 XMP。")
    return executable


def build_xmp(rating: int, keywords: list[str], develop: dict | None = None) -> str:
    if rating not in {1, 2, 3, 4, 5}:
        raise ValueError("XMP 星级必须为 1 到 5。")
    unique_keywords = list(dict.fromkeys(keyword.strip() for keyword in keywords if keyword.strip()))
    subject = "".join(f"<rdf:li>{html.escape(keyword)}</rdf:li>" for keyword in unique_keywords)
    hierarchical = "".join(f"<rdf:li>{html.escape(keyword)}</rdf:li>" for keyword in unique_keywords if "|" in keyword)
    develop_attributes = ""
    if develop:
        settings = xmp_settings_for_recipe(develop)
        develop_attributes = "\n" + "\n".join(
            f'    crs:{key}="{html.escape(str(value), quote=True)}"'
            for key, value in settings.items()
        )
    return f'''<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Landscape AI Culler 0.1.0">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:lr="http://ns.adobe.com/lightroom/1.0/"
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    xmp:Rating="{rating}"{develop_attributes}>
   <dc:subject><rdf:Bag>{subject}</rdf:Bag></dc:subject>
   <lr:hierarchicalSubject><rdf:Bag>{hierarchical}</rdf:Bag></lr:hierarchicalSubject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
'''


def validate_xmp(text: str) -> None:
    xml_body = "\n".join(line for line in text.splitlines() if not line.startswith("<?xpacket"))
    root = ET.fromstring(xml_body)
    if not root.tag.endswith("xmpmeta"):
        raise ValueError("生成的 sidecar 不是有效的 Adobe XMP 文档。")


def validate_xmp_with_exiftool(path: Path, expected_rating: int, develop: dict | None = None) -> None:
    executable = assert_exiftool_available()
    settings = xmp_settings_for_recipe(develop) if develop else {}
    develop_keys = [
        key
        for key in ("ProcessVersion", "Exposure2012", "CropLeft", "CropTop", "CropRight", "CropBottom", "CropAngle", "HasCrop")
        if key in settings
    ]
    completed = subprocess.run(
        [
            str(executable), "-j", "-G1", "-XMP:Rating", "-XMP:Subject", "-XMP:HierarchicalSubject",
            *(f"-XMP-crs:{key}" for key in develop_keys),
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload = json.loads(completed.stdout)
    rating = payload[0].get("XMP-xmp:Rating") if payload else None
    if rating != expected_rating:
        raise ValueError(f"ExifTool 回读星级不一致：期望 {expected_rating}，实际 {rating}")
    record = payload[0] if payload else {}
    for key in develop_keys:
        actual = record.get(f"XMP-crs:{key}")
        expected = settings[key]
        if actual is None:
            raise ValueError(f"ExifTool 未能回读裁剪调色字段：{key}")
        try:
            matches = abs(float(actual) - float(expected)) <= 1e-5
        except (TypeError, ValueError):
            matches = str(actual).casefold() == str(expected).casefold()
        if not matches:
            raise ValueError(f"ExifTool 回读裁剪调色字段不一致：{key}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _save_manifest(path: Path, manifest: dict) -> None:
    records = manifest["records"]
    manifest["created_count"] = sum(record["status"] == "created" for record in records)
    manifest["planned_count"] = sum(
        record["status"] in {"planned", "creating", "created_pending_validation"}
        for record in records
    )
    manifest["skipped_count"] = sum(record["status"].startswith("skipped") for record in records)
    manifest["failed_count"] = sum(record["status"].startswith("failed") for record in records)
    manifest["develop_count"] = sum(bool(record.get("develop")) for record in records)
    write_json(path, manifest)


def _summary(manifest: dict, manifest_path: Path) -> dict:
    return {
        **{key: value for key, value in manifest.items() if key != "records"},
        "manifest_path": str(manifest_path),
    }


def write_results_xmp(
    results_path: Path,
    commit: bool,
    min_rating: int = 3,
    limit: int | None = None,
) -> dict:
    created_at = datetime.now(timezone.utc)
    payload = read_json(results_path)
    eligible = [
        item
        for item in payload["results"]
        if not item.get("excluded")
        and int(item["rating"]) >= min_rating
        and Path(item["path"]).suffix.lower() in PROPRIETARY_RAW_EXTENSIONS
    ]
    eligible.sort(key=lambda item: (int(item["rating"]), float(item["score"])), reverse=True)
    if limit is not None:
        eligible = eligible[:limit]

    records: list[dict] = []
    for item in eligible:
        raw_path = Path(item["path"])
        if not raw_path.is_file():
            raise FileNotFoundError(f"RAW 不存在：{raw_path}")
        xmp_path = raw_path.with_suffix(".xmp")
        records.append({
            "raw_path": str(raw_path),
            "xmp_path": str(xmp_path),
            "rating": int(item["rating"]),
            "keywords": list(item.get("keywords", [])),
            "develop": (
                dict(item["develop"])
                if isinstance(item.get("develop"), dict) and item["develop"].get("confirmed") is True
                else None
            ),
            "raw_before": quick_fingerprint(raw_path),
            "status": "skipped_existing_xmp" if xmp_path.exists() else "planned",
        })

    if commit:
        suffix = created_at.strftime("%Y%m%d-%H%M%S-%f")
        manifest_path = results_path.parent / f"xmp-commit-manifest-{suffix}.json"
    else:
        manifest_path = results_path.parent / "xmp-dry-run.json"
    manifest = {
        "schema_version": 2,
        "created_at": created_at.isoformat(),
        "source_results": str(results_path),
        "commit": commit,
        "min_rating": min_rating,
        "limit": limit,
        "records": records,
    }
    _save_manifest(manifest_path, manifest)
    if not commit:
        return _summary(manifest, manifest_path)

    # Refuse before the first sidecar is created if either verifier is unavailable.
    assert_lightroom_not_running()
    assert_exiftool_available()
    for record in records:
        if record["status"] != "planned":
            continue
        raw_path = Path(record["raw_path"])
        xmp_path = Path(record["xmp_path"])
        assert_lightroom_not_running()
        xmp_text = build_xmp(record["rating"], record["keywords"], record.get("develop"))
        validate_xmp(xmp_text)
        record["xmp_content_sha256"] = hashlib.sha256(xmp_text.encode("utf-8")).hexdigest()
        record["status"] = "creating"
        _save_manifest(manifest_path, manifest)
        created_by_us = False
        try:
            atomic_create_text(xmp_path, xmp_text)
            created_by_us = True
        except FileExistsError:
            record["status"] = "skipped_existing_xmp_race"
            _save_manifest(manifest_path, manifest)
            continue
        except Exception as exc:
            record["status"] = "failed_before_create"
            record["error"] = f"{type(exc).__name__}: {exc}"
            _save_manifest(manifest_path, manifest)
            raise

        try:
            record["xmp_created"] = quick_fingerprint(xmp_path)
            record["status"] = "created_pending_validation"
            _save_manifest(manifest_path, manifest)
            persisted = xmp_path.read_text(encoding="utf-8")
            validate_xmp(persisted)
            if persisted != xmp_text:
                raise RuntimeError(f"XMP 回读内容与写入内容不一致：{xmp_path}")
            validate_xmp_with_exiftool(xmp_path, record["rating"], record.get("develop"))
            record["raw_after"] = quick_fingerprint(raw_path)
            if record["raw_before"] != record["raw_after"]:
                raise RuntimeError(f"RAW 指纹发生变化：{raw_path}")
            record["status"] = "created"
            _save_manifest(manifest_path, manifest)
        except Exception as exc:
            # Never remove a sidecar that another process replaced after creation.
            own_file = created_by_us and xmp_path.is_file() and _file_sha256(xmp_path) == record["xmp_content_sha256"]
            if own_file:
                xmp_path.unlink()
                record["status"] = "failed_removed"
            else:
                record["status"] = "failed_preserved"
            record["error"] = f"{type(exc).__name__}: {exc}"
            _save_manifest(manifest_path, manifest)
            raise

    return _summary(manifest, manifest_path)


def rollback_manifest(manifest_path: Path) -> dict:
    assert_lightroom_not_running()
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != 2 or manifest.get("commit") is not True:
        raise ValueError("不是可回滚的 XMP 提交清单。")
    removed: list[str] = []
    skipped: list[str] = []
    for record in manifest.get("records", []):
        status = record.get("status")
        if status not in {"created", "created_pending_validation", "creating"}:
            continue
        raw_value = record.get("raw_path")
        xmp_value = record.get("xmp_path")
        expected = record.get("xmp_created")
        expected_sha256 = record.get("xmp_content_sha256")
        if not raw_value or not xmp_value:
            skipped.append(f"{xmp_value or '未知路径'}（清单缺少路径）")
            continue
        raw_path = Path(raw_value)
        xmp_path = Path(xmp_value)
        if xmp_path != raw_path.with_suffix(".xmp") or raw_path.suffix.lower() not in PROPRIETARY_RAW_EXTENSIONS:
            skipped.append(f"{xmp_path}（路径校验失败）")
            continue
        if not xmp_path.exists():
            skipped.append(str(xmp_path))
            continue
        if status == "creating":
            if not isinstance(expected_sha256, str) or _file_sha256(xmp_path) != expected_sha256:
                skipped.append(f"{xmp_path}（创建状态不完整或文件已变化）")
                continue
        elif not isinstance(expected, dict) or quick_fingerprint(xmp_path) != expected:
            skipped.append(f"{xmp_path}（提交后已被修改）")
            continue
        xmp_path.unlink()
        removed.append(str(xmp_path))
    result = {"manifest": str(manifest_path), "removed": removed, "already_missing": skipped}
    result_path = manifest_path.parent / f"xmp-rollback-result-{manifest_path.stem}.json"
    write_json(result_path, result)
    return {**result, "result_path": str(result_path)}
