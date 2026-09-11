from __future__ import annotations

import argparse
import contextlib
import http.cookies
import json
import os
import secrets
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

PROTOCOL = "PHOTO_AI_SERVICE/1"
SESSION_COOKIE = "photo_ai_session"
_UNTRUSTED_OLLAMA_ENVIRONMENT = (
    "PHOTO_AI_OLLAMA_ENDPOINT",
    "PHOTO_AI_OLLAMA",
    "OLLAMA_HOST",
)
AsgiApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]


class _SessionGate:
    def __init__(self, bootstrap_token: str | None = None) -> None:
        self.bootstrap_token = bootstrap_token or secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self._consumed = False
        self._lock = threading.Lock()

    def exchange(self, candidate: str) -> bool:
        with self._lock:
            if self._consumed or not secrets.compare_digest(
                str(candidate), self.bootstrap_token
            ):
                return False
            self._consumed = True
            return True

    def valid_session(self, candidate: str | None) -> bool:
        return bool(candidate) and secrets.compare_digest(
            str(candidate), self.session_token
        )


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1").casefold(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _session_cookie(headers: dict[str, str]) -> str | None:
    cookie = http.cookies.SimpleCookie()
    try:
        cookie.load(headers.get("cookie", ""))
    except http.cookies.CookieError:
        return None
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel is not None else None


def _cookie_header(token: str) -> bytes:
    return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict").encode(
        "ascii"
    )


async def _plain_response(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    status: int,
    body: bytes,
    *,
    content_type: bytes = b"application/json; charset=utf-8",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    headers = [
        (b"content-type", content_type),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        *(extra_headers or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class ServiceApp:
    """ASGI authentication and lifecycle shell around the existing Web app."""

    def __init__(
        self,
        app: AsgiApp,
        bootstrap_token: str | None = None,
        control_token: str | None = None,
    ) -> None:
        self.app = app
        self.gate = _SessionGate(bootstrap_token)
        self._control_token = control_token or secrets.token_urlsafe(32)
        self.shutdown: Callable[[], None] | None = None

    @property
    def bootstrap_token(self) -> str:
        return self.gate.bootstrap_token

    @property
    def control_token(self) -> str:
        return self._control_token

    def valid_control_token(self, candidate: str | None) -> bool:
        return bool(candidate) and secrets.compare_digest(
            str(candidate), self._control_token
        )

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "/")
        if path == "/health":
            body = json.dumps(
                {"protocol": PROTOCOL, "status": "ok", "pid": os.getpid()},
                separators=(",", ":"),
            ).encode("utf-8")
            await _plain_response(send, 200, body)
            return

        headers = _headers(scope)
        session_valid = self.gate.valid_session(_session_cookie(headers))
        query_pairs = parse_qsl(
            bytes(scope.get("query_string") or b"").decode("utf-8", "replace"),
            keep_blank_values=True,
        )
        query_token = next(
            (value for key, value in query_pairs if key == "token"), None
        )
        authorization = headers.get("authorization", "")
        bearer = (
            authorization[7:].strip()
            if authorization.casefold().startswith("bearer ")
            else None
        )
        control_valid = path == "/api/service/shutdown" and self.valid_control_token(
            bearer
        )
        exchanged = False
        if (
            not session_valid
            and not control_valid
            and path != "/api/service/shutdown"
        ):
            candidate = query_token or bearer
            exchanged = bool(candidate) and self.gate.exchange(str(candidate))
            session_valid = exchanged

        if not session_valid and not control_valid:
            body = b'{"detail":"Unauthorized"}'
            await _plain_response(send, 401, body)
            return

        if query_token is not None and str(scope.get("method")) == "GET":
            clean_query = urlencode(
                [(key, value) for key, value in query_pairs if key != "token"],
                doseq=True,
            )
            location = path + (f"?{clean_query}" if clean_query else "")
            await _plain_response(
                send,
                303,
                b"",
                content_type=b"text/plain; charset=utf-8",
                extra_headers=[
                    (b"location", location.encode("utf-8")),
                    (b"set-cookie", _cookie_header(self.gate.session_token)),
                ],
            )
            return

        if path == "/api/service/shutdown":
            if str(scope.get("method") or "").upper() != "POST":
                await _plain_response(send, 405, b'{"detail":"Method Not Allowed"}')
                return
            await _plain_response(
                send,
                200,
                json.dumps(
                    {"protocol": PROTOCOL, "status": "stopping"},
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            if self.shutdown is not None:
                self.shutdown()
            return

        async def send_with_session(message: dict[str, Any]) -> None:
            if exchanged and message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    *list(message.get("headers") or []),
                    (b"set-cookie", _cookie_header(self.gate.session_token)),
                ]
            await send(message)

        await self.app(scope, receive, send_with_session)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PhotoAI local loopback service")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--content-root", type=Path)
    parser.add_argument("--bootstrap-content-root", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def _initialize_managed_content_root(root: Path) -> Any:
    """Initialize desktop storage without inheriting a developer Ollama.

    The standalone application owns both the portable Ollama executable and
    its per-job loopback endpoint.  Windows environment variables can survive
    an older development setup, so accepting them here could make a model
    delete request operate on an unrelated local Ollama instance.  The legacy
    browser entrypoint does not call this function and keeps its explicit
    development overrides.
    """

    from .content_root import initialize_content_root

    layout = initialize_content_root(
        root,
        apply_environment=True,
        persist_registry=False,
    )
    for name in _UNTRUSTED_OLLAMA_ENVIRONMENT:
        os.environ.pop(name, None)
    return layout


def _self_test() -> int:
    os.environ["PHOTO_AI_DEFER_DEFAULT_WEB_APP"] = "1"
    import fastapi
    import pydantic
    import uvicorn

    from . import web  # noqa: F401 - validates the packaged core import graph
    from .version import PRODUCT_VERSION

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
    if forbidden:
        print(json.dumps({"status": "failed", "unexpected_imports": forbidden}))
        return 1
    print(
        json.dumps(
            {
                "protocol": PROTOCOL,
                "status": "passed",
                "version": PRODUCT_VERSION,
                "dependencies": {
                    "fastapi": fastapi.__version__,
                    "pydantic": pydantic.__version__,
                    "uvicorn": uvicorn.__version__,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


def _parent_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        code = wintypes.DWORD()
        try:
            return bool(
                ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code))
            ) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _watch_parent(server: Any, pid: int) -> None:
    def watch() -> None:
        while not server.should_exit:
            if not _parent_process_alive(pid):
                server.should_exit = True
                return
            time.sleep(1)

    threading.Thread(target=watch, name="photoai-parent-watch", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.content_root is None:
        raise SystemExit("--content-root 是启动内部服务的必需参数。")
    if not 0 <= int(args.port) <= 65535:
        raise SystemExit("--port 必须是 0 到 65535。")

    handshake_stream = sys.stdout
    # Third-party initialization and the hosted app never own stdout. The
    # desktop parent receives exactly one machine-readable handshake line.
    with contextlib.redirect_stdout(sys.stderr):
        layout = _initialize_managed_content_root(args.content_root)
        import uvicorn

        os.environ["PHOTO_AI_DEFER_DEFAULT_WEB_APP"] = "1"
        from .web import PROJECT_ROOT, create_app

        inner = create_app(
            data_dir=layout.data_dir,
            project_root=PROJECT_ROOT,
            content_root=layout.root,
            bootstrap_storage=bool(args.bootstrap_content_root),
        )
        app = ServiceApp(inner)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(args.port),
            log_config=None,
            access_log=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        socket = config.bind_socket()
        port = int(socket.getsockname()[1])
        app.shutdown = lambda: setattr(server, "should_exit", True)

        parent_pid = int(os.environ.get("PHOTO_AI_PARENT_PID") or 0)
        if parent_pid:
            _watch_parent(server, parent_pid)

    handshake = {
        "port": port,
        "token": app.bootstrap_token,
        "control_token": app.control_token,
        "pid": os.getpid(),
        "origin": f"http://127.0.0.1:{port}",
    }
    print(
        f"{PROTOCOL} "
        + json.dumps(handshake, ensure_ascii=False, separators=(",", ":")),
        file=handshake_stream,
        flush=True,
    )
    with contextlib.redirect_stdout(sys.stderr):
        try:
            server.run(sockets=[socket])
        finally:
            socket.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
