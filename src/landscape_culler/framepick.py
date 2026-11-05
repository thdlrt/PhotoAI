"""Bundled FramePick launcher. Media and project decisions stay owned by FramePick."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


class FramePickLauncher:
    def __init__(self, data_dir: Path, project_root: Path):
        self.storage = data_dir / "framepick"
        self.project_root = project_root
        self._lock = threading.Lock()

    def executable(self) -> Path:
        resource = os.environ.get("PHOTO_AI_RESOURCE_ROOT")
        if resource:
            # Installed mode never adopts a source checkout or a PATH executable.
            return Path(resource) / "tools" / "framepick" / "FramePick.exe"
        if getattr(sys, "frozen", False):
            return Path(sys.executable).parent / "tools" / "framepick" / "FramePick.exe"
        return self.project_root / "integrations" / "framepick" / "output" / "FramePick" / "FramePick.exe"

    def status(self) -> dict:
        executable = self.executable()
        return {"available": os.name == "nt" and executable.is_file(), "version": "1.3.0",
                "project_directory": str(self.storage / "projects"), "storage_root": str(self.storage)}

    def launch_spec(self) -> dict:
        executable = self.executable().resolve()
        if os.name != "nt":
            raise ValueError("帧选需要 Windows 桌面环境。")
        if not executable.is_file():
            raise FileNotFoundError("未找到随工具箱安装的帧选。源码运行请先执行 packaging/build-framepick.ps1。")
        for name in ("projects", "state", "temp"):
            (self.storage / name).mkdir(parents=True, exist_ok=True)
        keys = {"SYSTEMROOT", "WINDIR", "PATH", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
                "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMSPEC", "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in keys}
        environment.update({"FRAMEPICK_DATA_ROOT": str(self.storage / "state"),
                            "FRAMEPICK_PROJECT_DIRECTORY": str(self.storage / "projects"),
                            "TEMP": str(self.storage / "temp"), "TMP": str(self.storage / "temp")})
        resource = os.environ.get("PHOTO_AI_RESOURCE_ROOT")
        media = Path(resource) / "tools" / "ffmpeg" if resource else self.project_root / "packaging" / "vendor" / "ffmpeg-8.0.1"
        if media.is_dir():
            environment["FRAMEPICK_FFMPEG_DIRECTORY"] = str(media.resolve())
        return {"command_line": subprocess.list2cmdline([str(executable)]), "working_directory": str(executable.parent),
                "environment": [key + "=" + value for key, value in sorted(environment.items())]}

    def launch(self) -> dict:
        # Windows WMI owns the launched GUI, outside the toolbox/Service process tree.
        with self._lock:
            payload = base64.b64encode(json.dumps(self.launch_spec(), ensure_ascii=False).encode("utf-8")).decode("ascii")
            script = f"""$ErrorActionPreference='Stop'
$spec=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{{ShowWindow=[UInt16]1;EnvironmentVariables=[string[]]$spec.environment}}
$result=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine=[string]$spec.command_line;CurrentDirectory=[string]$spec.working_directory;ProcessStartupInformation=$startup}}
if($result.ReturnValue -ne 0){{throw ('Windows 启动失败，错误码 '+$result.ReturnValue)}}
@{{pid=[int]$result.ProcessId}} | ConvertTo-Json -Compress
"""
            encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
            powershell = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise OSError("帧选启动失败，请确认 Windows WMI 服务可用。")
            try:
                pid = int(json.loads(result.stdout.strip().lstrip("\ufeff"))["pid"])
            except (KeyError, ValueError, TypeError) as exc:
                raise OSError("Windows 未返回有效的帧选启动回执。") from exc
            return {"launched": True, "pid": pid, "launch_mode": "windows_wmi", **self.status()}
