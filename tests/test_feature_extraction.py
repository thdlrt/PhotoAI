from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from landscape_culler import features
from landscape_culler.features import DINO_DIMENSIONS, FeatureExtractor
from landscape_culler.preview import load_preview, preview_cache_path


class SimulatedExtractor(FeatureExtractor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.fail_call = None

    def _embed_batch(self, images):
        self.calls.append(len(images))
        if len(self.calls) == self.fail_call:
            raise RuntimeError("simulated inference interruption")
        output = np.zeros((len(images), DINO_DIMENSIONS), dtype=np.float32)
        output[:, 0] = 1
        return output


def photos(tmp_path, count):
    paths = [tmp_path / f"frame-{index}.png" for index in range(count)]
    for index, path in enumerate(paths):
        Image.new("RGB", (80, 60), (60 + index % 100, 80, 120)).save(path)
    return paths


def track_images(monkeypatch):
    counts = {"open": 0, "peak": 0}
    def load(*args, **kwargs):
        image = load_preview(*args, **kwargs)
        counts["open"] += 1
        counts["peak"] = max(counts["peak"], counts["open"])
        close = image.close
        closed = False
        def tracked_close():
            nonlocal closed
            if not closed:
                counts["open"] -= 1
                closed = True
            close()
        image.close = tracked_close
        return image
    monkeypatch.setattr(features, "load_preview", load)
    return counts


def test_streaming_bounds_decoded_images_and_warm_cache_avoids_inference(tmp_path, monkeypatch):
    paths = photos(tmp_path, 37)
    originals = [path.read_bytes() for path in paths]
    counts = track_images(monkeypatch)
    extractor = SimulatedExtractor(tmp_path / "cache")
    matrix, ordered = extractor.extract(paths, batch_size=999)
    assert matrix.shape == (37, 776) and ordered == paths
    assert extractor.calls == [16, 16, 5]
    assert counts == {"open": 0, "peak": 16}
    second, ordered = extractor.extract(paths)
    np.testing.assert_array_equal(second, matrix)
    assert extractor.calls == [16, 16, 5]
    assert extractor.last_stats["cache_hits"] == 37
    assert [path.read_bytes() for path in paths] == originals


@pytest.mark.parametrize("corruption", ["truncated", "wrong-shape", "nan", "zero-embedding", "oversized", "object-array"])
def test_bad_feature_cache_recomputes_only_affected_photo(tmp_path, corruption):
    paths = photos(tmp_path, 3)
    extractor = SimulatedExtractor(tmp_path / "cache")
    original, _ = extractor.extract(paths)
    cached = extractor._cached_path(paths[1])
    if corruption == "truncated": cached.write_bytes(b"\x93NUMPY")
    elif corruption == "wrong-shape": np.save(cached, np.zeros(8, dtype=np.float32))
    elif corruption == "nan": np.save(cached, np.full(776, np.nan, dtype=np.float32))
    elif corruption == "zero-embedding": np.save(cached, np.zeros(776, dtype=np.float32))
    elif corruption == "oversized": cached.write_bytes(b"x" * 20000)
    else: np.save(cached, np.array([{"unexpected":True}], dtype=object))
    extractor.calls.clear()
    repaired, ordered = extractor.extract(paths)
    assert ordered == paths and repaired.shape == original.shape
    assert extractor.calls == [1] and extractor.last_stats["cache_repairs"] == 1
    np.testing.assert_array_equal(repaired[[0, 2]], original[[0, 2]])
    assert extractor._read_cached(cached) is not None


@pytest.mark.parametrize("missing", [False, True])
def test_preview_cache_repair_keeps_valid_feature_vectors(tmp_path, missing):
    paths = photos(tmp_path, 2)
    extractor = SimulatedExtractor(tmp_path / "cache")
    original, _ = extractor.extract(paths)
    preview = preview_cache_path(paths[0], extractor.preview_dir)
    if missing: preview.unlink()
    else: preview.write_bytes(b"broken jpeg")
    extractor.calls.clear()
    repaired, _ = extractor.extract(paths)
    assert extractor.calls == []
    np.testing.assert_array_equal(repaired, original)
    with Image.open(preview) as image:
        image.load()
        assert image.size == (80, 60)


def test_failure_closes_batch_and_retry_reuses_completed_features(tmp_path, monkeypatch):
    paths = photos(tmp_path, 21)
    counts = track_images(monkeypatch)
    extractor = SimulatedExtractor(tmp_path / "cache")
    extractor.fail_call = 2
    with pytest.raises(RuntimeError, match="interruption"):
        extractor.extract(paths)
    assert counts["open"] == 0
    assert sum(extractor._cached_path(path).is_file() for path in paths) == 16
    extractor.fail_call = None
    extractor.calls.clear()
    result, ordered = extractor.extract(paths)
    assert ordered == paths and result.shape == (21, 776)
    assert extractor.calls == [5]
    assert extractor.last_stats["cache_hits"] == 16
    assert counts["open"] == 0


def test_gpu_oom_reduces_batch_once_and_keeps_photo_order(tmp_path, monkeypatch):
    extractor = FeatureExtractor(tmp_path / "cache")
    monkeypatch.setattr(extractor, "_ensure_model", lambda: None)
    calls = []
    def infer(images):
        calls.append(len(images))
        if len(images) > 2:
            raise RuntimeError("CUDA out of memory")
        return np.array([[image] * 768 for image in images], dtype=np.float32)
    monkeypatch.setattr(extractor, "_infer_embeddings", infer)
    first = extractor._embed_batch(list(range(8)))
    second = extractor._embed_batch(list(range(8, 16)))
    assert [count for count in calls if count > 2] == [8, 4]
    assert first[:, 0].tolist() == list(range(8))
    assert second[:, 0].tolist() == list(range(8, 16))


def test_unreadable_source_is_reported_without_leaking_loaded_images(tmp_path, monkeypatch):
    paths = photos(tmp_path, 3)
    paths[1].write_bytes(b"bad source")
    counts = track_images(monkeypatch)
    extractor = SimulatedExtractor(tmp_path / "cache")
    result, ordered = extractor.extract(paths)
    assert ordered == [paths[0], paths[2]] and result.shape == (2, 776)
    assert len(extractor.failures) == 1 and extractor.failures[0][0] == paths[1]
    assert counts["open"] == 0


@pytest.mark.parametrize("batch_size", [0, -1, 1.5, True])
def test_invalid_batch_size_has_no_cache_side_effect(tmp_path, batch_size):
    extractor = FeatureExtractor(tmp_path / "cache")
    with pytest.raises(ValueError):
        extractor.extract([], batch_size=batch_size)
    assert not (tmp_path / "cache").exists()
