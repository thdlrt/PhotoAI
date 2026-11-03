"""Check a packaged CLI's independent serve mode in an isolated data directory."""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path


def verify(root: Path) -> dict:
    from landscape_culler.agent_connection import load_connection

    root = root.resolve(strict=True)
    executable = root / "core" / "service" / "PhotoToolbox.CLI.exe"
    if not executable.is_file():
        raise RuntimeError("Install payload is missing PhotoToolbox.CLI.exe")
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith(("PHOTO_AI_", "PHOTO_TOOLBOX_", "CONDA_", "PYTHON")) or name == "LRT_EXIFTOOL":
            environment.pop(name, None)
    environment["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix=".toolbox-cli-serve-qa-", dir=root.parent) as value:
        data = Path(value).resolve()
        if data.parent != root.parent:
            raise RuntimeError("Unexpected scratch directory")
        process = subprocess.Popen([str(executable), "serve", "--content-root", str(data)], cwd=data,
                                   env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict",
                                   creationflags=flags)
        try:
            lines = queue.Queue()
            threading.Thread(target=lambda: lines.put(process.stdout.readline(16384)), daemon=True).start()
            try:
                first_line = lines.get(timeout=20)
            except queue.Empty as exc:
                raise RuntimeError("Packaged CLI serve did not report readiness") from exc
            handshake = json.loads(first_line)
            if handshake.get("protocol") != "photography-toolbox/1" or not handshake.get("ok") or handshake.get("command") != "serve":
                raise RuntimeError("Packaged CLI serve handshake is invalid")
            connection_file = data / "state" / "cli-session.json"
            connection = load_connection(connection_file)
            descriptor = json.loads(connection_file.read_text(encoding="utf-8"))
            secrets = [*connection["headers"].values(), descriptor["credential"]["value"]]
            client = subprocess.run([str(executable), "--connection-file", str(connection_file), "capabilities"],
                                    cwd=data, env=environment, capture_output=True, text=True, encoding="utf-8",
                                    errors="strict", timeout=30, creationflags=flags)
            if any(secret in (first_line + client.stdout + client.stderr) for secret in secrets):
                raise RuntimeError("CLI output contains an authentication secret")
            payload = json.loads(client.stdout)
            if client.returncode or payload.get("protocol") != "photography-toolbox/1" or not payload.get("ok"):
                raise RuntimeError("Packaged standalone CLI capabilities failed")
            request = urllib.request.Request(connection["origin"] + "/api/toolbox/timelapse/capabilities",
                                             headers=connection["headers"])
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=10) as response:
                media = json.load(response)
            if not media.get("can_render"):
                raise RuntimeError("Standalone CLI could not discover bundled FFmpeg/FFprobe")
            for name in ("ffmpeg", "ffprobe", "exiftool"):
                path = Path(media.get(name) or "").resolve()
                if not path.is_file() or not path.is_relative_to(root):
                    raise RuntimeError(f"Standalone CLI does not use bundled {name}")
            return {"status": "passed", "install_root": str(root), "isolated_path": environment["PATH"],
                    "standalone_serve": True, "same_user_connection": True, "secret_free_output": True,
                    "bundled_media_tools": {name: media[name] for name in ("ffmpeg", "ffprobe", "exiftool")},
                    "private_scratch_data": True, "gui_launched": False}
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.install_root)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
