from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image
from tqdm import tqdm

from .constants import (
    OLLAMA_ENDPOINT,
    PREVIEW_VERSION,
    VLM_IMAGE_VERSION,
    VLM_MODEL_ID,
    VLM_PROMPT_VERSION,
)
from .fusion import vlm_dimension_score
from .preview import load_preview, preview_cache_path
from .progress import emit_progress, phase_end, phase_start, progress_enabled
from .util import cache_key, read_json, write_json

_SCORE_FIELDS = (
    "composition",
    "light",
    "subject_layers",
    "color",
    "technical_quality",
    "edit_potential",
    "distraction",
)


def _schema(count: int) -> dict[str, Any]:
    item_properties: dict[str, Any] = {
        "id": {"type": "string"},
        **{
            name: {"type": "number", "minimum": 0, "maximum": 100}
            for name in _SCORE_FIELDS
        },
        "rank": {"type": "integer", "minimum": 1, "maximum": count},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "strengths": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "summary": {"type": "string"},
    }
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": count,
                "maxItems": count,
                "items": {
                    "type": "object",
                    "properties": item_properties,
                    "required": list(item_properties),
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


class OllamaGroupCritic:
    """Structured, anonymous multi-image critique through local Qwen3-VL."""

    def __init__(
        self,
        data_dir: Path,
        endpoint: str | None = None,
        model: str = VLM_MODEL_ID,
        max_pixels: int = 768,
    ) -> None:
        self.data_dir = data_dir
        self.endpoint = (
            endpoint or os.environ.get("PHOTO_AI_OLLAMA_ENDPOINT") or OLLAMA_ENDPOINT
        ).rstrip("/")
        self.model = model
        from .vision_provider import load_config, identity
        self.cloud_config = load_config(data_dir)
        self.cloud = self.cloud_config.get("mode") == "cloud"
        if self.cloud:
            self.model = self.cloud_config["model"]
        self.provider_identity = identity(self.cloud_config) if self.cloud else "local"
        self.max_pixels = max_pixels
        cache_root = Path(os.environ.get("PHOTO_AI_CACHE_DIR") or data_dir / "cache")
        self.cache_dir = cache_root / "ai" / VLM_PROMPT_VERSION
        self.preview_dir = cache_root / "previews"
        self.log_dir = Path(os.environ.get("PHOTO_AI_LOGS_DIR") or data_dir / "web")
        self._server_process: subprocess.Popen[bytes] | None = None

    def _request(
        self, path: str, payload: dict[str, Any] | None = None, timeout: float = 15.0
    ) -> Any:
        if self.cloud:
            from .vision_provider import chat
            if path != "/api/chat" or payload is None:
                raise ValueError("云端不支持此本地模型操作。")
            return chat(self.cloud_config, payload)
        data = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _server_ready(self) -> bool:
        try:
            result = self._request("/api/version", timeout=2.0)
            return isinstance(result, dict) and bool(result.get("version"))
        except (OSError, ValueError, urllib.error.URLError):
            return False

    @staticmethod
    def _portable_executable() -> Path | None:
        configured = os.environ.get("PHOTO_AI_OLLAMA")
        content_root_value = os.environ.get("PHOTO_AI_CONTENT_ROOT")
        if content_root_value:
            content_root = Path(content_root_value).expanduser().resolve()
            tools_root = Path(
                os.environ.get("PHOTO_AI_TOOLS_DIR") or content_root / "tools"
            ).expanduser().resolve()
            if not tools_root.is_relative_to(content_root):
                return None
            if configured:
                configured_path = Path(configured).expanduser().resolve()
                if configured_path.is_file() and configured_path.is_relative_to(
                    tools_root
                ):
                    return configured_path
            matches = sorted(tools_root.glob("ollama-*/ollama.exe"), reverse=True)
            return matches[0] if matches else None
        if configured and Path(configured).is_file():
            return Path(configured)
        tools_dir = os.environ.get("PHOTO_AI_TOOLS_DIR")
        if tools_dir:
            matches = sorted(Path(tools_dir).glob("ollama-*/ollama.exe"), reverse=True)
            if matches:
                return matches[0]
        project_root = Path(__file__).resolve().parents[2]
        matches = sorted(
            (project_root / ".runtime" / "tools").glob("ollama-*/ollama.exe"),
            reverse=True,
        )
        return matches[0] if matches else None

    def ensure_ready(self) -> None:
        if self.cloud:
            if not self.cloud_config.get("key") or not self.cloud_config.get("upload_consent"):
                raise ValueError("请先在设置中配置云端视觉模型。")
            return
        if not self._server_ready():
            executable = self._portable_executable()
            if executable is None:
                raise RuntimeError(
                    "本地 Qwen3-VL 运行器尚未安装，请在“设置 → 资源”安装模型套装。"
                )
            log_path = self.log_dir / "ollama.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(
                OLLAMA_HOST=self.endpoint.removeprefix("http://").removeprefix(
                    "https://"
                ),
                OLLAMA_NO_CLOUD="true",
            )
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            with log_path.open("a", encoding="utf-8") as log:
                self._server_process = subprocess.Popen(
                    [str(executable), "serve"],
                    cwd=Path(
                        os.environ.get("PHOTO_AI_CONTENT_ROOT") or executable.parent
                    ),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                )
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and not self._server_ready():
                time.sleep(0.25)
        if not self._server_ready():
            raise RuntimeError("本地 Qwen3-VL 服务未能启动，请查看应用运行日志。")
        try:
            tags = self._request("/api/tags", timeout=5.0)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RuntimeError(f"无法读取本地视觉模型列表：{exc}") from exc
        names = {
            str(item.get("name") or item.get("model"))
            for item in tags.get("models", [])
            if isinstance(item, dict)
        }
        if self.model not in names:
            raise RuntimeError(
                f"深度模型 {self.model} 尚未下载，请在“设置 → 资源”修复当前模型套装。"
            )

    def _cache_path(self, paths: list[Path], context: str = "group") -> Path:
        payload = {
            "model": self.model,
            "provider": self.provider_identity,
            "prompt_version": VLM_PROMPT_VERSION,
            "preview_version": PREVIEW_VERSION,
            "image_version": VLM_IMAGE_VERSION,
            "max_pixels": self.max_pixels,
            "photos": [cache_key(path) for path in paths],
        }
        if context != "group":
            payload["comparison_context"] = context
        key = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.json"

    def _image(self, path: Path) -> str:
        preview = preview_cache_path(path, self.preview_dir)
        if not preview.is_file():
            load_preview(path, self.preview_dir)
        with Image.open(preview) as source:
            image = source.convert("RGB")
            image.thumbnail(
                (self.max_pixels, self.max_pixels), Image.Resampling.LANCZOS
            )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=86, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    @staticmethod
    def _prompt(count: int, context: str = "group") -> str:
        ids = "、".join(f"IMG_{index:02d}" for index in range(1, count + 1))
        if count == 1:
            comparison = (
                "这是单张照片，请做绝对评价；名次固定为 1，不要声称它优于其他照片。"
            )
        elif context == "global":
            comparison = (
                "这些照片来自不同相似组，请使用统一的成片标准做跨组比较并给出唯一名次。"
                "各维度使用固定绝对标尺：50 表示普通可用，70 表示扎实，85 表示优秀，95 表示极少见；"
                "不要因为当前批次整体偏强或偏弱而改变标尺。"
            )
        else:
            comparison = (
                "这些照片属于同一相似序列，请认真做组内相对比较并给出唯一名次。"
            )
        return (
            "你是严谨的风光摄影选片编辑。图片顺序依次对应："
            f"{ids}。{comparison}"
            "评价构图组织、光线时机、主体与空间层次、色彩关系、技术画质、后期潜力，以及画面干扰。"
            "所有分数使用 0–100；distraction 越高表示干扰越严重。"
            "summary 只写一条可观察、可核验的中文结论；strengths/issues 各不超过三条，禁止猜测地点、器材或拍摄者意图。"
            "严格按给定 JSON Schema 返回，不要输出 Markdown 或额外解释。"
        )

    @staticmethod
    def _validate(
        payload: Any,
        count: int,
        *,
        repair_ranks: bool = False,
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("缺少 items 数组")
        items = payload["items"]
        expected_ids = {f"IMG_{index:02d}" for index in range(1, count + 1)}
        if (
            len(items) != count
            or {item.get("id") for item in items if isinstance(item, dict)}
            != expected_ids
        ):
            raise ValueError("照片 ID 缺失、重复或未知")
        cleaned: list[dict[str, Any]] = []
        for item in items:
            if set(item) != {
                "id",
                *_SCORE_FIELDS,
                "rank",
                "confidence",
                "strengths",
                "issues",
                "summary",
            }:
                raise ValueError("视觉评审字段不完整")
            record = {"id": str(item["id"]), "rank": int(item["rank"])}
            for name in _SCORE_FIELDS:
                value = float(item[name])
                if not 0.0 <= value <= 100.0:
                    raise ValueError(f"{name} 超出 0–100")
                record[name] = value
            confidence = float(item["confidence"])
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("confidence 超出 0–1")
            record["confidence"] = confidence
            for name in ("strengths", "issues"):
                values = item[name]
                if (
                    not isinstance(values, list)
                    or len(values) > 3
                    or not all(isinstance(value, str) for value in values)
                ):
                    raise ValueError(f"{name} 必须是至多三条短文本")
                record[name] = [
                    value.strip()[:120] for value in values if value.strip()
                ]
            summary = item["summary"]
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("summary 不能为空")
            record["summary"] = summary.strip()[:240]
            cleaned.append(record)
        if {item["rank"] for item in cleaned} != set(range(1, count + 1)):
            if not repair_ranks:
                raise ValueError("组内名次必须唯一且完整")
            # The model occasionally emits duplicate ranks even though every
            # visual dimension is valid.  Downstream ranking is dimension-
            # based anyway, so keep the useful critique and rebuild a stable,
            # complete rank sequence instead of aborting the whole run.
            ranked = sorted(
                cleaned,
                key=lambda item: (-vlm_dimension_score(item), item["id"]),
            )
            for rank, item in enumerate(ranked, start=1):
                item["rank"] = rank
        return sorted(cleaned, key=lambda item: item["id"])

    def critique(
        self, paths: list[Path], context: str = "group"
    ) -> list[dict[str, Any]]:
        if not 1 <= len(paths) <= 6:
            raise ValueError("一次视觉评审只接受 1–6 张照片。")
        if context not in {"group", "global"}:
            raise ValueError("视觉评审场景必须是 group 或 global。")
        cached_path = self._cache_path(paths, context)
        if cached_path.is_file():
            try:
                cached = read_json(cached_path)
                if (
                    cached.get("model") == self.model
                    and cached.get("prompt_version") == VLM_PROMPT_VERSION
                    and cached.get("preview_version") == PREVIEW_VERSION
                    and cached.get("image_version") == VLM_IMAGE_VERSION
                ):
                    return self._validate(cached.get("result"), len(paths))
            except (OSError, ValueError, TypeError):
                pass

        images = [self._image(path) for path in paths]
        schema = _schema(len(paths))
        request_payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": self._prompt(len(paths), context),
                    "images": images,
                }
            ],
            "stream": False,
            "think": False,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1400},
            "keep_alive": "2m",
        }
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                response = self._request("/api/chat", request_payload, timeout=900.0)
                content = response["message"]["content"]
                parsed = json.loads(content) if isinstance(content, str) else content
                result = self._validate(parsed, len(paths), repair_ranks=True)
                write_json(
                    cached_path,
                    {
                        "model": self.model,
                        "prompt_version": VLM_PROMPT_VERSION,
                        "preview_version": PREVIEW_VERSION,
                        "image_version": VLM_IMAGE_VERSION,
                        "result": {"items": result},
                    },
                )
                return result
            except (
                KeyError,
                TypeError,
                ValueError,
                OSError,
                urllib.error.URLError,
            ) as exc:
                last_error = exc
        raise RuntimeError(f"Qwen3-VL 连续两次未返回有效评审：{last_error}")

    def critique_groups(
        self,
        paths: list[Path],
        groups: list[list[int]],
        fast_scores: list[float] | None = None,
        context: str = "group",
    ) -> dict[int, dict[str, Any]]:
        description = "候选跨组评审" if context == "global" else "构图组内评审"
        phase = "global_vlm" if context == "global" else "local_vlm"
        expected = {index for indices in groups for index in indices}
        phase_start(phase, description, len(expected), unit="张")
        self.ensure_ready()
        output: dict[int, dict[str, Any]] = {}
        for indices in tqdm(
            groups, desc=description, unit="组", disable=progress_enabled()
        ):
            if len(indices) <= 6:
                chunks = [indices]
            else:
                ranked = sorted(
                    indices,
                    key=lambda index: (fast_scores or [0.0] * len(paths))[index],
                    reverse=True,
                )
                anchor = ranked[0]
                chunks = [ranked[:6]]
                chunks.extend(
                    [
                        [anchor, *ranked[start : start + 5]]
                        for start in range(6, len(ranked), 5)
                    ]
                )
            anchor_baseline: dict[str, Any] | None = None
            for chunk_number, chunk in enumerate(chunks):
                chunk_paths = [paths[index] for index in chunk]
                records = (
                    self.critique(chunk_paths)
                    if context == "group"
                    else self.critique(chunk_paths, context=context)
                )
                if chunk_number == 0:
                    anchor_baseline = records[0]
                elif anchor_baseline is not None:
                    current_anchor = records[0]
                    offsets = {
                        name: float(anchor_baseline[name]) - float(current_anchor[name])
                        for name in _SCORE_FIELDS
                    }
                    for record in records[1:]:
                        for name, offset in offsets.items():
                            record[name] = min(
                                100.0, max(0.0, float(record[name]) + offset)
                            )
                for local_index, record in enumerate(records):
                    global_index = chunk[local_index]
                    if global_index not in output:
                        output[global_index] = record
                emit_progress(
                    phase,
                    description,
                    len(set(output).intersection(expected)),
                    len(expected),
                    unit="张",
                )
            # Replace model-authored local ranks with a deterministic global
            # ordering derived from the scored visual dimensions.
            ranked_global = sorted(
                indices,
                key=lambda index: vlm_dimension_score(output[index]),
                reverse=True,
            )
            for rank, global_index in enumerate(ranked_global, start=1):
                output[global_index]["rank"] = rank
        if set(output) != expected:
            raise RuntimeError("深度视觉评审未覆盖全部照片。")
        phase_end(phase, description, len(expected), unit="张")
        return output

    def unload(self) -> None:
        """Ask Ollama to release Qwen from VRAM after the scoring run."""

        if self.cloud:
            return
        try:
            self._request(
                "/api/generate",
                {"model": self.model, "keep_alive": 0},
                timeout=30.0,
            )
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if self._server_process is not None:
            try:
                self._server_process.terminate()
                self._server_process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._server_process.kill()
                except OSError:
                    pass
            finally:
                self._server_process = None
