"""Pre-cache Microsoft's offline installer without a short whole-file timeout."""
from pathlib import Path
import re
import time
import urllib.request

URL = "https://go.microsoft.com/fwlink/?linkid=2124701"
PREFIX = "https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/"
ROOT = Path(__file__).resolve().parents[1] / "desktop/src-tauri/target/.tauri/x64"


def main():
    with urllib.request.urlopen(urllib.request.Request(URL, method="HEAD"), timeout=30) as response:
        resolved = response.url
    if not resolved.startswith(PREFIX):
        raise RuntimeError("Unexpected Microsoft download host")
    relative = resolved.removeprefix(PREFIX)
    if not re.fullmatch(r"[a-fA-F0-9-]+/MicrosoftEdgeWebView2RuntimeInstallerX64\.exe", relative):
        raise RuntimeError("Unexpected Microsoft installer path")
    target = ROOT / relative
    if target.is_file():
        print(f"Cached installer: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".exe.part")
    offset = part.stat().st_size if part.is_file() else 0
    request = urllib.request.Request(resolved, headers={"Range": f"bytes={offset}-"} if offset else {})
    with urllib.request.urlopen(request, timeout=60) as response:
        resumed = offset if response.status == 206 else 0
        total = resumed + int(response.headers["Content-Length"])
        written = resumed
        started = last = time.monotonic()
        with part.open("ab" if resumed else "wb") as handle:
            while block := response.read(1024 * 1024):
                handle.write(block)
                written += len(block)
                now = time.monotonic()
                if now - last >= 5:
                    print(f"WebView2: {written / 1048576:.1f}/{total / 1048576:.1f} MiB, {(written - resumed) / (now - started) / 1048576:.1f} MiB/s", flush=True)
                    last = now
        if written != total:
            raise RuntimeError("Incomplete Microsoft installer; rerun to resume")
    part.replace(target)
    print(f"Cached installer: {target}")


if __name__ == "__main__":
    main()
