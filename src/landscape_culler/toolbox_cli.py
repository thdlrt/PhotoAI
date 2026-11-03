"""Machine-readable photography toolbox client. Never starts a second manager."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL = "photography-toolbox/1"
ACTIVE = {"queued", "running", "cancelling"}
TERMINAL_ERRORS = {"failed", "cancelled", "interrupted"}


class CliError(Exception):
    def __init__(self, code: str, message: str, *, exit_code: int = 1, details: Any = None):
        super().__init__(message)
        self.code, self.exit_code, self.details = code, exit_code, details


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("usage_error", message, exit_code=2, details={"usage": self.format_usage().strip()})


def _positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return value


def _revision(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("revision must be non-negative")
    return value


def _port(value):
    value = int(value)
    if not 0 <= value <= 65535: raise argparse.ArgumentTypeError("port must be 0..65535")
    return value


def build_parser() -> Parser:
    parser = Parser(prog="photography-toolbox", description="摄影后期工具箱 CLI：JSON API、交互审片、延时摄影与选片交接。")
    parser.add_argument("--url", help="已运行服务的回环地址；开发服务可直接连接")
    parser.add_argument("--connection-file", type=Path, help="桌面服务生成的 cli-session.json；密钥不放命令行")
    parser.add_argument("--data-dir", type=Path, help="state 数据目录或内容根目录；自动发现已运行服务")
    parser.add_argument("--offline", action="store_true", help="仅只读本地目录，不连接服务，不创建管理器")
    parser.add_argument("--request-timeout", type=_positive, default=30, help="单次 HTTP 请求超时秒数")
    parser.add_argument("--pretty", action="store_true", help="缩进 JSON 输出")
    groups = parser.add_subparsers(dest="group", required=True, parser_class=Parser)

    def command(group, name, operation, help):
        sub = group.add_parser(name, help=help, description=help)
        sub.set_defaults(operation=operation)
        return sub

    def revision(sub, name="--revision"):
        sub.add_argument(name, type=_revision, required=True)

    def input_json(sub, required=False):
        sub.add_argument("--input-json", type=Path, required=required, help="UTF-8 JSON 文件；- 表示标准输入")

    def wait_options(sub):
        sub.add_argument("--wait", action="store_true", help="等待本次任务完成")
        sub.add_argument("--wait-timeout", type=_positive, default=120)
        sub.add_argument("--interval", type=_positive, default=1)
        sub.add_argument("--cancel-on-timeout", action="store_true", help="等待超时后显式取消任务")

    command(groups, "capabilities", "capabilities", "查看工具和当前连接能力")
    schema = command(groups, "schema", "schema", "输出命令、参数、请求 JSON Schema 与退出码")
    schema.add_argument("--command", help="例如 review.decide 或 timelapse.process")
    serve = command(groups, "serve", "serve", "前台启动独立服务；保持此进程运行")
    serve.add_argument("--content-root", type=Path, required=True)
    serve.add_argument("--port", type=_port, default=0)

    review = groups.add_parser("review", help="照片和视频的协作审片").add_subparsers(dest="action", required=True, parser_class=Parser)
    command(review, "list", "review.list", "列出审片工程")
    create = command(review, "create", "review.create", "扫描目录并建立审片工程")
    create.add_argument("--input-root", required=True)
    create.add_argument("--name")
    get = command(review, "get", "review.get", "读取审片工程和用户选择")
    get.add_argument("workspace_id")
    get.add_argument("--since-revision", type=_revision)
    decide = command(review, "decide", "review.decide", "保存单项用户决定；必须使用该素材的 user.revision")
    decide.add_argument("workspace_id"); decide.add_argument("asset_id"); revision(decide); input_json(decide)
    decide.add_argument("--status", choices=["keep", "maybe", "reject", "unreviewed"])
    for field in ("quality", "beauty", "memory"):
        decide.add_argument(f"--{field}", type=int, choices=[0, 1, 2, 3])
    decide.add_argument("--chapter"); decide.add_argument("--notes"); decide.add_argument("--order", type=_revision)
    decide.add_argument("--rotation", type=int, choices=[0, 90, 180, 270])
    suggest = command(review, "suggest", "review.suggest", "生成编排/已有评分/视觉建议，不替用户作决定")
    suggest.add_argument("workspace_id"); revision(suggest); input_json(suggest); wait_options(suggest)
    suggest.add_argument("--mode", choices=["organize", "existing_ai", "vision"], default="organize")
    suggest.add_argument("--limit", type=int, default=12)
    imp = command(review, "import", "review.import", "导入外部 agent 的建议 {items:{素材ID:建议}}")
    imp.add_argument("workspace_id"); revision(imp); input_json(imp, required=True)
    rescan = command(review, "rescan", "review.rescan", "重新扫描素材目录，保留已有人工决定")
    rescan.add_argument("workspace_id"); revision(rescan)
    proxy = command(review, "proxy", "review.proxy", "为浏览器制作最多60秒的视频代理，不修改原片")
    proxy.add_argument("workspace_id"); proxy.add_argument("asset_id")
    proxy.add_argument("--start", type=float, default=0.0); proxy.add_argument("--duration", type=_positive, default=60.0)
    wait_options(proxy)
    export = command(review, "export", "review.export", "导出用户选择、章节顺序和源片段")
    export.add_argument("workspace_id"); export.add_argument("--format", choices=["json", "csv"], default="json")
    export.add_argument("--all", action="store_true"); export.add_argument("--output", type=Path)
    for name, operation, help in [("wait", "review.wait", "等待工作区修订号变大"),
                                   ("changes", "review.changes", "与基线比较用户选择；AI 建议变化不计入用户增量")]:
        sub = command(review, name, operation, help)
        sub.add_argument("workspace_id")
        sub.add_argument("--timeout", type=_positive, default=60); sub.add_argument("--interval", type=_positive, default=1)
        if name == "wait":
            sub.add_argument("--after-revision", type=_revision, required=True)
        else:
            sub.add_argument("--baseline", type=Path, required=True)
            sub.add_argument("--wait", action="store_true")
    watch = command(review, "watch", "review.watch", "等待用户决定或工作区的新修订；不会把 AI 建议当作用户确认")
    watch.add_argument("--workspace", dest="workspace_id", required=True)
    checkpoint = watch.add_mutually_exclusive_group(required=True)
    checkpoint.add_argument("--after-revision", type=_revision)
    checkpoint.add_argument("--after-user-revision", type=_revision)
    watch.add_argument("--timeout", type=_positive, default=60)
    watch.add_argument("--interval", type=_positive, default=1)

    boards = groups.add_parser("storyboard", help="AI/外部 agent 剪辑草案和人工审核").add_subparsers(dest="action", required=True, parser_class=Parser)
    context = command(boards, "context", "storyboard.context", "读取人工选择约束和素材，供外部 agent 编排")
    context.add_argument("workspace_id"); context.add_argument("--include-unreviewed", action="store_true")
    for name in ("list", "get", "history", "export", "create", "import", "update"):
        sub = command(boards, name, f"storyboard.{name}", f"Storyboard {name}")
        sub.add_argument("workspace_id")
        if name in {"get", "history", "export", "update"}:
            sub.add_argument("proposal_id")
        if name == "get": sub.add_argument("--revision", type=_revision)
        if name in {"create", "import", "update"}:
            revision(sub, "--workspace-revision"); input_json(sub, required=name in {"import", "update"})
            if name == "update": revision(sub)
            else:
                sub.add_argument("--title", default=""); sub.add_argument("--brief", default="")
                sub.add_argument("--include-unreviewed", action="store_true")
                wait_options(sub)
                if name == "create": sub.add_argument("--mode", choices=["rules", "model"], default="rules")
        if name == "export":
            sub.add_argument("--format", choices=["json", "csv"], default="json"); sub.add_argument("--output", type=Path)
            sub.add_argument("--revision", type=_revision)

    photo = groups.add_parser("photo", help="主照片选片流程：工程、分组、AI评分与人工星级").add_subparsers(dest="action", required=True, parser_class=Parser)
    command(photo, "list", "photo.list", "列出照片工程")
    command(photo, "projects", "photo.projects", "列出照片工程（list别名）")
    command(photo, "runs", "photo.runs", "列出所有照片选片批次")
    project = command(photo, "project", "photo.project", "读取工程及版本"); project.add_argument("project_id")
    create = command(photo, "create", "photo.create", "创建照片工程（需要选片模型就绪）"); create.add_argument("--input-root", required=True)
    get = command(photo, "open-run", "photo.open-run", "读取选片批次与人工星级"); get.add_argument("run_id")
    group = command(photo, "group", "photo.group", "为工程生成新分组批次，保留旧版本")
    group.add_argument("project_id"); input_json(group); wait_options(group)
    score = command(photo, "score", "photo.score", "按当前分组与修订号运行 AI 评分")
    score.add_argument("run_id"); revision(score); input_json(score); wait_options(score)
    for name, help in [("rate", "批量修改星级；JSON必须显式包含indexes和rating（null恢复AI分数）"),
                       ("groups", "调整素材所属分组"), ("reorder", "保存分组展示顺序"),
                       ("exclude", "从工程排除或恢复素材"), ("delete-photos", "按确认字符串移除选中源文件，沿用API回收保护")]:
        sub = command(photo, name, f"photo.{name}", help)
        sub.add_argument("run_id"); revision(sub); input_json(sub, required=True)
        if name == "delete-photos": sub.add_argument("--confirmation", required=True, choices=["delete-source-files"])
    delete = command(photo, "delete-project", "photo.delete-project", "删除工具箱工程记录；需要原API确认文本")
    delete.add_argument("project_id"); delete.add_argument("--confirmation", required=True, choices=["删除工程"])

    tools = groups.add_parser("tools", help="RAW/成片管理与XMP文件工具；预览和执行严格分开").add_subparsers(dest="action", required=True, parser_class=Parser)
    command(tools, "framepick-status", "tools.framepick-status", "查看工具箱内帧选组件与项目存储目录")
    command(tools, "framepick-open", "tools.framepick-open", "打开工具箱内帧选窗口；不会操作原视频")
    for name in ("raw-jpeg-preview", "xmp-cleanup-preview"):
        sub = command(tools, name, f"tools.{name}", "生成扫描计划，只预览不执行文件变更")
        input_json(sub, required=True)
    for name in ("raw-jpeg-execute", "xmp-cleanup-execute"):
        sub = command(tools, name, f"tools.{name}", "执行已经审阅的计划；必须明确提供plan-id")
        sub.add_argument("--plan-id", required=True); wait_options(sub)
    rollback = command(tools, "raw-jpeg-rollback", "tools.raw-jpeg-rollback", "按事务ID撤销RAW/成片整理")
    rollback.add_argument("--transaction-id", required=True); wait_options(rollback)
    transactions = command(tools, "transactions", "tools.transactions", "列出文件事务")
    transactions.add_argument("--kind", choices=["xmp", "raw-jpeg", "xmp-cleanup"], default="xmp")
    rollback = command(tools, "rollback", "tools.rollback", "撤销本工具创建的XMP；必须明确填写原API确认文本")
    rollback.add_argument("--transaction-id", required=True)
    rollback.add_argument("--confirmation", required=True, choices=["撤销 XMP"]); wait_options(rollback)

    timelapse = groups.add_parser("timelapse", help="LRTLite 序列处理与视频合成").add_subparsers(dest="action", required=True, parser_class=Parser)
    command(timelapse, "list", "timelapse.list", "列出延时摄影序列")
    get = command(timelapse, "get", "timelapse.get", "读取序列、修订号和处理结果"); get.add_argument("session_id")
    scan = command(timelapse, "scan", "timelapse.scan", "扫描图像序列")
    scan.add_argument("--input-root", required=True); wait_options(scan)
    for name in ("process", "render"):
        sub = command(timelapse, name, f"timelapse.{name}", "分析过渡和去闪" if name == "process" else "合成视频")
        sub.add_argument("session_id"); revision(sub); input_json(sub); wait_options(sub)
        if name == "process":
            sub.add_argument("--keyframe-mode", choices=["auto", "even", "existing", "manual"])
            sub.add_argument("--keyframe-count", type=int); sub.add_argument("--transition", action="store_true", default=None)
            sub.add_argument("--strength", type=float); sub.add_argument("--smooth-window", type=int)
            sub.add_argument("--no-deflicker", action="store_true"); sub.add_argument("--no-holy-grail", action="store_true")
        else:
            sub.add_argument("--fps", type=float); sub.add_argument("--width", type=int); sub.add_argument("--height", type=int)
            sub.add_argument("--crf", type=int); sub.add_argument("--apply-corrections", action="store_true", default=None)
            sub.add_argument("--allow-mixed-dimensions", action="store_true", default=None)
    jobs = command(timelapse, "jobs", "timelapse.jobs", "列出任务或读取指定任务"); jobs.add_argument("--job-id")
    cancel = command(timelapse, "cancel", "timelapse.cancel", "取消延时任务"); cancel.add_argument("job_id")

    selection = groups.add_parser("selection", help="AI 选片工程的选择导出").add_subparsers(dest="action", required=True, parser_class=Parser)
    command(selection, "list", "selection.list", "列出 AI 选片批次")
    for name in ("get", "export", "copy", "xmp"):
        sub = command(selection, name, f"selection.{name}", f"Selection {name}")
        sub.add_argument("run_id"); sub.add_argument("--min-rating", type=int, choices=range(1, 6), default=3)
        if name == "export":
            sub.add_argument("--format", choices=["json", "csv"], default="json"); sub.add_argument("--output", type=Path)
        if name in {"copy", "xmp"}:
            revision(sub); wait_options(sub)
        if name == "copy":
            sub.add_argument("--output-dir", required=True); sub.add_argument("--no-xmp", action="store_true")

    job = groups.add_parser("job", help="统一任务查询、等待和取消").add_subparsers(dest="action", required=True, parser_class=Parser)
    for name in ("get", "wait", "cancel"):
        sub = command(job, name, f"job.{name}", f"Job {name}")
        sub.add_argument("kind", choices=["review", "proxy", "storyboard", "timelapse", "copy", "service"])
        sub.add_argument("job_id"); sub.add_argument("--workspace-id")
        if name == "wait":
            sub.add_argument("--timeout", type=_positive, default=120); sub.add_argument("--interval", type=_positive, default=1)
            sub.add_argument("--cancel-on-timeout", action="store_true")
    return parser


def _unwrap(data):
    return data.get("data") if isinstance(data, dict) and data.get("ok") is True and "data" in data else data


def _read_json(path: Path | None, *, required_object=True):
    if path is None: return {}
    try:
        text = sys.stdin.read(16 * 1024 * 1024 + 1) if str(path) == "-" else path.read_text(encoding="utf-8-sig")
        if len(text.encode("utf-8")) > 16 * 1024 * 1024:
            raise ValueError("JSON exceeds 16 MiB")
        def invalid_constant(value): raise ValueError(f"non-finite number: {value}")
        data = _unwrap(json.loads(text, parse_constant=invalid_constant))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CliError("invalid_json", f"无法读取 JSON 输入：{exc}", exit_code=2) from None
    if required_object and not isinstance(data, dict):
        raise CliError("invalid_json", "JSON 输入必须是对象。", exit_code=2)
    return data


def _url(origin: str) -> str:
    try:
        parts = urllib.parse.urlsplit(origin)
        valid = (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}
                 and parts.port is not None and 0 < parts.port <= 65535 and not parts.username and not parts.password
                 and not parts.query and not parts.fragment and parts.path in {"", "/"})
    except ValueError:
        valid = False
    if not valid:
        raise CliError("invalid_url", "服务地址必须是带端口的本机 HTTP 回环地址，不能包含凭据或查询参数。", exit_code=2)
    return origin.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CliError("unexpected_redirect", "服务返回了重定向，已停止以保护本机连接凭据。")


class HttpClient:
    def __init__(self, origin: str, *, headers=None, timeout=30):
        self.origin, self.headers, self.timeout = _url(origin), dict(headers or {}), timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.bootstrap = self.request("GET", "/api/bootstrap")
        if not isinstance(self.bootstrap, dict) or not isinstance(self.bootstrap.get("token"), str):
            raise CliError("incompatible_service", "地址未返回受支持的工具箱 bootstrap。")
        self.headers["X-Photo-AI-Token"] = self.bootstrap["token"]

    def request(self, method, path, body=None, *, raw=False):
        headers = {"Accept": "application/json", **self.headers}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.origin + path, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read()
                return content if raw else json.loads(content.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try: detail = json.loads(exc.read(1024 * 1024).decode("utf-8")).get("detail", "请求失败")
            except (ValueError, UnicodeError, AttributeError): detail = "请求失败"
            code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 410: "retired", 422: "validation_error"}.get(exc.code, "http_error")
            raise CliError(code, detail if isinstance(detail, str) else "请求参数未通过验证。",
                           exit_code=3 if exc.code == 409 else 1, details={"status": exc.code, "detail": detail}) from None
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise CliError("service_unavailable", "无法连接工具箱服务，或请求已超时。") from None
        except (ValueError, UnicodeError):
            raise CliError("invalid_response", "服务没有返回有效 JSON。") from None


def _data_dir(value: Path | None) -> Path | None:
    if value:
        path = value.expanduser().resolve()
        return path / "state" if (path / "marker.json").is_file() and (path / "state").is_dir() else path
    configured = os.environ.get("PHOTO_AI_DATA_DIR")
    if configured: return Path(configured).expanduser().resolve()
    from .content_root import read_content_root_pointer
    root = os.environ.get("PHOTO_AI_CONTENT_ROOT") or read_content_root_pointer()
    return Path(root).expanduser().resolve() / "state" if root else None


def _connection(args, data_dir):
    if args.offline:
        if args.url or args.connection_file:
            raise CliError("usage_error", "--offline 不能与 --url 或 --connection-file 同时使用。", exit_code=2)
        return None
    path = args.connection_file
    if path is None and data_dir and (not args.url or args.data_dir is not None):
        path = next((p for p in [data_dir / "cli-session.json", data_dir / "state" / "cli-session.json",
                               data_dir.parent / "state" / "cli-session.json"] if p.is_file()), None)
    if path:
        try:
            from .agent_connection import load_connection
            connection = load_connection(path)
        except Exception as exc:
            code = getattr(exc, "code", "credential_unavailable" if isinstance(exc, PermissionError) or "凭据" in str(exc)
                           else "service_unavailable" if isinstance(exc, ConnectionError) else "connection_unavailable")
            raise CliError(str(code), "连接记录不可用；请重开服务，或用 --offline 执行只读查询。") from None
        origin = _url(connection["origin"])
        if args.url and _url(args.url) != origin:
            raise CliError("connection_mismatch", "--url 与连接文件记录的服务地址不同。", exit_code=2)
        return HttpClient(origin, headers=connection["headers"], timeout=args.request_timeout)
    if args.url:
        return HttpClient(args.url, timeout=args.request_timeout)
    return None


def _qid(value): return urllib.parse.quote(str(value), safe="")


def _job_path(kind, job_id, workspace_id=None):
    if kind in {"review", "proxy", "storyboard"}:
        if not workspace_id: raise CliError("usage_error", "此任务需要 --workspace-id。", exit_code=2)
        segment = {"review": "jobs", "proxy": "proxy-jobs", "storyboard": "storyboard-jobs"}[kind]
        return f"/api/review-workspaces/{_qid(workspace_id)}/{segment}/{_qid(job_id)}"
    return {"timelapse": "/api/toolbox/timelapse/jobs/", "copy": "/api/selection-copies/", "service": "/api/jobs/"}[kind] + _qid(job_id)


def _job(client, kind, job_id, workspace_id=None):
    answer = client.request("GET", _job_path(kind, job_id, workspace_id))
    return answer.get("job", answer)


def _wait_job(client, kind, job_id, workspace_id, timeout, interval, cancel_on_timeout=False):
    deadline = time.monotonic() + timeout
    while True:
        value = _job(client, kind, job_id, workspace_id)
        status = value.get("status")
        if status in TERMINAL_ERRORS:
            raise CliError("job_" + status, value.get("error") or value.get("message") or f"任务{status}。", details={"job": value})
        if status in {"done", "completed"}: return value
        if status not in ACTIVE:
            raise CliError("unknown_job_status", f"未知任务状态：{status}", details={"job": value})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if cancel_on_timeout: client.request("POST", _job_path(kind, job_id, workspace_id) + "/cancel", {})
            raise CliError("wait_timeout", "等待超时；任务" + ("已请求取消。" if cancel_on_timeout else "仍可查询或取消。"),
                           exit_code=4, details={"job": value, "cancel_requested": cancel_on_timeout})
        time.sleep(min(interval, remaining))


def _maybe_wait(client, result, kind, args):
    if not getattr(args, "wait", False): return result
    job = result.get("job", result)
    job_id = job.get("job_id") or job.get("id")
    if not job_id: return result
    return _wait_job(client, kind, job_id, getattr(args, "workspace_id", None), args.wait_timeout, args.interval, args.cancel_on_timeout)


def _deliver(raw: bytes, format: str, output: Path | None):
    if output:
        path = output.expanduser().resolve()
        try:
            with path.open("xb") as stream: stream.write(raw)
        except FileExistsError:
            raise CliError("output_exists", "输出文件已存在，未覆盖。", exit_code=3) from None
        return {"path": str(path), "bytes": len(raw), "format": format}
    return json.loads(raw.decode("utf-8-sig")) if format == "json" else {"format": "csv", "content": raw.decode("utf-8-sig")}


def _workspace(data):
    data["user_revision"] = sum(int(item.get("user", {}).get("revision", 0)) for item in data.get("items", []))
    return data


def _changes(current, baseline):
    if baseline.get("workspace_id") != current.get("workspace_id"):
        raise CliError("baseline_mismatch", "基线属于另一个审片工程。", exit_code=3)
    previous = {item["id"]: item.get("user", {}) for item in baseline.get("items", [])}
    items = [item for item in current["items"] if item["id"] not in previous
             or item.get("user", {}) != previous[item["id"]]]
    return {"workspace_id": current["workspace_id"], "revision": current["revision"], "user_revision": current["user_revision"],
            "changed": bool(items), "items": items, "user_revisions": {item["id"]: item["user"]["revision"] for item in current["items"]}}


def _read_wait(args, fetch):
    baseline = _read_json(args.baseline) if args.operation == "review.changes" else None
    deadline = time.monotonic() + args.timeout
    while True:
        current = _workspace(fetch())
        answer = _changes(current, baseline) if baseline is not None else current
        after_user = getattr(args, "after_user_revision", None)
        changed = (answer["changed"] if baseline is not None else
                   current["user_revision"] > after_user if after_user is not None else
                   int(current["revision"]) > args.after_revision)
        if changed or (baseline is not None and not args.wait): return answer
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CliError("wait_timeout", "等待期间没有新的审片修改。", exit_code=4,
                           details={"workspace_id": args.workspace_id, "revision": current["revision"], "user_revision": current["user_revision"]})
        time.sleep(min(args.interval, remaining))


def execute_http(args, client):
    op = args.operation
    if op == "capabilities":
        return {"protocol": PROTOCOL, "transport": "http", "origin": client.origin,
                "modules": ["selection", "review", "storyboard", "timelapse"],
                "timelapse": client.request("GET", "/api/toolbox/timelapse/capabilities"),
                "user_decisions_separate_from_ai": True}
    if op.startswith("review."):
        root = "/api/review-workspaces"
        path = root + "/" + _qid(args.workspace_id) if hasattr(args, "workspace_id") else root
        if op == "review.list": return client.request("GET", root)
        if op == "review.create": return client.request("POST", root, {"input_root": args.input_root, "name": args.name})
        if op == "review.rescan": return client.request("POST", path + "/rescan", {"revision": args.revision})
        if op == "review.proxy": return _maybe_wait(client, client.request("POST", path + "/proxy/" + _qid(args.asset_id), {"start": args.start, "duration": args.duration}), "proxy", args)
        if op == "review.get":
            data = _workspace(client.request("GET", path))
            return {"workspace_id": data["workspace_id"], "revision": data["revision"], "user_revision": data["user_revision"], "changed": False, "items": []} if args.since_revision is not None and data["revision"] <= args.since_revision else data
        if op in {"review.wait", "review.watch", "review.changes"}: return _read_wait(args, lambda: client.request("GET", path))
        if op == "review.export":
            query = urllib.parse.urlencode({"format": args.format, "selected_only": str(not args.all).lower()})
            return _deliver(client.request("GET", path + "/export?" + query, raw=True), args.format, args.output)
        body = _read_json(args.input_json)
        if op == "review.decide":
            for key in ("status", "quality", "beauty", "memory", "chapter", "notes", "order", "rotation"):
                if getattr(args, key) is not None: body[key] = getattr(args, key)
            body["revision"] = args.revision
            return client.request("PATCH", path + "/items/" + _qid(args.asset_id), body)
        body["revision"] = args.revision
        if op == "review.import": return client.request("POST", path + "/suggestions", body)
        body.update(mode=args.mode, limit=args.limit)
        return _maybe_wait(client, client.request("POST", path + "/suggest", body), "review", args)
    if op.startswith("storyboard."):
        root = f"/api/review-workspaces/{_qid(args.workspace_id)}/storyboards"
        if op == "storyboard.context":
            return client.request("GET", f"/api/review-workspaces/{_qid(args.workspace_id)}/storyboard-context?include_unreviewed={str(args.include_unreviewed).lower()}")
        path = root + "/" + _qid(args.proposal_id) if hasattr(args, "proposal_id") else root
        if op == "storyboard.list": return client.request("GET", root)
        if op == "storyboard.get": return client.request("GET", path + (f"?revision={args.revision}" if args.revision is not None else ""))
        if op == "storyboard.history": return client.request("GET", path + "/history")
        if op == "storyboard.export": return _deliver(client.request("GET", path + "/export?format=" + args.format + (f"&revision={args.revision}" if args.revision is not None else ""), raw=True), args.format, args.output)
        body = _read_json(args.input_json)
        if op == "storyboard.import":
            body = body.get("proposal", body)
            if not isinstance(body, dict): raise CliError("invalid_json", "导入草案必须是对象。", exit_code=2)
            body = {key: body[key] for key in ("title", "brief", "chapters", "evidence") if key in body}
        body["workspace_revision"] = args.workspace_revision
        if op == "storyboard.update":
            body["revision"] = args.revision
            return client.request("PATCH", path, body)
        body.update(mode="import" if op == "storyboard.import" else args.mode,
                    title=args.title or body.get("title", ""), brief=args.brief or body.get("brief", ""),
                    include_unreviewed=args.include_unreviewed)
        return _maybe_wait(client, client.request("POST", root, body), "storyboard", args)
    if op.startswith("photo."):
        if op in {"photo.list", "photo.projects"}: return client.request("GET", "/api/projects")
        if op == "photo.runs": return client.request("GET", "/api/runs")
        if op == "photo.project": return client.request("GET", "/api/projects/" + _qid(args.project_id))
        if op == "photo.create": return client.request("POST", "/api/projects", {"input_path": args.input_root})
        if op == "photo.open-run": return client.request("GET", "/api/runs/" + _qid(args.run_id))
        if op == "photo.delete-project": return client.request("DELETE", "/api/projects/" + _qid(args.project_id), {"confirmation": args.confirmation})
        body = _read_json(args.input_json)
        if op == "photo.group": return _maybe_wait(client, client.request("POST", "/api/projects/" + _qid(args.project_id) + "/group", body), "service", args)
        body["base_revision"] = args.revision
        root = "/api/runs/" + _qid(args.run_id)
        if op == "photo.score": return _maybe_wait(client, client.request("POST", root + "/score", body), "service", args)
        if op == "photo.rate" and not {"indexes", "rating"} <= set(body):
            raise CliError("invalid_json", "星级修改必须显式包含indexes和rating；rating:null表示恢复AI星级。", exit_code=2)
        if op == "photo.delete-photos":
            body["confirmation"] = args.confirmation
            return client.request("POST", root + "/photos/delete", body)
        suffix = {"photo.rate": "ratings", "photo.groups": "groups", "photo.reorder": "group-order", "photo.exclude": "excluded"}[op]
        return client.request("PATCH", root + "/" + suffix, body)
    if op.startswith("tools."):
        if op == "tools.framepick-status": return client.request("GET", "/api/toolbox/framepick")
        if op == "tools.framepick-open": return client.request("POST", "/api/toolbox/framepick/launch", {})
        if op == "tools.transactions":
            return client.request("GET", "/api/transactions" if args.kind == "xmp" else f"/api/tools/{args.kind}/transactions")
        if op == "tools.rollback":
            return _maybe_wait(client, client.request("POST", "/api/transactions/rollback", {"transaction_id": args.transaction_id, "confirmation": args.confirmation}), "service", args)
        area, action = args.action.rsplit("-", 1)
        body = (_read_json(args.input_json) if action == "preview" else
                {"plan_id": args.plan_id} if action == "execute" else {"transaction_id": args.transaction_id})
        result = client.request("POST", f"/api/tools/{area}/{action}", body)
        return result if action == "preview" else _maybe_wait(client, result, "service", args)
    if op.startswith("timelapse."):
        root = "/api/toolbox/timelapse"
        if op == "timelapse.list": return client.request("GET", root + "/sessions")
        if op == "timelapse.get": return client.request("GET", root + "/sessions/" + _qid(args.session_id))
        if op == "timelapse.jobs": return client.request("GET", root + "/jobs" + ("/" + _qid(args.job_id) if args.job_id else ""))
        if op == "timelapse.cancel": return client.request("POST", root + "/jobs/" + _qid(args.job_id) + "/cancel", {})
        if op == "timelapse.scan": return _maybe_wait(client, client.request("POST", root + "/scan", {"input_root": args.input_root}), "timelapse", args)
        body = _read_json(args.input_json)
        keys = ("keyframe_mode", "keyframe_count", "transition", "strength", "smooth_window") if op == "timelapse.process" else ("fps", "width", "height", "crf", "apply_corrections", "allow_mixed_dimensions")
        for key in keys:
            if getattr(args, key) is not None: body[key] = getattr(args, key)
        if op == "timelapse.process":
            if args.no_deflicker: body["deflicker"] = False
            if args.no_holy_grail: body["holy_grail"] = False
        body["base_revision"] = args.revision
        return _maybe_wait(client, client.request("POST", root + "/sessions/" + _qid(args.session_id) + "/" + args.action, body), "timelapse", args)
    if op.startswith("selection."):
        if op == "selection.list": return client.request("GET", "/api/runs")
        path = f"/api/runs/{_qid(args.run_id)}/selection-export"
        if op == "selection.get": return client.request("GET", path + f"?min_rating={args.min_rating}")
        if op == "selection.export":
            return _deliver(client.request("GET", path + f"/download?format={args.format}&min_rating={args.min_rating}", raw=True), args.format, args.output)
        body = {"base_revision": args.revision, "min_rating": args.min_rating}
        if op == "selection.copy": body.update(output_dir=args.output_dir, include_xmp=not args.no_xmp)
        return _maybe_wait(client, client.request("POST", path + "/" + args.action, body), "copy" if args.action == "copy" else "service", args)
    if op.startswith("job."):
        if args.action == "get": return _job(client, args.kind, args.job_id, args.workspace_id)
        if args.action == "cancel": return client.request("POST", _job_path(args.kind, args.job_id, args.workspace_id) + "/cancel", {})
        return _wait_job(client, args.kind, args.job_id, args.workspace_id, args.timeout, args.interval, args.cancel_on_timeout)
    raise CliError("unsupported_command", op, exit_code=2)


def _read_file(path: Path):
    if not path.is_file(): raise CliError("not_found", "本地记录不存在。")
    return _read_json(path)


def _record_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value): raise CliError("invalid_id", "记录编号无效。", exit_code=2)
    return value


def _offline_selection(data_dir, run_id, min_rating):
    from .selection_export import selection_manifest
    root = data_dir.parent / "projects" if data_dir.name == "state" and (data_dir.parent / "marker.json").is_file() else data_dir / "runs"
    folder = root / _record_id(run_id)
    payload = _read_file(folder / "results.json")
    review = _read_file(folder / "review.json") if (folder / "review.json").is_file() else {}
    payload["review_revision"] = int(review.get("revision", 0))
    for index, item in enumerate(payload.get("results", [])):
        key = str(index)
        item["ai_rating"] = item.get("rating", 0)
        if key in review.get("ratings", {}):
            item["rating"] = review["ratings"][key]; item["manual_override"] = True
        if key in review.get("groups", {}): item["group_id"] = review["groups"][key]
        if key in review.get("excluded", {}): item["excluded"] = review["excluded"][key]
        if key in review.get("deleted", {}): item["deleted"] = True
    return selection_manifest(payload, min_rating)


def execute_offline(args, data_dir):
    op = args.operation
    if op == "capabilities":
        return {"protocol": PROTOCOL, "transport": "offline", "data_dir": str(data_dir) if data_dir else None,
                "read_only": True, "mutations_require_service": True,
                "modules": ["selection", "review", "storyboard", "timelapse"]}
    readonly = {"review.list", "review.get", "review.export", "review.wait", "review.watch", "review.changes",
                "storyboard.list", "storyboard.get", "storyboard.history", "storyboard.export", "storyboard.context",
                "timelapse.list", "timelapse.get", "timelapse.jobs", "selection.list", "selection.get", "selection.export"}
    if op not in readonly:
        raise CliError("service_required", "此操作需要已运行的工具箱服务。使用 --connection-file、--url，或在独立进程运行 serve --content-root。", exit_code=5)
    if not data_dir or not data_dir.is_dir(): raise CliError("data_dir_required", "离线读取需要存在的 --data-dir。", exit_code=2)
    if op.startswith("review.") or op.startswith("storyboard."):
        from .collaborative_review import ReviewStore, export_csv
        store = ReviewStore(data_dir)
        if op == "review.list": return {"workspaces": store.list()}
        if op == "review.get":
            data = _workspace(store.get(args.workspace_id))
            return {"workspace_id": data["workspace_id"], "revision": data["revision"], "user_revision": data["user_revision"], "changed": False, "items": []} if args.since_revision is not None and data["revision"] <= args.since_revision else data
        if op in {"review.wait", "review.watch", "review.changes"}: return _read_wait(args, lambda: store.get(args.workspace_id))
        if op == "review.export":
            data = store.export(args.workspace_id, selected_only=not args.all)
            raw = export_csv(data).encode("utf-8") if args.format == "csv" else json.dumps(data, ensure_ascii=False).encode("utf-8")
            return _deliver(raw, args.format, args.output)
        from .collaborative_storyboard import StoryboardStore
        boards = StoryboardStore(store)
        if op == "storyboard.context":
            return {"context": boards.context(args.workspace_id, store.get(args.workspace_id)["revision"], include_unreviewed=args.include_unreviewed)}
        if op == "storyboard.list": return {"proposals": boards.list(args.workspace_id)}
        if op == "storyboard.get": return {"proposal": boards.get(args.workspace_id, args.proposal_id, revision=args.revision)}
        if op == "storyboard.history": return {"revisions": boards.history(args.workspace_id, args.proposal_id)}
        data = boards.export(args.workspace_id, args.proposal_id, revision=args.revision)
        if args.format == "csv":
            from .collaborative_storyboard import storyboard_csv
            raw = storyboard_csv(data).encode("utf-8")
        else: raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return _deliver(raw, args.format, args.output)
    if op.startswith("timelapse."):
        root = data_dir / "toolbox" / "timelapse"
        if op == "timelapse.get": return _read_file(root / "sessions" / _record_id(args.session_id) / "session.json")
        if op == "timelapse.jobs":
            return _read_file(root / "jobs" / (_record_id(args.job_id) + ".json")) if args.job_id else [_read_file(p) for p in sorted((root / "jobs").glob("*.json"))]
        return [{k: v for k, v in _read_file(p).items() if k not in {"frames", "timeline"}} for p in sorted((root / "sessions").glob("*/session.json"))]
    if op == "selection.list":
        root = data_dir.parent / "projects" if data_dir.name == "state" and (data_dir.parent / "marker.json").is_file() else data_dir / "runs"
        return [{"run_id": p.parent.name, "input_root": _read_file(p).get("input_root")} for p in sorted(root.glob("*/results.json"), reverse=True)]
    manifest = _offline_selection(data_dir, args.run_id, args.min_rating)
    if op == "selection.get": return manifest
    from .selection_export import selection_download
    return _deliver(selection_download(manifest, args.format).body, args.format, args.output)


def command_schema():
    result = {"protocol": PROTOCOL, "output": {"success": {"ok": True, "command": "...", "data": {}},
              "error": {"ok": False, "command": "...", "error": {"code": "...", "message": "...", "details": {}}}},
              "exit_codes": {"0": "success", "1": "operation/http error", "2": "invalid arguments/input", "3": "revision/output conflict", "4": "wait timeout", "5": "service required", "130": "interrupted"},
              "rules": ["AI suggestions and user decisions are separate.", "Ratings use 0=unrated, 1..3.",
                        "review decide requires item.user.revision; suggestions require workspace.revision.",
                        "storyboard update requires proposal revision and workspace revision.",
                        "timelapse process/render and selection copy/xmp require --revision.",
                        "No credentials are accepted as literal command-line arguments.",
                        "Offline operations are read-only and never initialize task managers."], "commands": {}}
    result["rules"].extend(["user_revision is the sum of item.user.revision; AI updates do not change this marker.",
                             "review watch --after-user-revision observes human decisions; workspace revisions also include AI updates.",
                             "No implicit acceptance: importing/suggesting never changes user decisions or approves a storyboard."])
    def walk(parser):
        operation = parser.get_default("operation")
        if operation:
            result["commands"][operation] = {"help": parser.description,
                "arguments": [{"name": action.dest, "flags": action.option_strings, "required": action.required,
                               "default": action.default if isinstance(action.default, (str, int, float, bool, type(None))) else None,
                               "choices": list(action.choices) if action.choices is not None else None}
                              for action in parser._actions if action.dest not in {"help", "operation"}]}
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values(): walk(child)
    walk(build_parser())
    from .collaborative_review_api import ReviewItemBody, ReviewSuggestBody, ImportSuggestionsBody, CreateStoryboardBody, UpdateStoryboardBody
    from .timelapse_api import ProcessRequest, RenderRequest
    from .selection_export import SelectionCopyBody, SelectionExportBody
    for op, cls in {"review.decide": ReviewItemBody, "review.suggest": ReviewSuggestBody,
                    "review.import": ImportSuggestionsBody, "timelapse.process": ProcessRequest,
                    "timelapse.render": RenderRequest, "selection.copy": SelectionCopyBody,
                    "selection.xmp": SelectionExportBody, "storyboard.create": CreateStoryboardBody,
                    "storyboard.import": CreateStoryboardBody, "storyboard.update": UpdateStoryboardBody}.items():
        result["commands"][op]["request_schema"] = cls.model_json_schema()
    # Snapshot of the existing API request models, generated with default Web
    # startup deferred. Reading schema must never instantiate a second manager.
    from .toolbox_legacy_schemas import LEGACY_REQUEST_SCHEMAS
    for operation, metadata in LEGACY_REQUEST_SCHEMAS.items():
        result["commands"][operation].update(metadata)
    return result


def _serve(args, output):
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        if executable_dir.name.casefold() == "service" and executable_dir.parent.name.casefold() == "core":
            resources = executable_dir.parent.parent
        elif os.environ.get("PHOTO_AI_RESOURCE_ROOT"):
            resources = Path(os.environ["PHOTO_AI_RESOURCE_ROOT"]).resolve()
        else:
            raise CliError("incomplete_installation", "独立服务需要完整安装目录；请从安装目录的 core/service 运行 CLI。")
        worker = resources / "core" / "worker" / "PhotoAI.CoreWorker.exe"
        if not worker.is_file():
            raise CliError("incomplete_installation", "安装目录缺少 core/worker/PhotoAI.CoreWorker.exe。")
        os.environ["PHOTO_AI_RESOURCE_ROOT"] = str(resources)
        os.environ["PHOTO_AI_INSTALL_DIR"] = str(resources)
        os.environ["PHOTO_AI_CORE_WORKER_EXE"] = str(worker)
    from . import service
    class HandshakeFilter(io.TextIOBase):
        buffer = ""
        def write(self, text):
            self.buffer += text
            while "\n" in self.buffer:
                line, self.buffer = self.buffer.split("\n", 1)
                if line.startswith(service.PROTOCOL + " "):
                    try:
                        data = json.loads(line[len(service.PROTOCOL) + 1:])
                        answer = {"protocol": PROTOCOL, "ok": True, "command": "serve", "data": {
                            "origin": data.get("origin"), "pid": data.get("pid"),
                            "connection_file": str(args.content_root.resolve() / "state" / "cli-session.json"),
                            "mode": "foreground"}}
                        output.write(json.dumps(answer, ensure_ascii=False) + "\n"); output.flush()
                    except ValueError:
                        raise CliError("invalid_handshake", "服务握手无效。") from None
                elif line.strip():
                    sys.stderr.write(line + "\n")
            return len(text)
        def flush(self): pass
    with contextlib.redirect_stdout(HandshakeFilter()):
        return service.main(["--content-root", str(args.content_root), "--port", str(args.port)])


def main(argv=None) -> int:
    output = sys.stdout
    reconfigure = getattr(output, "reconfigure", None)
    if callable(reconfigure): reconfigure(encoding="utf-8", errors="strict")
    args = None
    try:
        args = build_parser().parse_args(argv)
        if args.operation == "serve": return _serve(args, output)
        if args.operation == "schema":
            data = command_schema()
            if args.command:
                if args.command not in data["commands"]: raise CliError("unknown_command", "Schema 中没有该命令。", exit_code=2)
                data = data["commands"][args.command]
            transport = "local"
        else:
            data_dir = _data_dir(args.data_dir)
            client = _connection(args, data_dir)
            with contextlib.redirect_stdout(sys.stderr):
                data = execute_http(args, client) if client else execute_offline(args, data_dir)
            transport = "http" if client else "offline"
        result = {"protocol": PROTOCOL, "ok": True, "command": args.operation, "data": data, "meta": {"transport": transport}}
        code = 0
    except KeyboardInterrupt:
        result = {"protocol": PROTOCOL, "ok": False, "command": getattr(args, "operation", None),
                  "error": {"code": "interrupted", "message": "CLI 等待已停止；服务上的任务可继续查询或显式取消。"}}
        code = 130
    except Exception as exc:
        error = exc if isinstance(exc, CliError) else CliError("operation_error", str(exc))
        result = {"protocol": PROTOCOL, "ok": False, "command": getattr(args, "operation", None),
                  "error": {"code": error.code, "message": str(error)}}
        if error.details is not None: result["error"]["details"] = error.details
        code = error.exit_code
    output.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2 if getattr(args, "pretty", False) else None) + "\n")
    output.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
