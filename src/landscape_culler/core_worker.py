from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import re
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROTOCOL = "PHOTO_AI_WORKER/1"
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_MAX_SPEC_BYTES = 1024 * 1024

# These are the CLI implementations shipped in CoreWorker, not in the AI
# engine. The release contract checks this list against both CLI and .spec.
CORE_COMMAND_MODULES = (
    "landscape_culler.xmp_cleanup",
    "landscape_culler.toolbox",
    "landscape_culler.xmp",
    "landscape_culler.lightroom_apply",
    "landscape_culler.lightroom_export",
    "landscape_culler.ai_runtime",
    "landscape_culler.model_resources",
    "landscape_culler.offline_bundle",
)


class WorkerProtocolError(ValueError):
    """A malformed or unsupported local CoreWorker job specification."""


def _validate_managed_job_path(path: Path, label: str) -> Path:
    """Bind installed worker I/O to CONTENT_ROOT/state/web/jobs."""

    content_root = os.environ.get("PHOTO_AI_CONTENT_ROOT")
    if not content_root:
        return path
    jobs_root = (Path(content_root).expanduser().resolve() / "state" / "web" / "jobs")
    try:
        path.relative_to(jobs_root)
    except ValueError as exc:
        raise WorkerProtocolError(
            f"{label} 必须位于受管数据目录的 state/web/jobs 中。"
        ) from exc
    return path


def _emit(event: str, job_id: str | None, **payload: Any) -> None:
    envelope = {
        "protocol": PROTOCOL,
        "event": event,
        "job_id": job_id,
        **payload,
    }
    print(
        json.dumps(envelope, ensure_ascii=True, separators=(",", ":")),
        flush=True,
    )


def _read_spec(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkerProtocolError(f"无法读取 CoreWorker job spec：{exc}") from exc
    if size <= 0 or size > _MAX_SPEC_BYTES:
        raise WorkerProtocolError("CoreWorker job spec 大小无效。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError(f"CoreWorker job spec 不是有效 JSON：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise WorkerProtocolError(f"CoreWorker job spec 必须声明 {PROTOCOL}。")
    job_id = str(payload.get("job_id") or "")
    if not _JOB_ID.fullmatch(job_id):
        raise WorkerProtocolError("CoreWorker job_id 无效。")
    return payload


def _result_path(spec: Mapping[str, Any], explicit: Path | None) -> Path:
    value = explicit if explicit is not None else spec.get("result_path")
    if value is None or not str(value).strip():
        raise WorkerProtocolError("CoreWorker 缺少原子结果路径。")
    path = _validate_managed_job_path(
        Path(str(value)).expanduser().resolve(), "CoreWorker 结果文件"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _execute(spec: Mapping[str, Any]) -> Any:
    command = str(spec.get("command") or "")
    if command == "ping":
        return {"pong": True, "payload": spec.get("payload")}
    if command == "ai-runtime-self-test":
        # PyInstaller cannot discover the command-level lazy imports used by
        # the runtime installer. Exercise the exact modules in the packaged
        # CoreWorker so a missing installer can never pass the release gate.
        from .ai_runtime import install_ai_profile
        from .model_resources import configure_model_profile

        return {
            "ai_runtime": callable(install_ai_profile),
            "model_resources": callable(configure_model_profile),
        }
    if command != "cli":
        raise WorkerProtocolError(f"CoreWorker 不支持命令：{command or 'missing'}")
    argv = spec.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(value, str) or "\x00" in value for value in argv)
    ):
        raise WorkerProtocolError("CoreWorker cli 命令必须提供非空字符串 argv。")
    # Import only after the job has been accepted. The CLI itself performs a
    # second command-level lazy import, so an idle worker stays lightweight.
    from .cli import run

    # Reserve stdout for protocol NDJSON. Existing CLI progress remains
    # inspectable on stderr until a future protocol revision envelopes it.
    with contextlib.redirect_stdout(sys.stderr):
        return run(list(argv))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PhotoAI isolated CoreWorker")
    parser.add_argument("--job-spec", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--remove-owned-lightroom-plugin", action="store_true")
    parser.add_argument("--validate-owned-content-root", type=Path)
    return parser


def _self_test() -> int:
    from .cli import build_parser as build_cli_parser
    from .version import PRODUCT_VERSION

    build_cli_parser()
    import_errors = {}
    for module_name in CORE_COMMAND_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # report missing packaged command dependencies
            import_errors[module_name] = f"{type(exc).__name__}: {exc}"
    forbidden = sorted(
        name
        for name in (
            "torch",
            "transformers",
            "cv2",
            "pyiqa",
            "PyOpenColorIO",
            "pyvips",
            "rawpy",
        )
        if name in sys.modules
    )
    status = "passed" if not forbidden and not import_errors else "failed"
    _emit(
        "self-test",
        None,
        status=status,
        version=PRODUCT_VERSION,
        unexpected_imports=forbidden,
        command_import_errors=import_errors,
    )
    return 0 if status == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.remove_owned_lightroom_plugin:
        from .lightroom_bridge import remove_owned_lightroom_plugin

        _emit("lightroom-plugin-uninstall", None, **remove_owned_lightroom_plugin())
        return 0
    if args.validate_owned_content_root is not None:
        from .content_root import resolve_content_root

        try:
            layout = resolve_content_root(
                args.validate_owned_content_root,
                install_dir=Path(sys.executable).resolve().parent,
            )
        except Exception as exc:  # noqa: BLE001 - installer validation boundary
            _emit("content-root-validation", None, status="failed", error=str(exc))
            return 1
        _emit(
            "content-root-validation",
            None,
            status="passed",
            root=str(layout.root),
        )
        return 0
    if args.job_spec is None:
        raise SystemExit("--job-spec 是执行 CoreWorker 任务的必需参数。")
    job_id: str | None = None
    result_path: Path | None = None
    try:
        spec_path = _validate_managed_job_path(
            args.job_spec.expanduser().resolve(), "CoreWorker job spec"
        )
        spec = _read_spec(spec_path)
        job_id = str(spec["job_id"])
        result_path = _result_path(spec, args.result)
        _emit("started", job_id, pid=os.getpid())
        result = _execute(spec)
        result_record = {
            "protocol": PROTOCOL,
            "job_id": job_id,
            "status": "completed",
            "result": result,
        }
        _write_atomic(result_path, result_record)
        _emit("completed", job_id, result_path=str(result_path))
        return 0
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - protocol failure boundary
        # CLI handlers raise SystemExit(1) *from* the useful installation
        # exception. Preserve that cause in the NDJSON result, not just "1".
        cause = exc
        while isinstance(cause, SystemExit) and cause.__cause__ is not None:
            cause = cause.__cause__
        message = str(cause).strip() or type(cause).__name__
        if isinstance(cause, SystemExit) and isinstance(cause.code, int):
            message = f"任务命令无法执行（退出码 {cause.code}），请查看任务日志中的具体错误。"
        message = message[-2000:]
        failed_record = {
            "protocol": PROTOCOL,
            "job_id": job_id,
            "status": "failed",
            "error": message,
        }
        if result_path is not None:
            try:
                _write_atomic(result_path, failed_record)
            except OSError:
                pass
        _emit("failed", job_id, error=message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
