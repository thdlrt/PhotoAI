from concurrent.futures import ThreadPoolExecutor
import ctypes
import errno
import os
import threading
import time

import pytest

from landscape_culler import util


def windows_lock_error():
    error = PermissionError("locked")
    error.winerror = 5
    return error


def test_atomic_write_retries_windows_lock_and_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "progress.json"
    target.write_text("old", encoding="utf-8")
    real_replace = os.replace
    calls = []

    def replace(source, destination):
        calls.append(source)
        if len(calls) < 4:
            assert target.read_text() == "old"
            raise windows_lock_error()
        real_replace(source, destination)

    delays = []
    monkeypatch.setattr(util.os, "replace", replace)
    monkeypatch.setattr(util.time, "sleep", delays.append)
    util.atomic_write_text(target, "new")
    assert target.read_text() == "new"
    assert len(calls) == 4 and delays == [.02, .04, .08]
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("error,retries", [(windows_lock_error(), 10), (OSError(errno.ENOSPC, "full"), 1)])
def test_atomic_write_reports_persistent_errors_without_truncating_target(tmp_path, monkeypatch, error, retries):
    target = tmp_path / "result.json"
    target.write_text("previous", encoding="utf-8")
    calls = []

    def replace(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(util.os, "replace", replace)
    monkeypatch.setattr(util.time, "sleep", lambda _: None)
    with pytest.raises(OSError):
        util.atomic_write_text(target, "next")
    assert len(calls) == retries and target.read_text() == "previous"
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_same_process_concurrent_writers_use_unique_temps(tmp_path, monkeypatch):
    target = tmp_path / "progress.json"
    real_replace = os.replace
    barrier = threading.Barrier(8)
    sources = []

    def replace(source, destination):
        first_attempt = source not in sources
        if first_attempt:
            sources.append(source)
            barrier.wait(timeout=10)
        real_replace(source, destination)

    monkeypatch.setattr(util.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: util.write_json(target, {"index": i, "data": "x" * 10000}), range(8)))
    assert len(set(sources)) == 8
    assert util.read_json(target)["index"] in range(8)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
def test_atomic_write_recovers_from_real_windows_reader_lock(tmp_path):
    from ctypes import wintypes
    target = tmp_path / "develop-progress.json"
    util.write_json(target, {"completed": 7})
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # Allow read/write sharing, but deliberately withhold FILE_SHARE_DELETE.
    handle = kernel.CreateFileW(str(target), 0x80000000, 3, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(PermissionError):
            os.replace(target, tmp_path / "blocked.json")
        release = threading.Thread(target=lambda: (time.sleep(.15), kernel.CloseHandle(handle)))
        release.start()
        util.write_json(target, {"completed": 8})
        release.join(timeout=5)
        handle = None
        assert util.read_json(target) == {"completed": 8}
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
