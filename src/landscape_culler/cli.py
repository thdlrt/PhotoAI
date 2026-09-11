from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Command implementations are deliberately absent from the launcher import
# path. Frozen desktop/service processes can parse and route a job without
# importing CUDA, Transformers, OpenCV, Q-ReAlign, smart-crop, or OpenColorIO.
# These override slots remain public so focused tests and embedding hosts can
# inject a command implementation without importing its heavyweight module.
classify_directory: Callable[..., Any] | None = None
score_directory: Callable[..., Any] | None = None
write_results_xmp: Callable[..., Any] | None = None
rollback_manifest: Callable[..., Any] | None = None
execute_raw_jpeg_plan: Callable[..., Any] | None = None
rollback_raw_jpeg_manifest: Callable[..., Any] | None = None
execute_xmp_cleanup_plan: Callable[..., Any] | None = None
rollback_xmp_cleanup_manifest: Callable[..., Any] | None = None
apply_results_in_lightroom: Callable[..., Any] | None = None
execute_lightroom_export: Callable[..., Any] | None = None
run_style_worker: Callable[..., Any] | None = None
configure_model_profile: Callable[..., Any] | None = None
install_ai_profile: Callable[..., Any] | None = None
delete_ai_runtime: Callable[..., Any] | None = None
render_export_luts: Callable[..., Any] | None = None
run_develop_plan_job: Callable[..., Any] | None = None


def _command_callable(module_name: str, name: str) -> Callable[..., Any]:
    override = globals().get(name)
    if callable(override):
        return override
    module = importlib.import_module(module_name, package=__package__)
    implementation = getattr(module, name)
    if not callable(implementation):
        raise TypeError(f"命令实现不可调用：{module_name}:{name}")
    return implementation


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise argparse.ArgumentTypeError(f"路径不存在：{path}")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地风光 AI 选片工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    group = subparsers.add_parser("group", help="只生成相似照片分组，不进行评分")
    group.add_argument("--input", type=_path, required=True)
    group.add_argument("--data-dir", type=Path, required=True)
    group.add_argument("--retain-ratio", type=float, default=0.30)
    group.add_argument(
        "--mode", choices=("fast", "deep"), default="deep", help="保存后续评分方式"
    )

    score = subparsers.add_parser("score", help="评分一个待处理目录或已确认分组")
    score.add_argument("--input", type=_path, required=True)
    score.add_argument("--data-dir", type=Path, required=True)
    score.add_argument("--retain-ratio", type=float, default=0.30)
    score.add_argument(
        "--mode",
        choices=("fast", "deep"),
        default="deep",
        help="快速通用审美或深度构图评审",
    )
    score.add_argument(
        "--groups-from", type=_path, help="使用已确认的分组结果，不再自动分组"
    )
    score.add_argument("--source-run-id", help="来源分组/评分批次")

    develop_plan = subparsers.add_parser(
        "develop-plan", help="在隔离的受管 AI Worker 中生成智能构图方案"
    )
    develop_plan.add_argument("--input", type=_path, required=True)
    develop_plan.add_argument("--run-dir", type=_path, required=True)
    develop_plan.add_argument("--review-revision", type=int, required=True)

    write = subparsers.add_parser("write-xmp", help="dry-run 或提交创建 XMP")
    write.add_argument("--results", type=_path, required=True)
    mode = write.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--commit", action="store_true")
    write.add_argument("--min-rating", type=int, default=3)
    write.add_argument("--limit", type=int)

    rollback = subparsers.add_parser("rollback", help="删除某次事务中新建的 XMP")
    rollback.add_argument("--manifest", type=_path, required=True)

    raw_jpeg_execute = subparsers.add_parser(
        "raw-jpeg-execute", help="按预览计划整理 RAW/成片孤片"
    )
    raw_jpeg_execute.add_argument("--plan", type=_path, required=True)
    raw_jpeg_execute.add_argument("--transactions-dir", type=Path, required=True)

    raw_jpeg_rollback = subparsers.add_parser(
        "raw-jpeg-rollback", help="撤销一次 RAW/成片整理"
    )
    raw_jpeg_rollback.add_argument("--manifest", type=_path, required=True)

    xmp_cleanup_execute = subparsers.add_parser(
        "xmp-cleanup-execute", help="按预览计划永久删除文件夹中的 XMP"
    )
    xmp_cleanup_execute.add_argument("--plan", type=_path, required=True)
    xmp_cleanup_execute.add_argument("--transactions-dir", type=Path, required=True)

    xmp_cleanup_rollback = subparsers.add_parser(
        "xmp-cleanup-rollback", help="恢复一次文件夹 XMP 清理"
    )
    xmp_cleanup_rollback.add_argument("--manifest", type=_path, required=True)

    lightroom_apply = subparsers.add_parser(
        "lightroom-apply", help="在 Lightroom 中执行已确认的基础调整和裁切"
    )
    lightroom_apply.add_argument("--results", type=_path, required=True)
    lightroom_apply.add_argument("--data-dir", type=Path, required=True)
    lightroom_apply.add_argument("--batch-id", required=True)
    lightroom_apply.add_argument("--lightroom-exe", type=Path)
    lightroom_apply.add_argument("--startup-timeout", type=float, default=120.0)
    lightroom_apply.add_argument("--batch-timeout", type=float, default=7200.0)

    lightroom_export = subparsers.add_parser(
        "lightroom-export", help="按冻结 spec 在 Lightroom 中生成 XMP、JPEG 或两者"
    )
    lightroom_export.add_argument("--spec", type=_path, required=True)
    lightroom_export.add_argument("--data-dir", type=Path, required=True)
    lightroom_export.add_argument("--batch-id", required=True)
    lightroom_export.add_argument("--lightroom-exe", type=Path)
    lightroom_export.add_argument("--startup-timeout", type=float, default=120.0)
    lightroom_export.add_argument("--batch-timeout", type=float, default=7200.0)

    style_recommend = subparsers.add_parser(
        "style-recommend",
        help="用 Lightroom 真实预览和本地 AI 为照片组推荐或重渲风格",
    )
    style_recommend.add_argument("--run-dir", type=_path, required=True)
    style_recommend.add_argument("--data-dir", type=Path, required=True)
    style_recommend.add_argument("--batch-id", required=True)
    style_recommend.add_argument("--base-revision", type=int, required=True)
    style_recommend.add_argument("--group-id", type=int)
    style_recommend.add_argument(
        "--all-groups",
        action="store_true",
        help="在同一个任务中为每个照片组分别生成推荐",
    )
    style_recommend.add_argument("--preset-id")
    style_recommend.add_argument("--preset-hash")
    style_recommend.add_argument("--lut-id")
    style_recommend.add_argument("--lut-hash")
    style_recommend.add_argument("--amount", type=int, default=100)
    style_recommend.add_argument("--preview-only", action="store_true")
    style_recommend.add_argument("--lightroom-exe", type=Path)
    style_recommend.add_argument("--startup-timeout", type=float, default=120.0)
    style_recommend.add_argument("--batch-timeout", type=float, default=7200.0)

    lut_export = subparsers.add_parser(
        "render-export-luts",
        help="在受管 AI Worker 中为 Lightroom 基础 JPEG 渲染冻结的 .cube LUT",
    )
    lut_export.add_argument("--task", type=_path, required=True)
    lut_export.add_argument("--project-root", type=Path)

    model_resources = subparsers.add_parser(
        "model-resources-configure", help="下载并应用一个本地 AI 显存档位"
    )
    model_resources.add_argument("--profile", choices=("8gb", "16gb"), required=True)
    model_resources.add_argument("--runtime-root", type=Path, required=True)
    model_resources.add_argument("--data-dir", type=Path, required=True)

    ai_install = subparsers.add_parser(
        "ai-runtime-install", help="安装、校验并原子启用完整 AI 环境"
    )
    ai_install.add_argument("--profile", choices=("8gb", "16gb"), required=True)
    ai_install.add_argument("--content-root", type=_path, required=True)

    ai_offline_import = subparsers.add_parser(
        "ai-runtime-import", help="从本地离线包安装、校验并启用完整 AI 环境"
    )
    ai_offline_import.add_argument("--content-root", type=_path, required=True)
    ai_offline_import.add_argument("--package", type=_path, required=True)

    ai_delete = subparsers.add_parser(
        "ai-runtime-delete", help="删除受管 AI 环境但保留模型和项目"
    )
    ai_delete.add_argument("--content-root", type=_path, required=True)
    return parser


def run(argv: list[str] | None = None) -> Any:
    """Parse and execute one command, importing only its implementation."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "style-recommend":
        preset_pair = bool(args.preset_id) or bool(args.preset_hash)
        lut_pair = bool(args.lut_id) or bool(args.lut_hash)
        if args.all_groups and args.group_id is not None:
            parser.error("style-recommend --all-groups 不能同时指定 --group-id")
        if args.all_groups and (preset_pair or lut_pair or args.preview_only):
            parser.error("style-recommend --all-groups 只支持生成各组独立 AI 推荐")
        if bool(args.preset_id) != bool(args.preset_hash):
            parser.error("style-recommend 要求 --preset-id 与 --preset-hash 成对提供")
        if bool(args.lut_id) != bool(args.lut_hash):
            parser.error("style-recommend 要求 --lut-id 与 --lut-hash 成对提供")
        if preset_pair and lut_pair:
            parser.error("style-recommend 不能同时指定 Lightroom 预设和 .cube LUT")
        if args.preview_only and not (preset_pair or lut_pair):
            parser.error("style-recommend --preview-only 要求一组预设或 LUT 的精确身份")
        if lut_pair and not args.preview_only:
            parser.error(".cube LUT 当前只支持 --preview-only 精确预览")
    if args.command == "group":
        result = _command_callable(".scoring", "classify_directory")(
            args.input, args.data_dir, args.retain_ratio, mode=args.mode
        )
    elif args.command == "score":
        result = _command_callable(".scoring", "score_directory")(
            args.input,
            args.data_dir,
            args.retain_ratio,
            mode=args.mode,
            grouping_path=args.groups_from,
            source_run_id=args.source_run_id,
        )
    elif args.command == "develop-plan":
        result = _command_callable(".develop_worker", "run_develop_plan_job")(
            args.input,
            args.run_dir,
            args.review_revision,
        )
    elif args.command == "write-xmp":
        result = _command_callable(".xmp", "write_results_xmp")(
            args.results,
            commit=args.commit,
            min_rating=args.min_rating,
            limit=args.limit,
        )
    elif args.command == "rollback":
        result = _command_callable(".xmp", "rollback_manifest")(args.manifest)
    elif args.command == "raw-jpeg-execute":
        result = _command_callable(".toolbox", "execute_raw_jpeg_plan")(
            args.plan, args.transactions_dir
        )
    elif args.command == "raw-jpeg-rollback":
        result = _command_callable(".toolbox", "rollback_raw_jpeg_manifest")(
            args.manifest
        )
    elif args.command == "xmp-cleanup-execute":
        result = _command_callable(".xmp_cleanup", "execute_xmp_cleanup_plan")(
            args.plan, args.transactions_dir
        )
    elif args.command == "xmp-cleanup-rollback":
        result = _command_callable(".xmp_cleanup", "rollback_xmp_cleanup_manifest")(
            args.manifest
        )
    elif args.command == "lightroom-apply":
        try:
            result = _command_callable(
                ".lightroom_apply", "apply_results_in_lightroom"
            )(
                args.results,
                args.data_dir,
                args.batch_id,
                lightroom_exe=args.lightroom_exe,
                startup_timeout=args.startup_timeout,
                batch_timeout=args.batch_timeout,
            )
        except (FileExistsError, RuntimeError, ValueError) as exc:
            print(f"Lightroom 处理失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    elif args.command == "lightroom-export":
        try:
            result = _command_callable(".lightroom_export", "execute_lightroom_export")(
                args.spec,
                args.data_dir,
                args.batch_id,
                lightroom_exe=args.lightroom_exe,
                startup_timeout=args.startup_timeout,
                batch_timeout=args.batch_timeout,
            )
        except (FileExistsError, RuntimeError, ValueError) as exc:
            print(f"Lightroom 导出失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    elif args.command == "style-recommend":
        try:
            result = _command_callable(".style_worker", "run_style_worker")(
                args.run_dir,
                args.data_dir,
                args.batch_id,
                args.base_revision,
                scope="group" if args.all_groups else None,
                group_id=args.group_id,
                preset_id=args.preset_id,
                preset_hash=args.preset_hash,
                lut_id=args.lut_id,
                lut_hash=args.lut_hash,
                amount=args.amount,
                preview_only=args.preview_only,
                preserve_confirmed=bool(args.all_groups),
                lightroom_exe=args.lightroom_exe,
                startup_timeout=args.startup_timeout,
                batch_timeout=args.batch_timeout,
            )
        except (FileExistsError, RuntimeError, ValueError) as exc:
            print(f"AI 风格推荐失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    elif args.command == "render-export-luts":
        try:
            result = _command_callable(".lut_export_worker", "render_export_luts")(
                args.task,
                project_root=args.project_root,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"创意 LUT 导出失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    elif args.command == "model-resources-configure":
        try:
            result = _command_callable(".model_resources", "configure_model_profile")(
                args.profile, args.runtime_root, args.data_dir
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"模型资源配置失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    elif args.command == "ai-runtime-install":
        from .content_root import resolve_content_root

        layout = resolve_content_root(args.content_root, apply_environment=True)
        try:
            result = _command_callable(".ai_runtime", "install_ai_profile")(
                layout, args.profile
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"AI 环境配置失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    elif args.command == "ai-runtime-import":
        from .content_root import resolve_content_root

        layout = resolve_content_root(args.content_root, apply_environment=True)
        try:
            result = _command_callable(".offline_bundle", "install_offline_bundle")(
                layout, args.package
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"AI 离线包导入失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    elif args.command == "ai-runtime-delete":
        from .content_root import resolve_content_root

        layout = resolve_content_root(args.content_root, apply_environment=True)
        try:
            result = _command_callable(".ai_runtime", "delete_ai_runtime")(layout)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"AI 环境删除失败：{exc}", file=sys.stderr, flush=True)
            raise SystemExit(1) from exc
    else:
        raise AssertionError(args.command)
    return result


def main(argv: list[str] | None = None) -> None:
    result = run(argv)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
