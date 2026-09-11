from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .util import read_json, write_json

CREATIVE_LUT_INDEX_SCHEMA_VERSION = 1
CREATIVE_LUT_RENDER_VERSION = "ocio-display-srgb-v1"
MAX_CUBE_FILE_BYTES = 64 * 1024 * 1024
MAX_LUT_1D_SIZE = 262_144
MAX_LUT_3D_SIZE = 128
ORDINARY_XMP_LUT_COMPATIBLE = False
ORDINARY_XMP_LUT_LIMITATION = (
    "普通 Lightroom XMP 不能表达或嵌入任意 .cube LUT；创意 LUT 只能写入渲染成片，"
    "或先转换为 Lightroom 支持的配置文件/预设后再由 Lightroom 管理。"
)

_INDEX_LOCK = threading.RLock()


class _LazyModule:
    """Keep optional rendering libraries out of the desktop service startup."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.module: Any | None = None

    def __getattr__(self, attribute: str) -> Any:
        if self.module is None:
            self.module = importlib.import_module(self.name)
        return getattr(self.module, attribute)


np = _LazyModule("numpy")
ocio = _LazyModule("PyOpenColorIO")
Image = _LazyModule("PIL.Image")


def _unidentified_image_error() -> type[Exception]:
    return importlib.import_module("PIL").UnidentifiedImageError


class CreativeLutError(RuntimeError):
    """Base error for the creative LUT subsystem."""


class InvalidCubeLutError(CreativeLutError):
    """Raised when a `.cube` resource is malformed or unsafe to process."""


class CreativeLutNotFoundError(CreativeLutError):
    """Raised when a LUT id is not present in the managed index."""


@dataclass(frozen=True, slots=True)
class CubeInspection:
    kind: str
    title: str | None
    size_1d: int | None
    size_3d: int | None
    domain_min: tuple[float, float, float]
    domain_max: tuple[float, float, float]
    data_rows: int


@dataclass(frozen=True, slots=True)
class CreativeLutDescriptor:
    lut_id: str
    lut_hash: str
    name: str
    kind: str
    resource_file: str
    size_1d: int | None
    size_3d: int | None
    file_size: int
    imported_at: str
    source_label: str | None = None
    source_license: str | None = None
    source_url: str | None = None
    xmp_compatible: bool = ORDINARY_XMP_LUT_COMPATIBLE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CreativeLutDescriptor:
        try:
            return cls(
                lut_id=str(payload["lut_id"]),
                lut_hash=str(payload["lut_hash"]),
                name=str(payload["name"]),
                kind=str(payload["kind"]),
                resource_file=str(payload["resource_file"]),
                size_1d=(
                    int(payload["size_1d"])
                    if payload.get("size_1d") is not None
                    else None
                ),
                size_3d=(
                    int(payload["size_3d"])
                    if payload.get("size_3d") is not None
                    else None
                ),
                file_size=int(payload["file_size"]),
                imported_at=str(payload["imported_at"]),
                source_label=(
                    str(payload["source_label"])
                    if payload.get("source_label") is not None
                    else None
                ),
                source_license=(
                    str(payload["source_license"])
                    if payload.get("source_license") is not None
                    else None
                ),
                source_url=(
                    str(payload["source_url"])
                    if payload.get("source_url") is not None
                    else None
                ),
                # A managed .cube is deliberately never advertised as ordinary-XMP compatible,
                # even if an old or hand-edited index claims otherwise.
                xmp_compatible=ORDINARY_XMP_LUT_COMPATIBLE,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CreativeLutError("创意 LUT 索引条目无效。") from exc


@dataclass(frozen=True, slots=True)
class LutImportFailure:
    source_path: str
    error: str


@dataclass(frozen=True, slots=True)
class LutImportReport:
    imported: tuple[CreativeLutDescriptor, ...]
    failures: tuple[LutImportFailure, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported": [item.to_dict() for item in self.imported],
            "failures": [asdict(item) for item in self.failures],
        }


@dataclass(frozen=True, slots=True)
class CreativeLutRenderResult:
    output_path: Path
    cache_path: Path
    cache_key: str
    cache_hit: bool
    lut_id: str
    lut_hash: str
    strength: float
    xmp_compatible: bool
    metadata_preserved: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "cache_path": str(self.cache_path),
            "cache_key": self.cache_key,
            "cache_hit": self.cache_hit,
            "lut_id": self.lut_id,
            "lut_hash": self.lut_hash,
            "strength": self.strength,
            "xmp_compatible": self.xmp_compatible,
            "metadata_preserved": list(self.metadata_preserved),
            "xmp_limitation": ORDINARY_XMP_LUT_LIMITATION,
        }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path, *, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(block_size):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_title(value: str, fallback: str) -> str:
    title = value.strip().strip('"').strip("'").strip()
    return title[:160] if title else fallback


def _parse_three_floats(
    values: list[str], *, directive: str
) -> tuple[float, float, float]:
    if len(values) != 3:
        raise InvalidCubeLutError(f"{directive} 必须包含三个数值。")
    try:
        parsed = tuple(float(value) for value in values)
    except ValueError as exc:
        raise InvalidCubeLutError(f"{directive} 包含无效数值。") from exc
    if not all(math.isfinite(value) for value in parsed):
        raise InvalidCubeLutError(f"{directive} 不能包含无穷大或 NaN。")
    return parsed  # type: ignore[return-value]


def inspect_cube_lut(path: Path) -> CubeInspection:
    """Inspect and validate a common Iridas/Resolve 1D or 3D `.cube` file.

    OpenColorIO remains authoritative for parsing and execution.  This bounded
    text pass rejects oversized declarations and malformed row counts before a
    third-party file reaches the native binding.
    """

    source = Path(path).resolve()
    if source.suffix.casefold() != ".cube":
        raise InvalidCubeLutError("仅支持 .cube 创意 LUT。")
    if not source.is_file():
        raise InvalidCubeLutError("LUT 文件不存在。")
    file_size = source.stat().st_size
    if file_size <= 0 or file_size > MAX_CUBE_FILE_BYTES:
        raise InvalidCubeLutError("LUT 文件为空或超过 64 MiB 安全上限。")
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise InvalidCubeLutError("LUT 不是有效的 UTF-8/ASCII 文本。") from exc
    if "\x00" in text:
        raise InvalidCubeLutError("LUT 文件包含无效二进制内容。")

    title: str | None = None
    size_1d: int | None = None
    size_3d: int | None = None
    domain_min = (0.0, 0.0, 0.0)
    domain_max = (1.0, 1.0, 1.0)
    data_rows = 0

    for line_number, original in enumerate(text.splitlines(), start=1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        directive = parts[0].upper()
        values = parts[1:]
        if directive == "TITLE":
            title = _safe_title(line[len(parts[0]) :], source.stem)
            continue
        if directive in {"LUT_1D_SIZE", "LUT_3D_SIZE"}:
            if len(values) != 1:
                raise InvalidCubeLutError(f"第 {line_number} 行的尺寸声明无效。")
            try:
                size = int(values[0])
            except ValueError as exc:
                raise InvalidCubeLutError(
                    f"第 {line_number} 行的尺寸不是整数。"
                ) from exc
            limit = MAX_LUT_1D_SIZE if directive == "LUT_1D_SIZE" else MAX_LUT_3D_SIZE
            if size < 2 or size > limit:
                raise InvalidCubeLutError(
                    f"{directive} 必须位于 2–{limit} 的安全范围。"
                )
            if directive == "LUT_1D_SIZE":
                if size_1d is not None:
                    raise InvalidCubeLutError("LUT_1D_SIZE 不能重复声明。")
                size_1d = size
            else:
                if size_3d is not None:
                    raise InvalidCubeLutError("LUT_3D_SIZE 不能重复声明。")
                size_3d = size
            continue
        if directive == "DOMAIN_MIN":
            domain_min = _parse_three_floats(values, directive=directive)
            continue
        if directive == "DOMAIN_MAX":
            domain_max = _parse_three_floats(values, directive=directive)
            continue

        try:
            row = tuple(float(value) for value in parts)
        except ValueError:
            # OpenColorIO is the final authority for uncommon format directives.
            # Unknown textual metadata is allowed here and rejected there if invalid.
            continue
        if len(row) != 3 or not all(math.isfinite(value) for value in row):
            raise InvalidCubeLutError(f"第 {line_number} 行不是三个有限数值。")
        data_rows += 1

    if size_1d is None and size_3d is None:
        raise InvalidCubeLutError("缺少 LUT_1D_SIZE 或 LUT_3D_SIZE。")
    expected_rows = (size_1d or 0) + ((size_3d or 0) ** 3)
    if data_rows != expected_rows:
        raise InvalidCubeLutError(
            f"LUT 数据行数不符：应为 {expected_rows}，实际为 {data_rows}。"
        )
    if any(low >= high for low, high in zip(domain_min, domain_max, strict=True)):
        raise InvalidCubeLutError("DOMAIN_MIN 必须逐通道小于 DOMAIN_MAX。")

    if size_1d is not None and size_3d is not None:
        kind = "1d+3d"
    elif size_3d is not None:
        kind = "3d"
    else:
        kind = "1d"

    try:
        processor = _build_cpu_processor(source, kind=kind)
        probe = np.array([[0.0, 0.5, 1.0]], dtype=np.float32)
        processor.applyRGB(probe)
        if not np.isfinite(probe).all():
            raise InvalidCubeLutError("OpenColorIO 处理结果包含无效数值。")
    except InvalidCubeLutError:
        raise
    except Exception as exc:
        raise InvalidCubeLutError(f"OpenColorIO 无法载入 LUT：{exc}") from exc

    return CubeInspection(
        kind=kind,
        title=title,
        size_1d=size_1d,
        size_3d=size_3d,
        domain_min=domain_min,
        domain_max=domain_max,
        data_rows=data_rows,
    )


def _build_cpu_processor(path: Path, *, kind: str) -> ocio.CPUProcessor:
    transform = ocio.FileTransform(src=str(path))
    if kind == "1d":
        transform.setInterpolation(ocio.INTERP_LINEAR)
    elif kind == "3d":
        transform.setInterpolation(ocio.INTERP_TETRAHEDRAL)
    else:
        transform.setInterpolation(ocio.INTERP_BEST)
    config = ocio.Config.CreateRaw()
    return config.getProcessor(transform).getDefaultCPUProcessor()


def _validate_strength(value: float) -> float:
    try:
        strength = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("LUT 强度必须是 0–200 的数值。") from exc
    if not math.isfinite(strength) or not 0.0 <= strength <= 200.0:
        raise ValueError("LUT 强度必须位于 0–200。")
    return round(strength, 4)


def _atomic_copy(source: Path, destination: Path, *, overwrite: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        if _sha256(source) == _sha256(destination):
            return
        raise FileExistsError(f"目标文件已存在：{destination}")
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        if overwrite:
            os.replace(temporary, destination)
        elif os.name == "nt":
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


class CreativeLutEngine:
    """Managed OpenColorIO `.cube` library and display-sRGB JPEG renderer.

    The input is expected to be a Lightroom-rendered, base-calibrated sRGB JPEG.
    LUT resources and previews stay under ``runtime_root``.  Ordinary Lightroom
    sidecar XMP cannot reproduce an arbitrary `.cube`, so all descriptors and
    render results expose ``xmp_compatible=False`` explicitly.
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        enforce_e_drive: bool = False,
        library_root: Path | None = None,
        cache_root: Path | None = None,
    ) -> None:
        root = Path(runtime_root).resolve()
        # ``enforce_e_drive`` remains as a source-compatible no-op for callers
        # from the development Web build.  Storage ownership is now enforced
        # by the selected ContentRoot boundary, never by a machine-specific
        # drive letter.
        del enforce_e_drive
        self.runtime_root = root
        self.library_root = (
            Path(library_root).resolve()
            if library_root is not None
            else root / "data" / "creative-luts"
        )
        self.resource_root = self.library_root / "resources"
        self.cache_root = (
            Path(cache_root).resolve()
            if cache_root is not None
            else root / "cache" / "creative-luts"
        )
        self.index_path = self.library_root / "index.json"
        self.resource_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        if not self.resource_root.resolve().is_relative_to(root):
            raise ValueError("LUT 资源目录必须位于 runtime_root 内。")
        if not self.cache_root.resolve().is_relative_to(root):
            raise ValueError("LUT 缓存目录必须位于 runtime_root 内。")

    @classmethod
    def for_project(cls, project_root: Path) -> CreativeLutEngine:
        return cls(Path(project_root).resolve() / ".runtime")

    @classmethod
    def for_content_root(cls, layout: Any) -> CreativeLutEngine:
        return cls(
            Path(layout.root),
            enforce_e_drive=False,
            library_root=Path(layout.styles) / "creative-luts",
            cache_root=Path(layout.cache) / "creative-luts",
        )

    def _empty_index(self) -> dict[str, Any]:
        return {
            "schema_version": CREATIVE_LUT_INDEX_SCHEMA_VERSION,
            "generated_at": None,
            "xmp_compatible": ORDINARY_XMP_LUT_COMPATIBLE,
            "xmp_limitation": ORDINARY_XMP_LUT_LIMITATION,
            "entries": [],
        }

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return self._empty_index()
        try:
            payload = read_json(self.index_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise CreativeLutError("创意 LUT 索引无法读取。") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise CreativeLutError("创意 LUT 索引版本无效。")
        if not isinstance(payload.get("entries"), list):
            raise CreativeLutError("创意 LUT 索引条目无效。")
        return payload

    def list_luts(self) -> list[CreativeLutDescriptor]:
        with _INDEX_LOCK:
            payload = self._read_index()
            descriptors = [
                CreativeLutDescriptor.from_dict(item)
                for item in payload["entries"]
                if isinstance(item, dict)
            ]
        return sorted(descriptors, key=lambda item: (item.name.casefold(), item.lut_id))

    def import_lut(
        self,
        source_path: Path,
        *,
        name: str | None = None,
        source_label: str | None = None,
        source_license: str | None = None,
        source_url: str | None = None,
    ) -> CreativeLutDescriptor:
        source = Path(source_path).resolve()
        inspection = inspect_cube_lut(source)
        lut_hash = _sha256(source)
        lut_id = f"cube-{lut_hash[:24]}"
        resource_file = f"resources/{lut_hash}.cube"
        destination = self.library_root / resource_file
        descriptor = CreativeLutDescriptor(
            lut_id=lut_id,
            lut_hash=lut_hash,
            name=_safe_title(name or inspection.title or source.stem, source.stem),
            kind=inspection.kind,
            resource_file=resource_file,
            size_1d=inspection.size_1d,
            size_3d=inspection.size_3d,
            file_size=source.stat().st_size,
            imported_at=_utc_now(),
            source_label=source_label,
            source_license=source_license,
            source_url=source_url,
            xmp_compatible=ORDINARY_XMP_LUT_COMPATIBLE,
        )

        with _INDEX_LOCK:
            payload = self._read_index()
            for item in payload["entries"]:
                if isinstance(item, dict) and item.get("lut_hash") == lut_hash:
                    existing = CreativeLutDescriptor.from_dict(item)
                    existing_path = self._resource_path(existing)
                    if _sha256(existing_path) != existing.lut_hash:
                        raise CreativeLutError("已索引 LUT 的资源哈希不一致。")
                    return existing
            _atomic_copy(source, destination, overwrite=False)
            # Validate the immutable managed copy, not only the external source.
            inspect_cube_lut(destination)
            payload["generated_at"] = _utc_now()
            payload["xmp_compatible"] = ORDINARY_XMP_LUT_COMPATIBLE
            payload["xmp_limitation"] = ORDINARY_XMP_LUT_LIMITATION
            payload["entries"].append(descriptor.to_dict())
            payload["entries"].sort(
                key=lambda item: (str(item.get("name", "")).casefold(), item["lut_id"])
            )
            write_json(self.index_path, payload)
        return descriptor

    def import_directory(
        self,
        source_root: Path,
        *,
        recursive: bool = True,
        source_label: str | None = None,
        source_license: str | None = None,
        source_url: str | None = None,
    ) -> LutImportReport:
        root = Path(source_root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"LUT 目录不存在：{root}")
        iterator = root.rglob("*") if recursive else root.glob("*")
        paths = sorted(
            (
                path
                for path in iterator
                if path.is_file() and path.suffix.casefold() == ".cube"
            ),
            key=lambda path: str(path).casefold(),
        )
        imported: list[CreativeLutDescriptor] = []
        failures: list[LutImportFailure] = []
        for path in paths:
            try:
                imported.append(
                    self.import_lut(
                        path,
                        source_label=source_label,
                        source_license=source_license,
                        source_url=source_url,
                    )
                )
            except (CreativeLutError, OSError) as exc:
                failures.append(LutImportFailure(str(path), str(exc)))
        return LutImportReport(tuple(imported), tuple(failures))

    def get_lut(self, lut_id: str) -> CreativeLutDescriptor:
        for descriptor in self.list_luts():
            if descriptor.lut_id == lut_id:
                return descriptor
        raise CreativeLutNotFoundError(f"找不到创意 LUT：{lut_id}")

    def delete_lut(
        self,
        lut_id: str,
        *,
        expected_hash: str,
        archive_root: Path,
    ) -> CreativeLutDescriptor:
        """Remove one exact user LUT from the active library, preserving an archive.

        The expected content hash prevents a stale browser row from removing a
        replacement.  The immutable resource is moved out of the active library
        before the index is committed and restored if that commit fails.
        """

        with _INDEX_LOCK:
            payload = self._read_index()
            matches = [
                (index, CreativeLutDescriptor.from_dict(item))
                for index, item in enumerate(payload["entries"])
                if isinstance(item, dict) and item.get("lut_id") == lut_id
            ]
            if not matches:
                raise CreativeLutNotFoundError(f"找不到创意 LUT：{lut_id}")
            index, descriptor = matches[0]
            if descriptor.lut_hash != expected_hash:
                raise CreativeLutError("LUT 已变化，请刷新风格库后重试。")
            resource = self._verified_resource(descriptor)
            archive_dir = Path(archive_root).resolve() / (
                f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            )
            archive_dir.mkdir(parents=True, exist_ok=False)
            archived_resource = archive_dir / f"{descriptor.lut_hash}.cube"
            os.replace(resource, archived_resource)
            updated = dict(payload)
            updated["entries"] = [
                item for item_index, item in enumerate(payload["entries"])
                if item_index != index
            ]
            updated["generated_at"] = _utc_now()
            try:
                write_json(
                    archive_dir / "removed.json",
                    {
                        "removed_at": _utc_now(),
                        "resource": descriptor.to_dict(),
                        "archived_file": archived_resource.name,
                    },
                )
                write_json(self.index_path, updated)
            except Exception:
                os.replace(archived_resource, resource)
                raise
            return descriptor

    def _resource_path(self, descriptor: CreativeLutDescriptor) -> Path:
        relative = Path(descriptor.resource_file)
        if relative.is_absolute() or ".." in relative.parts:
            raise CreativeLutError("LUT 资源路径越界。")
        path = (self.library_root / relative).resolve()
        if not path.is_relative_to(self.resource_root.resolve()) or not path.is_file():
            raise CreativeLutError("LUT 资源不存在或路径越界。")
        return path

    def _verified_resource(self, descriptor: CreativeLutDescriptor) -> Path:
        path = self._resource_path(descriptor)
        if _sha256(path) != descriptor.lut_hash:
            raise CreativeLutError("LUT 资源哈希不一致，已拒绝渲染。")
        return path

    def cache_key(
        self,
        input_jpeg: Path,
        *,
        lut_hash: str,
        strength: float,
        jpeg_quality: int = 90,
    ) -> str:
        source = Path(input_jpeg).resolve()
        amount = _validate_strength(strength)
        payload = {
            "input_sha256": _sha256(source),
            "lut_hash": lut_hash,
            "strength": amount,
            "jpeg_quality": int(jpeg_quality),
            "render_version": CREATIVE_LUT_RENDER_VERSION,
            "ocio_version": ocio.__version__,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def render_jpeg(
        self,
        input_jpeg: Path,
        *,
        lut_id: str,
        strength: float = 100,
        output_path: Path | None = None,
        jpeg_quality: int = 90,
        overwrite: bool = False,
    ) -> CreativeLutRenderResult:
        source = Path(input_jpeg).resolve()
        if source.suffix.casefold() not in {".jpg", ".jpeg"} or not source.is_file():
            raise ValueError("创意 LUT 输入必须是 Lightroom 导出的 sRGB JPEG。")
        if not 1 <= int(jpeg_quality) <= 100:
            raise ValueError("JPEG 质量必须位于 1–100。")
        amount = _validate_strength(strength)
        descriptor = self.get_lut(lut_id)
        lut_path = self._verified_resource(descriptor)
        key = self.cache_key(
            source,
            lut_hash=descriptor.lut_hash,
            strength=amount,
            jpeg_quality=jpeg_quality,
        )
        cache_path = self.cache_root / key[:2] / f"{key}.jpg"
        cache_hit = cache_path.is_file()
        preserved: tuple[str, ...]

        if cache_hit:
            try:
                with Image.open(cache_path) as cached:
                    cached.verify()
            except (OSError, _unidentified_image_error()):
                cache_path.unlink(missing_ok=True)
                cache_hit = False

        if not cache_hit:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(
                f".{cache_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                if amount == 0.0:
                    shutil.copy2(source, temporary)
                    with Image.open(source) as image:
                        preserved = _preserved_metadata_names(image.info)
                else:
                    preserved = self._render_to_path(
                        source,
                        lut_path=lut_path,
                        lut_kind=descriptor.kind,
                        strength=amount,
                        destination=temporary,
                        jpeg_quality=int(jpeg_quality),
                    )
                os.replace(temporary, cache_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        else:
            with Image.open(cache_path) as image:
                preserved = _preserved_metadata_names(image.info)

        destination = cache_path
        if output_path is not None:
            requested = Path(output_path).resolve()
            if requested.suffix.casefold() not in {".jpg", ".jpeg"}:
                raise ValueError("创意 LUT 输出必须使用 .jpg 或 .jpeg。")
            if requested == source:
                raise ValueError("不能覆盖基础校准输入 JPEG。")
            _atomic_copy(cache_path, requested, overwrite=overwrite)
            destination = requested

        return CreativeLutRenderResult(
            output_path=destination,
            cache_path=cache_path,
            cache_key=key,
            cache_hit=cache_hit,
            lut_id=descriptor.lut_id,
            lut_hash=descriptor.lut_hash,
            strength=amount,
            xmp_compatible=ORDINARY_XMP_LUT_COMPATIBLE,
            metadata_preserved=preserved,
        )

    def _render_to_path(
        self,
        source: Path,
        *,
        lut_path: Path,
        lut_kind: str,
        strength: float,
        destination: Path,
        jpeg_quality: int,
    ) -> tuple[str, ...]:
        try:
            with Image.open(source) as image:
                if image.format != "JPEG":
                    raise ValueError("创意 LUT 输入不是有效 JPEG。")
                image.load()
                info = dict(image.info)
                pixels = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        except _unidentified_image_error() as exc:
            raise ValueError("创意 LUT 输入不是有效 JPEG。") from exc

        processed = np.ascontiguousarray(pixels.copy(), dtype=np.float32)
        try:
            _build_cpu_processor(lut_path, kind=lut_kind).applyRGB(processed)
        except Exception as exc:
            raise CreativeLutError(f"OpenColorIO 渲染失败：{exc}") from exc
        if not np.isfinite(processed).all():
            raise CreativeLutError("OpenColorIO 渲染产生了无效像素。")

        blended = pixels + (processed - pixels) * (strength / 100.0)
        encoded = np.rint(np.clip(blended, 0.0, 1.0) * 255.0).astype(np.uint8)
        output = Image.fromarray(encoded, mode="RGB")
        save_options, preserved = _jpeg_metadata_options(info)
        save_options.update(
            {
                "format": "JPEG",
                "quality": jpeg_quality,
                "optimize": False,
            }
        )
        output.save(destination, **save_options)
        with Image.open(destination) as verification:
            verification.verify()
        return preserved


def _preserved_metadata_names(info: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for key, label in (
        ("exif", "EXIF"),
        ("icc_profile", "ICC"),
        ("xmp", "XMP"),
        ("XML:com.adobe.xmp", "XMP"),
        ("dpi", "DPI"),
        ("comment", "comment"),
    ):
        if info.get(key) is not None and label not in names:
            names.append(label)
    return tuple(names)


def _jpeg_metadata_options(
    info: dict[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    options: dict[str, Any] = {}
    if info.get("exif"):
        options["exif"] = info["exif"]
    if info.get("icc_profile"):
        options["icc_profile"] = info["icc_profile"]
    xmp = info.get("xmp") or info.get("XML:com.adobe.xmp")
    if xmp:
        options["xmp"] = xmp
    if info.get("dpi"):
        options["dpi"] = info["dpi"]
    if info.get("comment"):
        options["comment"] = info["comment"]
    if info.get("progressive") or info.get("progression"):
        options["progressive"] = True
    return options, _preserved_metadata_names(info)
