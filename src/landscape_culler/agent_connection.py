"""Same-user CLI discovery for the authenticated loopback service.

Windows credentials are protected with current-user DPAPI, not printed in argv
or exported settings. Other platforms require an owner-only descriptor.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import stat
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

CONNECTION_SCHEMA = "PHOTO_TOOLBOX_CONNECTION/1"
CLI_HEADER = "X-Photo-AI-CLI-Token"


def acquire_service_lock(path: Path):
    """Keep one live task owner per data directory, including CLI-only servers."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        stream.close()
        raise RuntimeError("此数据目录已有服务运行。请连接已有服务，勿重复启动。") from None
    return stream


def release_service_lock(stream) -> None:
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _dpapi(value: bytes, *, decrypt: bool = False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        method = crypt32.CryptUnprotectData
        method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        method = crypt32.CryptProtectData
        method.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = method(ctypes.byref(source), "Photography toolbox local CLI", None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise OSError("无法读取当前 Windows 用户的 CLI 凭据，请在相同账户重新启动工具箱。")
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        kernel32.LocalFree(output.data)


def validate_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value))
    try:
        valid_port = parsed.port is not None and 0 < parsed.port <= 65535
    except ValueError:
        valid_port = False
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or not valid_port or parsed.username or parsed.password or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment):
        raise ValueError("CLI 只连接明确的本机回环 HTTP 地址。")
    return value.rstrip("/")


def publish_connection(path: Path, *, origin: str, pid: int, instance_id: str,
                       token: str, content_root: Path, data_dir: Path) -> Path:
    origin = validate_origin(origin)
    if not token or len(token) < 16:
        raise ValueError("CLI 凭据无效。")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    protected = _dpapi(token.encode("utf-8")) if os.name == "nt" else token.encode("utf-8")
    record = {"schema": CONNECTION_SCHEMA, "origin": origin, "pid": int(pid), "instance_id": instance_id,
              "content_root": str(content_root), "data_dir": str(data_dir),
              "credential": {"kind": "windows-dpapi" if os.name == "nt" else "owner-file",
                             "value": base64.b64encode(protected).decode("ascii")}}
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("本机服务发现不接受重定向。")


def load_connection(path: Path, *, verify_health: bool = True) -> dict[str, Any]:
    path = Path(path)
    if path.stat().st_size > 32768:
        raise ValueError("CLI 连接记录过大。")
    if os.name != "nt" and (stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise PermissionError("CLI 凭据文件必须只允许当前用户访问（0600）。")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema") != CONNECTION_SCHEMA:
        raise ValueError("CLI 连接记录版本不兼容，请重新启动工具箱。")
    origin = validate_origin(record.get("origin", ""))
    if verify_health:
        # Probe without any secret first. A stale/reused port cannot receive the
        # credential unless the expected process and instance are still present.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(origin + "/health", timeout=3) as response:
                health = json.loads(response.read(4096))
        except (OSError, ValueError) as exc:
            raise ConnectionError("工具箱服务未运行或连接记录已失效。") from exc
        if (health.get("protocol") != "PHOTO_AI_SERVICE/1"
            or health.get("pid") != record.get("pid")
            or health.get("instance_id") != record.get("instance_id")):
            raise ConnectionError("服务身份与连接记录不一致，请重新启动工具箱。")
    credential = record.get("credential") or {}
    try:
        encoded = base64.b64decode(credential.get("value", ""), validate=True)
        if credential.get("kind") == "windows-dpapi" and os.name == "nt":
            token = _dpapi(encoded, decrypt=True).decode("utf-8")
        elif credential.get("kind") == "owner-file" and os.name != "nt":
            token = encoded.decode("utf-8")
        else:
            raise ValueError("此凭据属于其他系统或用户。")
        if len(token) < 16:
            raise ValueError("无效凭据。")
    except (ValueError, UnicodeError, OSError) as exc:
        raise ValueError("CLI 凭据不可用，请在当前账户重开工具箱。") from exc
    return {key: record.get(key) for key in ("origin", "pid", "instance_id", "content_root", "data_dir")} | {
        "headers": {CLI_HEADER: token}}


def remove_connection(path: Path, instance_id: str) -> None:
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        if record.get("schema") == CONNECTION_SCHEMA and record.get("instance_id") == instance_id:
            Path(path).unlink()
    except (OSError, ValueError):
        pass
