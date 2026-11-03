"""Measure preview working-set growth with real Pillow images and simulated DINO.

Run with the project Python. --baseline accepts a saved features.py for a
separate-process comparison. This measures host memory, not GPU throughput.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image


def peak_rss_bytes():
    if sys.platform == "win32":
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ["peak", "working", "peak_paged", "paged",
                                                  "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile"]]
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        api = ctypes.WinDLL("psapi")
        api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return counters.peak
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


class Tensor:
    def __init__(self, array): self.array = array
    def __getitem__(self, key): return Tensor(self.array[key])
    def to(self, _device): return self
    def float(self): return self
    def cpu(self): return self
    def numpy(self): return self.array


def measure(args):
    if args.module:
        spec = importlib.util.spec_from_file_location("landscape_culler._benchmark_features", args.module)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        from landscape_culler import features as module
    counts = {"open": 0, "peak": 0}
    def load(*_args, **_kwargs):
        image = Image.new("RGB", (args.width, args.height), (80, 120, 160))
        counts["open"] += 1
        counts["peak"] = max(counts["peak"], counts["open"])
        closed = False
        def release():
            nonlocal closed
            if not closed:
                counts["open"] -= 1
                closed = True
        original_close = image.close
        def close():
            release()
            original_close()
        image.close = close
        weakref.finalize(image, release)
        return image
    module.load_preview = load
    with tempfile.TemporaryDirectory(prefix="PhotoAI-memory-benchmark-") as temporary:
        folder = Path(temporary)
        paths = [folder / f"{index}.ARW" for index in range(args.count)]
        for path in paths: path.write_bytes(b"isolated benchmark source")
        extractor = module.FeatureExtractor(folder / "cache")
        extractor._ensure_model = lambda: None
        extractor._processor = lambda images, **kwargs: {"pixels": Tensor(np.arange(len(images)))}
        extractor._model = lambda pixels: SimpleNamespace(last_hidden_state=Tensor(
            np.ones((len(pixels.array), 1, 768), dtype=np.float32)))
        extractor._torch = SimpleNamespace(inference_mode=contextlib.nullcontext,
            nn=SimpleNamespace(functional=SimpleNamespace(normalize=lambda tensor, dim: Tensor(
                tensor.array / np.linalg.norm(tensor.array, axis=dim, keepdims=True)))),
            cuda=SimpleNamespace(is_available=lambda: False))
        started = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            matrix, actual = extractor.extract(paths, batch_size=args.batch_size)
        elapsed = time.perf_counter() - started
        assert actual == paths and matrix.shape == (args.count, 776)
        return {"implementation": "baseline" if args.module else "current", "photos":args.count,
            "preview_size":[args.width,args.height],"batch_size":args.batch_size,
            "peak_open_previews":counts["peak"],"peak_rss_bytes":peak_rss_bytes(),
            "elapsed_seconds":round(elapsed,3), "shape":list(matrix.shape)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=96)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/feature-memory-benchmark.json"))
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--module", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.count,args.width,args.height,args.batch_size) < 1:
        parser.error("count, dimensions and batch-size must be positive")
    if args.child:
        print(json.dumps(measure(args)))
        return
    rows = []
    for variant in ([args.baseline.resolve(), None] if args.baseline else [None]):
        command = [sys.executable, str(Path(__file__).resolve()), "--child", "--count", str(args.count),
                   "--width", str(args.width), "--height", str(args.height), "--batch-size",str(args.batch_size)]
        if variant: command += ["--module",str(variant)]
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=180)
        if process.returncode: raise RuntimeError(process.stderr)
        rows.append(json.loads(process.stdout))
    report = {"scope":"separate processes; real Pillow buffers and technical features; simulated GPU inference",
              "results":rows}
    if len(rows)==2:
        report["peak_rss_reduction_percent"] = round((1-rows[1]["peak_rss_bytes"]/rows[0]["peak_rss_bytes"])*100,1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == "__main__": main()
