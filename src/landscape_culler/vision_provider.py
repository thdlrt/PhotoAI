"""Private vision-provider settings and OpenAI-compatible transport (no AI imports)."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import read_json, write_json

DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
THINKING_LABELS = {"off": "关闭", "low": "低", "medium": "中", "high": "高", "max": "最高", "auto": "服务默认"}
THINKING_BUDGETS = {"low": 2048, "medium": 4096, "high": 8192, "max": 16384, "auto": 8192}
TEST_IMAGE = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAgACADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3EKKKKACiiigAooooA//Z"


def settings_path(data_dir: Path) -> Path:
    root = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    return (Path(root) / "state" if root else data_dir) / "vision-provider.json"


def _protect(value: str, decrypt: bool = False) -> str:
    # Windows DPAPI is tied to the current account; keys never enter exports/jobs.
    if os.name != "nt":
        raise ValueError("云端密钥存储目前仅支持 Windows。")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(value) if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("无法读取或保存云端密钥，请重新填写。")
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode("utf-8") if decrypt else base64.b64encode(result).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def load_config(data_dir: Path) -> dict:
    path = settings_path(data_dir)
    return read_json(path) if path.is_file() else {"mode": "local"}


def cloud_enabled(data_dir: Path) -> bool:
    return load_config(data_dir).get("mode") == "cloud"


def model_label(data_dir: Path) -> str:
    config = load_config(data_dir)
    return str(config.get("model") or "云端视觉模型") if config.get("mode") == "cloud" else "Qwen3-VL"


class CloudResponseError(ValueError):
    def __init__(self, message: str, diagnostics: dict, response_text: str = ""):
        super().__init__(message)
        self.finish_reason = diagnostics.get("finish_reason")
        self.diagnostics = diagnostics
        self.response_text = response_text[:16384]


def thinking_capabilities(config: dict) -> dict:
    hostname = (urllib.parse.urlsplit(str(config.get("base_url") or DEFAULT_URL)).hostname or "").lower()
    model = str(config.get("model") or "").lower().split("/")[-1]
    is_bailian = (hostname.endswith("aliyuncs.com") and ("dashscope" in hostname or ".maas." in hostname)
                  or hostname.endswith("qwencloudapi.com"))
    if (hostname == "api.deepseek.com" or model.startswith("deepseek")) and not is_bailian:
        family, efforts, default = "deepseek", ["off", "low", "high", "max", "auto"], "off"
        hint = "开启思考会增加耗时；程序会增加输出额度，为评分正文留出空间。"
    elif model.startswith(("qwen3", "kimi-k2.5", "kimi-k2.6")):
        family, efforts, default = "bailian", ["off", "low", "medium", "high", "max", "auto"], "off"
        if "thinking" in model:
            efforts, default = efforts[1:], "auto"
        elif "instruct" in model:
            family, efforts, default = "plain", ["auto"], "auto"
        hint = "按思考额度适配强度；开启后会增加耗时。模型不支持的开关不会发送。"
    elif model.startswith(("gpt-4", "qwen2", "qwen-vl")) or (is_bailian and model.startswith("deepseek")):
        family, efforts, default = "plain", ["auto"], "auto"
        hint = "此模型或接口暂未适配思考强度，使用服务默认设置。"
    else:
        family, efforts, default = "compatible", ["low", "medium", "high", "auto"], "auto"
        if model.startswith("gpt-5-pro"):
            efforts = ["high", "auto"]
        elif model.startswith("gpt-6"):
            efforts = ["off", "low", "medium", "high", "max", "auto"]
        elif model.startswith("gpt-5"):
            if not model.startswith(("gpt-5-mini", "gpt-5-nano")) and model not in {"gpt-5", "gpt-5-chat-latest"}:
                efforts = ["off", *efforts]
        hint = "兼容接口需支持思考强度；请保存后测试连接。服务默认不会发送额外思考参数。"
    return {"family": family, "options": [{"value": item, "label": THINKING_LABELS[item]} for item in efforts],
            "default": default, "hint": hint}


def thinking_effort(config: dict) -> str:
    capabilities = thinking_capabilities(config)
    effort = config.get("thinking_effort", capabilities["default"])
    if not isinstance(effort, str) or effort not in {option["value"] for option in capabilities["options"]}:
        raise ValueError("当前模型不支持所选思考强度，请重新选择。")
    return effort


def thinking_request(config: dict, answer_tokens: int) -> dict:
    """Translate saved UI intent, independent of local workers' think=False."""
    family, effort = thinking_capabilities(config)["family"], thinking_effort(config)
    body = {}
    if family == "deepseek":
        if effort != "auto":
            body["thinking"] = {"type": "disabled" if effort == "off" else "enabled"}
            if effort != "off":
                body["reasoning_effort"] = effort
    elif family == "bailian":
        if effort != "auto":
            body["enable_thinking"] = effort != "off"
            if effort != "off":
                body["thinking_budget"] = THINKING_BUDGETS[effort]
    elif family == "compatible" and effort != "auto":
        body["reasoning_effort"] = "none" if effort == "off" else effort
    reserve = THINKING_BUDGETS.get(effort, 0) if family != "plain" else 0
    model = str(config.get("model") or "").lower()
    if family == "compatible" and effort == "auto" and not model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        reserve = 0  # Unknown non-thinking models retain their original limits.
    body["max_tokens"] = min(65536, int(answer_tokens) + reserve)
    if family == "compatible" and model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        body["max_completion_tokens"] = body.pop("max_tokens")
    return body


def public_config(data_dir: Path) -> dict:
    config = load_config(data_dir)
    public = {key: config.get(key, default) for key, default in {
        "mode": "local", "base_url": DEFAULT_URL, "model": "qwen3-vl-plus",
        "timeout": 120, "upload_consent": False,
    }.items()}
    effective = {**config, **public}
    return public | {"has_key": bool(config.get("key")), "thinking_effort": thinking_effort(effective),
                     "thinking_capabilities": thinking_capabilities(effective)}


def save_config(data_dir: Path, values: dict) -> dict:
    old = load_config(data_dir)
    url = str(values.get("base_url") or DEFAULT_URL).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("API 地址必须为 HTTPS；仅本机服务允许 HTTP。请填写 Base URL，不含密钥或查询参数。")
    if url.endswith("/chat/completions"):
        url = url[:-len("/chat/completions")]
    key = str(values.get("api_key") or "").strip()
    encrypted = _protect(key) if key else old.get("key", "")
    if values.get("clear_key"):
        encrypted = ""
    config = {"mode": values.get("mode", "local"), "base_url": url,
              "model": str(values.get("model") or "").strip(),
              "timeout": int(values.get("timeout", 120)), "key": encrypted,
              "upload_consent": bool(values.get("upload_consent"))}
    same_model = old.get("base_url") == url and old.get("model") == config["model"]
    config["thinking_effort"] = values.get("thinking_effort", old.get("thinking_effort") if same_model else None)
    if config["thinking_effort"] is None and "thinking_effort" not in values:
        config["thinking_effort"] = thinking_capabilities(config)["default"]
    thinking_effort(config)
    if config["mode"] not in {"local", "cloud"} or not 10 <= config["timeout"] <= 600:
        raise ValueError("模型模式或超时时间无效。")
    if config["mode"] == "cloud" and not (encrypted and config["model"] and config["upload_consent"]):
        raise ValueError("请填写模型和 API Key，并同意发送照片预览图后启用云端。")
    write_json(settings_path(data_dir), config)
    return public_config(data_dir)


def identity(config: dict) -> str:
    parts = [config.get("base_url"), config.get("model")]
    effort = thinking_effort(config)
    # Preserve existing default caches; changing quality invalidates only AI
    # results produced for that provider, without rewriting old projects.
    if effort != thinking_capabilities(config)["default"]:
        parts.extend(["thinking", effort])
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:20]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _log_response(diagnostics: dict, data_dir: Path | None) -> None:
    """Desktop stdout may be closed; diagnostics must never fail inference."""
    try:
        log_root = os.environ.get("PHOTO_AI_LOGS_DIR")
        if log_root or data_dir is not None:
            root = Path(log_root) if log_root else data_dir / "web" / "logs"
            write_json(root / "vision-responses" / f"{time.time_ns()}.json", diagnostics)
    except Exception:  # noqa: BLE001 - even an unwritable log disk is non-fatal
        pass
    try:
        # A desktop pipe can look open while its native handle is invalid.
        # Only echo to an interactive terminal; desktop diagnostics stay on disk.
        if sys.stdout is not None and sys.stdout.isatty():
            print("云端视觉响应：" + json.dumps(diagnostics, ensure_ascii=False), flush=True)
    except Exception:  # noqa: BLE001 - isolate optional diagnostics completely
        pass


def chat(config: dict, payload: dict, *, data_dir: Path | None = None) -> dict:
    if not config.get("upload_consent"):
        raise ValueError("尚未同意发送照片预览图。")
    messages = []
    for message in payload.get("messages", []):
        content = [{"type": "text", "text": str(message.get("content", ""))}]
        content.extend({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}
                       for image in message.get("images", []))
        messages.append({"role": message.get("role", "user"), "content": content})
    if payload.get("format"):
        messages.insert(0, {"role": "system", "content": "Return only valid JSON matching this schema: " + json.dumps(payload["format"])})
    body = {"model": config["model"], "messages": messages, "stream": False,
            **thinking_request(config, payload.get("options", {}).get("num_predict", 1400))}
    if thinking_effort(config) == "off" or thinking_capabilities(config)["family"] == "plain":
        body["temperature"] = 0
    request = urllib.request.Request(config["base_url"] + "/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
        "Authorization": "Bearer " + _protect(config["key"], decrypt=True)}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=config.get("timeout", 120)) as response:
            result = json.loads(response.read(8 * 1024 * 1024).decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            try:
                detail = json.loads(exc.read(4096)).get("error", {}).get("message", "").lower()
            except (OSError, ValueError, AttributeError):
                detail = ""
            if any(value in detail for value in ("not support image", "does not support image", "不支持图片", "vision is not supported")):
                raise ValueError("云端模型 HTTP 400：服务明确拒绝图片输入，请选择该服务支持的视觉模型。") from None
        hints = {400: "请检查模型是否支持所选思考强度和图片输入，或选择“服务默认”后测试",
                 401: "密钥无效或地域不匹配", 403: "没有模型调用权限", 429: "请求限流或额度不足"}
        raise ValueError(f"云端模型 HTTP {exc.code}：{hints.get(exc.code, '请检查地址、模型名及服务状态')}") from None
    except (OSError, ValueError) as exc:
        raise ValueError("云端连接失败或响应无法解析，请检查网络和超时设置。") from None
    try:
        choice = result["choices"][0]
        message = choice["message"]
        content = message.get("content")
        finish_reason = choice.get("finish_reason")
        usage = result.get("usage") or {}
        diagnostics = {"model": config["model"], "finish_reason": finish_reason,
                       "max_tokens": body.get("max_tokens", body.get("max_completion_tokens")),
                       "thinking_effort": thinking_effort(config), "completion_tokens": usage.get("completion_tokens"),
                       "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                       "content_chars": len(content) if isinstance(content, str) else 0}
        # JobManager captures stdout; keep only response metadata, never API
        # keys, image data, or hidden reasoning text in the task log.
        _log_response(diagnostics, data_dir)
        if finish_reason == "length":
            raise CloudResponseError("云端回复被输出额度截断（finish_reason=length），需要增加输出 token 额度或降低思考强度；调整超时不能解决。",
                                     diagnostics, content if isinstance(content, str) else "")
        if finish_reason == "content_filter":
            raise CloudResponseError("云端服务过滤了回复（finish_reason=content_filter）。", diagnostics)
        if not isinstance(content, str) or not content.strip():
            raise CloudResponseError(f"云端模型未返回最终正文（finish_reason={finish_reason or 'unknown'}），请检查模型的回复与思考设置。",
                                     diagnostics)
        # Some compatible services wrap JSON in a code fence despite instructions.
        if content.strip().startswith("```"):
            content = content.strip().split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return {"message": {"content": content}, "finish_reason": finish_reason, "usage": usage}
    except CloudResponseError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ValueError("云端响应格式无效：缺少 choices/message 正文结构。") from None
