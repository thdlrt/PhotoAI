import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from landscape_culler import develop, develop_checkpoint as storage
from landscape_culler.util import read_json, write_json
from test_develop_resume import _photos, _crop, _records, _legacy_checkpoint


@pytest.fixture
def cache(monkeypatch):
    instance = storage._CheckpointReadCache()
    monkeypatch.setattr(storage, "_read_cache", instance)
    return instance


@pytest.mark.parametrize("legacy", [False, True])
def test_repeated_summaries_parse_each_unchanged_record_once(tmp_path, monkeypatch, cache, legacy):
    payload, run = _photos(tmp_path, 3)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan(payload, run, 0)
    if legacy:
        _legacy_checkpoint(run)
    calls = []
    def read(path):
        calls.append(path)
        return read_json(path)
    monkeypatch.setattr(storage, "read_json", read)
    for _ in range(4):
        assert develop.develop_resume_summary(payload, run)["completed"] == 3
    successful = [p for p in calls if p.is_file()]
    assert len(successful) == (1 if legacy else 3)


def test_atomic_replacement_same_size_and_timestamp_is_detected(tmp_path, cache):
    path = tmp_path / "progress.json"
    write_json(path, {"value": "first"})
    stamp = path.stat()
    assert cache.read(path) == {"value": "first"}
    replacement = tmp_path / "replacement.json"
    write_json(replacement, {"value": "later"})
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_size == stamp.st_size
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns
    assert cache.read(path) == {"value": "later"}


@pytest.mark.parametrize("failure", [PermissionError, ValueError])
def test_failed_read_is_not_cached_without_stat_change(tmp_path, monkeypatch, cache, failure):
    path = tmp_path / "record.json"
    write_json(path, {"value": "ok"})
    calls = []
    def read(p):
        calls.append(1)
        if len(calls) == 1:
            raise failure("temporary failure")
        return read_json(p)
    monkeypatch.setattr(storage, "read_json", read)
    with pytest.raises(failure):
        cache.read(path)
    assert cache.read(path) == {"value": "ok"}
    assert cache.read(path) == {"value": "ok"}
    assert len(calls) == 2


def test_file_replaced_during_read_retries_current_snapshot(tmp_path, monkeypatch, cache):
    path = tmp_path / "record.json"
    write_json(path, {"value": "old"})
    calls = []
    def read(p):
        value = read_json(p)
        calls.append(1)
        if len(calls) == 1:
            write_json(p, {"value": "new"})
        return value
    monkeypatch.setattr(storage, "read_json", read)
    assert cache.read(path) == {"value": "new"}
    assert cache.read(path) == {"value": "new"}
    assert len(calls) == 2


def test_deleted_record_is_not_served_from_cache(tmp_path, cache):
    path = tmp_path / "record.json"
    write_json(path, {"old": True})
    cache.read(path)
    assert path.resolve().is_relative_to(tmp_path.resolve())
    path.unlink()
    with pytest.raises(FileNotFoundError):
        cache.read(path)
    write_json(path, {"new": True})
    assert cache.read(path) == {"new": True}


def test_cache_is_bounded_under_parallel_progress_reads(tmp_path):
    cache = storage._CheckpointReadCache(max_bytes=120, max_entries=3)
    paths = [tmp_path / f"{i}.json" for i in range(20)]
    for i, path in enumerate(paths):
        write_json(path, {"value": i})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(cache.read, paths * 4))
    assert results == [{"value": i} for i in range(20)] * 4
    assert cache.bytes <= 120 and len(cache.entries) <= 3
    assert cache.bytes == sum(stamp[0] for stamp, value in cache.entries.values())
    large = tmp_path / "large.json"
    write_json(large, {"large": "x" * 200})
    assert cache.read(large) == {"large": "x" * 200}
    assert large not in cache.entries


def test_modifying_restored_plan_does_not_modify_cached_analysis(tmp_path, monkeypatch, cache):
    payload, run = _photos(tmp_path, 1)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan(payload, run, 0)
    before = {p: p.read_bytes() for p in _records(run)}
    restored = develop.create_develop_plan(payload, run, 0)
    restored["items"][0]["smart_crop"]["warnings"].append("manual edit")
    restored["items"][0]["crop_candidates"][0]["bounds"]["left"] = 0.4
    again = develop.create_develop_plan(payload, run, 0)
    assert again["items"][0]["smart_crop"]["warnings"] == ["preserved fallback warning"]
    assert again["items"][0]["crop_candidates"][0]["bounds"]["left"] == 0
    assert all(p.read_bytes() == value for p, value in before.items())
