"""Owned, portable storage for mutable PhotoAI content.

This module deliberately has no development-directory or drive-letter fallback.
Callers must supply a root, set :data:`CONTENT_ROOT_ENV`, or configure the
per-user registry pointer.  That fail-closed rule prevents a disconnected data
drive from silently redirecting large caches back to the system drive.
"""

from __future__ import annotations

import copy
import ctypes
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import atomic_create_text, write_json

CONTENT_ROOT_ENV = "PHOTO_AI_CONTENT_ROOT"
REGISTRY_SUBKEY = r"Software\PhotoAI"
REGISTRY_VALUE = "ContentRoot"
MARKER_NAME = "marker.json"
MARKER_APPLICATION = "PhotoAI"
LAYOUT_VERSION = 1
SETTINGS_EXPORT_SCHEMA_VERSION = 1

_WINDOWS_ALLOWED_FILESYSTEMS = frozenset({"NTFS", "REFS"})
_WINDOWS_LOCAL_DRIVE_TYPES = frozenset({2, 3})  # removable and fixed
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class ContentRootError(RuntimeError):
    """Base error for portable content-root failures."""


class ContentRootNotConfiguredError(ContentRootError):
    """No explicit, environment, or per-user registry pointer was found."""


class ContentRootUnavailableError(ContentRootError):
    """The configured root is missing or cannot currently be accessed."""


class ContentRootValidationError(ContentRootError):
    """The proposed root violates storage ownership or safety rules."""


class ResourcePathError(ContentRootError):
    """A relative resource path attempted to leave its owned directory."""


@dataclass(frozen=True, slots=True)
class ContentRootLayout:
    """Stable top-level layout below a user-owned content root."""

    root: Path
    state: Path
    projects: Path
    models: Path
    runtimes: Path
    tools: Path
    styles: Path
    cache: Path
    downloads: Path
    temp: Path
    logs: Path
    backups: Path

    @classmethod
    def from_root(cls, root: str | os.PathLike[str]) -> ContentRootLayout:
        normalized = _absolute_path(root)
        return cls(
            root=normalized,
            state=normalized / "state",
            projects=normalized / "projects",
            models=normalized / "models",
            runtimes=normalized / "runtimes",
            tools=normalized / "tools",
            styles=normalized / "styles",
            cache=normalized / "cache",
            downloads=normalized / "downloads",
            temp=normalized / "temp",
            logs=normalized / "logs",
            backups=normalized / "backups",
        )

    @property
    def marker(self) -> Path:
        return self.root / MARKER_NAME

    @property
    def data_dir(self) -> Path:
        """Compatibility directory for the current monolithic Web service.

        New code should use the specific layout properties.  Until the Web
        state is split, its ``PHOTO_AI_DATA_DIR`` points at ``state`` rather
        than inventing another mutable top-level directory.
        """

        return self.state

    def owned_directories(self) -> tuple[Path, ...]:
        return tuple(
            getattr(self, item.name) for item in fields(self) if item.name != "root"
        )


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value).strip()
    if not raw:
        raise ContentRootValidationError("内容目录不能为空。")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ContentRootValidationError("内容目录必须使用绝对路径。")
    return path.resolve(strict=False)


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(candidate)), os.path.normcase(str(parent))]
        )
    except ValueError:
        return False
    return common == os.path.normcase(str(parent))


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise ContentRootUnavailableError(f"无法访问内容目录所在卷：{path}")
        current = parent
    return current


def _is_unc_path(path: Path) -> bool:
    raw = str(path)
    return raw.startswith(("\\\\", "//"))


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _windows_volume_root(path: Path) -> str:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume_path_name = kernel32.GetVolumePathNameW
    get_volume_path_name.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_volume_path_name.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(32768)
    if not get_volume_path_name(str(_existing_ancestor(path)), buffer, len(buffer)):
        error = ctypes.get_last_error()
        raise ContentRootUnavailableError(
            f"无法识别内容目录所在卷（Windows 错误 {error}）。"
        )
    return buffer.value


def _windows_filesystem_type(path: Path) -> str:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume_information = kernel32.GetVolumeInformationW
    get_volume_information.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_volume_information.restype = wintypes.BOOL
    fs_name = ctypes.create_unicode_buffer(64)
    if not get_volume_information(
        _windows_volume_root(path), None, 0, None, None, None, fs_name, len(fs_name)
    ):
        error = ctypes.get_last_error()
        raise ContentRootUnavailableError(
            f"无法读取内容目录文件系统（Windows 错误 {error}）。"
        )
    return fs_name.value.upper()


def _windows_drive_type(path: Path) -> int:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [wintypes.LPCWSTR]
    get_drive_type.restype = wintypes.UINT
    return int(get_drive_type(_windows_volume_root(path)))


def _probe_writable(path: Path) -> None:
    probe = path / f".photoai-write-test-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, b"PhotoAI content-root probe\n")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        probe.unlink()
    except OSError as exc:
        raise ContentRootValidationError(f"内容目录不可写：{path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if probe.exists():
            probe.unlink()


def validate_content_root(
    root: str | os.PathLike[str],
    *,
    install_dir: str | os.PathLike[str] | None = None,
    require_exists: bool = True,
    check_writable: bool = True,
    filesystem_type_getter: Callable[[Path], str] | None = None,
    drive_type_getter: Callable[[Path], int] | None = None,
    allow_managed_runtime: bool = False,
) -> Path:
    """Validate a content root without inventing or redirecting it.

    On Windows, UNC/mapped-network locations, drive roots, reparse roots, and
    non-NTFS/ReFS volumes are rejected. The program directory itself and its
    ancestors are rejected, while the default ``<install>\\data`` child is
    intentionally supported.
    """

    lexical = Path(os.fspath(root).strip()).expanduser()
    if not lexical.is_absolute():
        raise ContentRootValidationError("内容目录必须使用绝对路径。")
    if os.name == "nt" and _is_unc_path(lexical):
        raise ContentRootValidationError("内容目录不能使用 UNC 网络路径。")
    if lexical.exists() and _is_link_or_junction(lexical):
        raise ContentRootValidationError("内容目录不能是符号链接或目录联接。")

    normalized = lexical.resolve(strict=False)
    if os.name == "nt" and normalized == Path(normalized.anchor):
        raise ContentRootValidationError("不能直接使用盘符根目录，请选择其下的文件夹。")

    if install_dir is None:
        install_dir = os.environ.get("PHOTO_AI_INSTALL_DIR") or Path(
            sys.executable
        ).resolve().parent
    installed = _absolute_path(install_dir)
    if not allow_managed_runtime and (
        normalized == installed or _is_within(installed, normalized)
    ):
        raise ContentRootValidationError(
            "内容目录不能直接使用程序安装目录或它的上级目录。"
        )

    if require_exists:
        if not normalized.exists():
            raise ContentRootUnavailableError(f"内容目录当前不可用：{normalized}")
        if not normalized.is_dir():
            raise ContentRootValidationError("内容目录必须是文件夹。")

    volume_probe = normalized if normalized.exists() else _existing_ancestor(normalized)
    if os.name == "nt":
        get_drive_type = drive_type_getter or _windows_drive_type
        if get_drive_type(volume_probe) not in _WINDOWS_LOCAL_DRIVE_TYPES:
            raise ContentRootValidationError(
                "内容目录必须位于本机磁盘，不能使用映射网络盘。"
            )
        get_filesystem_type = filesystem_type_getter or _windows_filesystem_type
        filesystem_type = get_filesystem_type(volume_probe).upper()
        if filesystem_type not in _WINDOWS_ALLOWED_FILESYSTEMS:
            raise ContentRootValidationError(
                f"内容目录所在文件系统为 {filesystem_type or '未知'}；仅支持 NTFS/ReFS。"
            )

    if check_writable:
        _probe_writable(normalized if normalized.exists() else volume_probe)
    return normalized


def read_content_root_pointer() -> str | None:
    """Read the current-user registry pointer on Windows."""

    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_SUBKEY) as key:
            value, value_type = winreg.QueryValueEx(key, REGISTRY_VALUE)
    except FileNotFoundError:
        return None
    if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
        raise ContentRootValidationError("注册表中的内容目录指针类型无效。")
    value = str(value).strip()
    if value_type == winreg.REG_EXPAND_SZ:
        value = os.path.expandvars(value)
    return value or None


def write_content_root_pointer(
    root: str | os.PathLike[str],
    *,
    install_dir: str | os.PathLike[str] | None = None,
) -> None:
    """Persist only the root pointer below ``HKCU\\Software\\PhotoAI``."""

    if os.name != "nt":
        return
    import winreg

    normalized = validate_content_root(
        root,
        install_dir=install_dir,
        require_exists=True,
        check_writable=True,
    )
    _read_and_validate_marker(normalized / MARKER_NAME)
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, REGISTRY_SUBKEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, str(normalized))


def _configured_root(
    explicit: str | os.PathLike[str] | None,
    environment: Mapping[str, str] | None,
) -> str | os.PathLike[str]:
    if explicit is not None:
        return explicit
    source = os.environ if environment is None else environment
    configured = str(source.get(CONTENT_ROOT_ENV, "")).strip()
    if configured:
        return configured
    registered = read_content_root_pointer()
    if registered:
        return registered
    raise ContentRootNotConfiguredError(
        "尚未配置内容目录；请选择一个可写的本机数据文件夹。"
    )


def _marker_payload() -> dict[str, Any]:
    return {
        "application": MARKER_APPLICATION,
        "layout_version": LAYOUT_VERSION,
        "root_id": str(uuid.uuid4()),
        "created_utc": datetime.now(UTC).isoformat(),
    }


def _read_and_validate_marker(marker: Path) -> dict[str, Any]:
    if not marker.exists():
        raise ContentRootUnavailableError(f"内容目录缺少 {MARKER_NAME} 所有权标记。")
    if not marker.is_file() or _is_link_or_junction(marker):
        raise ContentRootValidationError("内容目录所有权标记无效。")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContentRootValidationError("内容目录所有权标记已损坏。") from exc
    if not isinstance(payload, dict):
        raise ContentRootValidationError("内容目录所有权标记格式无效。")
    if payload.get("application") != MARKER_APPLICATION:
        raise ContentRootValidationError("该目录不属于 PhotoAI。")
    if payload.get("layout_version") != LAYOUT_VERSION:
        raise ContentRootValidationError("内容目录版本不受当前程序支持。")
    try:
        uuid.UUID(str(payload["root_id"]))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ContentRootValidationError("内容目录标识无效。") from exc
    if not isinstance(payload.get("created_utc"), str):
        raise ContentRootValidationError("内容目录创建时间无效。")
    return payload


def _ensure_owned_directories(layout: ContentRootLayout) -> None:
    for directory in layout.owned_directories():
        if directory.exists() and (
            not directory.is_dir() or _is_link_or_junction(directory)
        ):
            raise ContentRootValidationError(f"内容目录结构无效：{directory.name}")
        directory.mkdir(parents=True, exist_ok=True)
        resolved = directory.resolve(strict=True)
        if not _is_within(resolved, layout.root):
            raise ContentRootValidationError(f"内容目录发生路径越界：{directory.name}")


def initialize_content_root(
    root: str | os.PathLike[str] | None = None,
    *,
    install_dir: str | os.PathLike[str] | None = None,
    environment: MutableMapping[str, str] | None = None,
    apply_environment: bool = True,
    persist_registry: bool = True,
) -> ContentRootLayout:
    """Create or reopen an owned content root and its stable layout.

    A pre-existing non-empty directory without a valid marker is refused.  This
    prevents uninstall/repair operations from ever treating an arbitrary photo
    directory as application-owned data.
    """

    configured = _configured_root(root, environment)
    normalized = validate_content_root(
        configured,
        install_dir=install_dir,
        require_exists=False,
        check_writable=True,
    )
    existed = normalized.exists()
    if existed and not normalized.is_dir():
        raise ContentRootValidationError("内容目录必须是文件夹。")
    marker = normalized / MARKER_NAME
    if existed and not marker.exists() and any(normalized.iterdir()):
        raise ContentRootValidationError(
            "所选文件夹已有其他内容；请选择空文件夹或已配置的 PhotoAI 目录。"
        )

    normalized.mkdir(parents=True, exist_ok=True)
    validate_content_root(
        normalized,
        install_dir=install_dir,
        require_exists=True,
        check_writable=True,
    )
    if marker.exists():
        _read_and_validate_marker(marker)
    else:
        if any(normalized.iterdir()):
            raise ContentRootValidationError(
                "初始化期间文件夹内容发生变化，已停止配置。"
            )
        payload = _marker_payload()
        try:
            atomic_create_text(
                marker, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            )
        except FileExistsError:
            pass
        _read_and_validate_marker(marker)

    layout = ContentRootLayout.from_root(normalized)
    _ensure_owned_directories(layout)
    if apply_environment:
        apply_runtime_environment(layout, environment=environment)
    if persist_registry:
        write_content_root_pointer(layout.root, install_dir=install_dir)
    return layout


def resolve_content_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    install_dir: str | os.PathLike[str] | None = None,
    environment: MutableMapping[str, str] | None = None,
    apply_environment: bool = False,
    allow_managed_runtime: bool = False,
) -> ContentRootLayout:
    """Resolve an already initialized root without any fallback."""

    configured = _configured_root(explicit, environment)
    normalized = validate_content_root(
        configured,
        install_dir=install_dir,
        require_exists=True,
        check_writable=True,
        allow_managed_runtime=allow_managed_runtime,
    )
    layout = ContentRootLayout.from_root(normalized)
    _read_and_validate_marker(layout.marker)
    for directory in layout.owned_directories():
        if not directory.is_dir() or _is_link_or_junction(directory):
            raise ContentRootUnavailableError(f"内容目录结构不完整：{directory.name}")
        if not _is_within(directory.resolve(strict=True), layout.root):
            raise ContentRootValidationError(f"内容目录发生路径越界：{directory.name}")
    if apply_environment:
        apply_runtime_environment(layout, environment=environment)
    return layout


def resolve_resource_path(
    base: str | os.PathLike[str],
    relative_path: str | os.PathLike[str],
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve an application resource below ``base`` with traversal defense."""

    root = _absolute_path(base)
    raw = os.fspath(relative_path).strip()
    if not raw or "\x00" in raw:
        raise ResourcePathError("资源路径不能为空。")
    candidate = Path(raw)
    if (
        candidate.is_absolute()
        or candidate.drive
        or raw.startswith(("\\\\", "//"))
        or _WINDOWS_DRIVE_PATH.match(raw)
    ):
        raise ResourcePathError("资源路径必须是相对路径。")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ResourcePathError("资源路径包含无效的路径段。")
    if os.name == "nt" and any(":" in part for part in candidate.parts):
        raise ResourcePathError("资源路径不能包含备用数据流。")

    try:
        resolved = (root / candidate).resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise ResourcePathError("资源不存在。") from exc
    if not _is_within(resolved, root):
        raise ResourcePathError("资源路径越过了内容目录边界。")
    if must_exist and not resolved.exists():
        raise ResourcePathError("资源不存在。")
    return resolved


def runtime_environment(layout: ContentRootLayout) -> dict[str, str]:
    """Return the complete process/cache mapping for managed dependencies."""

    canonical = ContentRootLayout.from_root(layout.root)
    if layout != canonical:
        raise ContentRootValidationError("内容目录布局不是规范布局。")
    _read_and_validate_marker(layout.marker)
    for directory in layout.owned_directories():
        if not directory.is_dir() or _is_link_or_junction(directory):
            raise ContentRootUnavailableError(f"内容目录结构不完整：{directory.name}")
        if not _is_within(directory.resolve(strict=True), layout.root):
            raise ContentRootValidationError(f"内容目录发生路径越界：{directory.name}")

    paths = {
        "UV_PYTHON_INSTALL_DIR": resolve_resource_path(layout.root, "runtimes/python"),
        "UV_CACHE_DIR": resolve_resource_path(layout.root, "cache/uv"),
        "PIP_CACHE_DIR": resolve_resource_path(layout.root, "cache/pip"),
        "XDG_CACHE_HOME": resolve_resource_path(layout.root, "cache/xdg"),
        # Model files are durable user resources, not disposable Web caches.
        # The inference loaders receive an exact pinned snapshot path below
        # this Hub cache; they never resolve a mutable repo ID or refs/main.
        "HF_HOME": resolve_resource_path(layout.root, "models/huggingface"),
        "HF_HUB_CACHE": resolve_resource_path(layout.root, "models/huggingface/hub"),
        "HF_ASSETS_CACHE": resolve_resource_path(
            layout.root, "cache/huggingface/assets"
        ),
        "HF_DATASETS_CACHE": resolve_resource_path(
            layout.root, "cache/huggingface/datasets"
        ),
        "TORCH_HOME": resolve_resource_path(layout.root, "cache/torch"),
        "OLLAMA_MODELS": resolve_resource_path(layout.root, "models/ollama"),
        "TEMP": layout.temp,
        "TMP": layout.temp,
        "TMPDIR": layout.temp,
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {
        CONTENT_ROOT_ENV: str(layout.root),
        "PHOTO_AI_DATA_DIR": str(layout.data_dir),
        "PHOTO_AI_STATE_DIR": str(layout.state),
        "PHOTO_AI_PROJECTS_DIR": str(layout.projects),
        "PHOTO_AI_MODELS_DIR": str(layout.models),
        "PHOTO_AI_RUNTIMES_DIR": str(layout.runtimes),
        "PHOTO_AI_TOOLS_DIR": str(layout.tools),
        "PHOTO_AI_STYLES_DIR": str(layout.styles),
        "PHOTO_AI_CACHE_DIR": str(layout.cache),
        "PHOTO_AI_DOWNLOADS_DIR": str(layout.downloads),
        "PHOTO_AI_TEMP_DIR": str(layout.temp),
        "PHOTO_AI_LOGS_DIR": str(layout.logs),
        "PHOTO_AI_BACKUPS_DIR": str(layout.backups),
        **{key: str(value) for key, value in paths.items()},
        "UV_PYTHON_NO_REGISTRY": "1",
        "PYTHONNOUSERSITE": "1",
        # Runtime work is deterministic and never probes the network.  The
        # explicit installer temporarily overrides these two flags while it
        # is downloading a selected profile.
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }


def apply_runtime_environment(
    layout: ContentRootLayout,
    *,
    environment: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Force mutable runtime locations below ``layout.root``."""

    target = os.environ if environment is None else environment
    assigned = runtime_environment(layout)
    target.update(assigned)
    return assigned


_SETTINGS_EXPORT_KEYS = (
    "ui",
    "ui_preferences",
    "workflow_defaults",
    "model_profile_preference",
    "style_sources",
    "style_library",
    "export_defaults",
)
_SETTINGS_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "cache_dir",
        "content_root",
        "data_root",
        "downloads_dir",
        "entries",
        "executable_path",
        "gpu_info",
        "install_dir",
        "items",
        "lightroom_path",
        "machine_id",
        "models",
        "models_dir",
        "password",
        "presets",
        "projects",
        "resources",
        "runs",
        "runtime_root",
        "secret",
        "session_token",
        "style_files",
        "tasks",
        "temp_dir",
        "token",
        "tools_dir",
        "uploaded_files",
        "user_uploads",
    }
)


def _machine_specific_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized in _SETTINGS_FORBIDDEN_KEYS or normalized.endswith(
        ("_path", "_root", "_directory", "_token")
    )


def _absolute_string(value: str) -> bool:
    stripped = value.strip()
    return bool(
        stripped.startswith(("/", "\\\\", "//"))
        or stripped.casefold().startswith("file:")
        or _WINDOWS_DRIVE_PATH.match(stripped)
    )


_DROP = object()


def _sanitize_setting_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _DROP if _absolute_string(value) else value
    if isinstance(value, list):
        cleaned = [_sanitize_setting_value(item) for item in value]
        return [item for item in cleaned if item is not _DROP]
    if isinstance(value, dict):
        cleaned_dict: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("设置键必须是字符串。")
            if _machine_specific_key(key):
                continue
            cleaned = _sanitize_setting_value(item)
            if cleaned is not _DROP:
                cleaned_dict[key] = cleaned
        return cleaned_dict
    raise TypeError(f"设置包含不可导出的值类型：{type(value).__name__}")


def sanitize_settings_export(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Build the small, machine-independent ``.photoai-settings`` payload.

    Only documented preference categories are retained.  Absolute paths,
    machine integration details, task/project state and credentials are
    removed recursively; the input mapping is never mutated.
    """

    if not isinstance(settings, Mapping):
        raise TypeError("设置导出内容必须是对象。")
    source = copy.deepcopy(dict(settings))
    exported: dict[str, Any] = {"schema_version": SETTINGS_EXPORT_SCHEMA_VERSION}
    for key in _SETTINGS_EXPORT_KEYS:
        if key not in source:
            continue
        cleaned = _sanitize_setting_value(source[key])
        if cleaned is not _DROP:
            exported[key] = cleaned
    return exported


def write_settings_export(path: Path, settings: Mapping[str, Any]) -> None:
    """Atomically write a sanitized settings-only migration file."""

    write_json(path, sanitize_settings_export(settings))
