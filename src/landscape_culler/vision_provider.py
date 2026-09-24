"""Private vision-provider settings and OpenAI-compatible transport (no AI imports)."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import read_json, write_json

DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
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


def public_config(data_dir: Path) -> dict:
    config = load_config(data_dir)
    return {key: config.get(key, default) for key, default in {
        "mode": "local", "base_url": DEFAULT_URL, "model": "qwen3-vl-plus",
        "timeout": 120, "upload_consent": False,
    }.items()} | {"has_key": bool(config.get("key"))}


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
    if config["mode"] not in {"local", "cloud"} or not 10 <= config["timeout"] <= 600:
        raise ValueError("模型模式或超时时间无效。")
    if config["mode"] == "cloud" and not (encrypted and config["model"] and config["upload_consent"]):
        raise ValueError("请填写模型和 API Key，并同意发送照片预览图后启用云端。")
    write_json(settings_path(data_dir), config)
    return public_config(data_dir)


def identity(config: dict) -> str:
    return hashlib.sha256(json.dumps([config.get("base_url"), config.get("model")]).encode()).hexdigest()[:20]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def chat(config: dict, payload: dict) -> dict:
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
            "temperature": 0, "max_tokens": payload.get("options", {}).get("num_predict", 1400)}
    request = urllib.request.Request(config["base_url"] + "/chat/completions",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
        "Authorization": "Bearer " + _protect(config["key"], decrypt=True)}, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=config.get("timeout", 120)) as response:
            result = json.loads(response.read(8 * 1024 * 1024).decode())
    except urllib.error.HTTPError as exc:
        hints = {401: "密钥无效或地域不匹配", 403: "没有模型调用权限", 429: "请求限流或额度不足"}
        raise ValueError(f"云端模型 HTTP {exc.code}：{hints.get(exc.code, '请检查地址、模型名及服务状态')}") from None
    except (OSError, ValueError) as exc:
        raise ValueError("云端连接失败或响应无法解析，请检查网络和超时设置。") from None
    try:
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError()
        # Some compatible services wrap JSON in a code fence despite instructions.
        if content.strip().startswith("```"):
            content = content.strip().split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return {"message": {"content": content}}
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError("云端模型未返回有效文本，请选择支持图片输入的视觉模型。") from None
