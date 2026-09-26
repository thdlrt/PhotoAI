"""Opt-in updates from this project's GitHub Releases; no model/project writes."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from .version import PRODUCT_VERSION

REPOSITORY = "thdlrt/PhotoAI"
ASSET = "PhotoAI-Setup-x64.exe"
API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"


def version_key(value: str) -> tuple:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?", value)
    if not match:
        raise ValueError("不支持的版本号")
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch), {"alpha": 0, "beta": 1, "rc": 2, None: 3}[stage], int(number or 0))


def select_release(releases: list, current: str = PRODUCT_VERSION) -> dict | None:
    candidates = []
    for release in releases:
        if release.get("draft") or (release.get("prerelease") and "-" not in current):
            continue
        tag = str(release.get("tag_name", ""))
        try:
            key = version_key(tag)
        except ValueError:
            continue
        if key <= version_key(current):
            continue
        for asset in release.get("assets", []):
            digest = str(asset.get("digest") or "")
            expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{ASSET}"
            if (asset.get("name") != ASSET or asset.get("state") != "uploaded"
                    or asset.get("browser_download_url") != expected_url
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
                    or not 0 < int(asset.get("size", 0)) < 1024**3):
                continue
            candidates.append((key, {"version": tag.removeprefix("v"), "tag": tag,
                "url": expected_url, "sha256": digest[7:], "size": int(asset["size"]),
                "notes": str(release.get("body") or "")[:12000],
                "page": f"https://github.com/{REPOSITORY}/releases/tag/{tag}"}))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


class GithubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
            raise ValueError("更新下载被重定向到不受信任的地址。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url: str, headers: dict | None = None):
    return urllib.request.build_opener(GithubRedirects()).open(urllib.request.Request(url,
        headers={"User-Agent": "PhotoAI-Updater", **(headers or {})}), timeout=20)


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def installer_script(installer: Path, install_dir: Path, desktop_pid: int, digest: str, log: Path) -> str:
    # Wait for the desktop to exit before invoking the existing transactional NSIS installer.
    return f"""$ErrorActionPreference='Stop'
try {{
  Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1"
  Import-Module "$PSHOME/Modules/Microsoft.PowerShell.Management/Microsoft.PowerShell.Management.psd1"
  $deadline=(Get-Date).AddSeconds(60)
  while (Get-Process -Id {desktop_pid} -ErrorAction SilentlyContinue) {{
    if ((Get-Date) -gt $deadline) {{ throw '程序未退出，更新已取消，请重试。' }}
    Start-Sleep -Milliseconds 250
  }}
  if ((Get-FileHash -LiteralPath {ps_quote(str(installer))} -Algorithm SHA256).Hash.ToLower() -ne '{digest}') {{ throw '安装包校验失败' }}
  $result=Start-Process -FilePath {ps_quote(str(installer))} -ArgumentList @('/S', {ps_quote('/D=' + str(install_dir))}) -Wait -PassThru
  if ($result.ExitCode -ne 0) {{ throw ('安装失败，退出码：' + $result.ExitCode) }}
  '安装成功' | Set-Content -LiteralPath {ps_quote(str(log))} -Encoding UTF8
}} catch {{
  $_.Exception.Message | Set-Content -LiteralPath {ps_quote(str(log))} -Encoding UTF8
}}
if (Test-Path -LiteralPath {ps_quote(str(install_dir / 'PhotoAI.exe'))}) {{ Start-Process -FilePath {ps_quote(str(install_dir / 'PhotoAI.exe'))} }}
"""


class AppUpdater:
    def __init__(self, root: Path):
        self.root = root / "downloads" / "app-updates"
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.installing = False
        self.state = {"phase": "idle", "current_version": PRODUCT_VERSION,
                      "release": None, "downloaded": 0, "speed": 0, "message": ""}

    def status(self):
        with self.lock:
            result = dict(self.state)
            log = self.root / "install-result.txt"
            if log.is_file():
                result["last_install"] = log.read_text(encoding="utf-8-sig")[:1000]
            return result

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, action: str):
        with self.lock:
            if self.state["phase"] in {"checking", "downloading", "installing"}:
                raise ValueError("更新操作正在进行。")
            if action == "download" and not self.state["release"]:
                raise ValueError("请先检查更新。")
            self.cancelled.clear()
            self._set(phase="checking" if action == "check" else "downloading", message="", speed=0)
            threading.Thread(target=self._work, args=(action,), daemon=True).start()
        return self.status()

    def _work(self, action: str):
        try:
            if action == "check":
                with open_url(API, {"Accept": "application/vnd.github+json"}) as response:
                    release = select_release(json.loads(response.read(4 * 1024 * 1024)))
                self._set(phase="available" if release else "current", release=release, downloaded=0)
            else:
                self._download()
        except Exception as exc:
            self._set(phase="cancelled" if self.cancelled.is_set() else "failed",
                      message="已取消，可继续下载。" if self.cancelled.is_set() else f"更新失败：{type(exc).__name__}。请检查网络后重试。", speed=0)

    def _download(self):
        release = self.state["release"]
        folder = self.root / release["version"]
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / (ASSET + ".part")
        final = folder / ASSET
        if final.is_file() and final.stat().st_size == release["size"]:
            with final.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == release["sha256"]:
                    self._set(phase="ready", downloaded=release["size"], speed=0)
                    return
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset >= release["size"]:
            partial.unlink()
            offset = 0
        if shutil.disk_usage(folder).free < release["size"] - offset + 100 * 1024**2:
            raise ValueError("数据盘空间不足")
        with open_url(release["url"], {"Range": f"bytes={offset}-"} if offset else {}) as response:
            if offset and response.status != 206:
                offset = 0
            if response.status == 206 and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                raise ValueError("下载续传范围不匹配")
            start, base = time.monotonic(), offset
            with partial.open("ab" if offset else "wb") as output:
                while chunk := response.read(256 * 1024):
                    if self.cancelled.is_set():
                        raise ValueError("cancelled")
                    offset += len(chunk)
                    if offset > release["size"]:
                        raise ValueError("下载大小异常")
                    output.write(chunk)
                    self._set(downloaded=offset, speed=(offset-base)/max(.01, time.monotonic()-start))
        with partial.open("rb") as stream:
            valid = offset == release["size"] and hashlib.file_digest(stream, "sha256").hexdigest() == release["sha256"]
        if not valid:
            partial.unlink()
            raise ValueError("安装包完整性校验失败")
        if self.cancelled.is_set():
            raise ValueError("cancelled")
        partial.replace(final)
        self._set(phase="ready", downloaded=offset, speed=0)

    def install(self):
        with self.lock:
            if self.state["phase"] != "ready":
                raise ValueError("请先完成安装包下载。")
            desktop_pid = int(os.environ.get("PHOTO_AI_DESKTOP_PID", "0"))
            install_dir = Path(os.environ.get("PHOTO_AI_INSTALL_DIR", ""))
            if desktop_pid <= 0 or not (install_dir / "PhotoAI.exe").is_file() or not (install_dir / ".photoai-install-marker").is_file():
                raise ValueError("请从桌面安装版执行更新。")
            release = self.state["release"]
            script = installer_script(self.root / release["version"] / ASSET, install_dir,
                                      desktop_pid, release["sha256"], self.root / "install-result.txt")
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            process = subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(script.encode("utf-16-le")).decode()],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=self.root)
            self.installing = True
            self._set(phase="installing", message="正在关闭程序并安装更新…")
            def watch_helper():
                process.wait()
                # If this service is still alive, the desktop did not exit.
                with self.lock:
                    self.installing = False
                    self._set(phase="failed", message="更新安装流程已结束，请查看安装结果；可重试或重启软件。")
            threading.Thread(target=watch_helper, daemon=True).start()
        return self.status()
