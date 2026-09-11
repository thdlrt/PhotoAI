from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
from PIL import Image

from landscape_culler.style_ai import (
    AIStageUnavailable,
    DinoMedoidSelector,
    LocalClipStyleRetriever,
    QwenStyleSceneAnalyzer,
    _clip_feature_tensor,
    run_style_ai_cascade,
)


class _FakeExtractor:
    def __init__(self, cache_dir: Path, **_kwargs: Any) -> None:
        self.cache_dir = cache_dir
        self.released = False

    def _cached_path(self, path: Path) -> Path:
        cached = self.cache_dir / f"{path.stem}.npy"
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.touch()
        return cached

    def extract(self, paths: list[Path]) -> tuple[np.ndarray, list[Path]]:
        technical = np.zeros((3, 8), dtype=np.float32)
        technical[:, 1] = [0.1, 0.5, 0.9]
        # B is the cosine medoid of A/B/C, not merely the stable first path.
        embedding = np.asarray(
            [[1.0, 0.0], [0.8, 0.6], [-1.0, 0.0]], dtype=np.float32
        )
        return np.concatenate([technical, embedding], axis=1), paths

    def release(self) -> None:
        self.released = True


def test_clip_retriever_uses_portable_content_cache(
    tmp_path: Path, monkeypatch
) -> None:
    content_root = (tmp_path / "portable").resolve()
    state = content_root / "state"
    styles = content_root / "styles"
    cache = content_root / "cache"
    for directory in (state, styles, cache):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(content_root))
    monkeypatch.setenv("PHOTO_AI_STYLES_DIR", str(styles))
    monkeypatch.setenv("PHOTO_AI_CACHE_DIR", str(cache))

    retriever = LocalClipStyleRetriever(state)
    assert retriever.storage_root == content_root
    assert retriever.cache_dir == cache / "ai" / "style-clip-v1"


def test_dino_selector_computes_real_cosine_medoid(tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("a.arw", "b.arw", "c.arw")]
    for path in paths:
        path.write_bytes(path.name.encode())
    selector = DinoMedoidSelector(tmp_path / "data", extractor_factory=_FakeExtractor)

    result = selector.select([{"path": str(path)} for path in paths])

    assert Path(result["probes"]["representative"]) == paths[1]
    assert Path(result["probes"]["darkest"]) == paths[0]
    assert Path(result["probes"]["brightest"]) == paths[2]
    assert result["probes"]["basis"] == "dinov2_medoid"
    assert result["stage"]["used"] is True
    assert result["stage"]["embedding_dimensions"] == 2


class _FakeCritic:
    requests = 0

    def __init__(self, _data_dir: Path, **_kwargs: Any) -> None:
        self.ready = False

    def ensure_ready(self) -> None:
        self.ready = True

    def _image(self, path: Path) -> str:
        return f"image:{path.name}"

    def _request(self, path: str, _payload: dict, timeout: float) -> dict:
        assert path == "/api/chat"
        assert timeout == 900.0
        type(self).requests += 1
        return {
            "message": {
                "content": {
                    "scene": "wetland landscape",
                    "weather": "overcast",
                    "time_of_day": "daytime",
                    "subjects": ["lotus", "pavilion"],
                    "dominant_colors": ["green", "gray"],
                    "mood": ["quiet", "natural"],
                    "avoid_effects": ["crushed shadows", "oversaturation"],
                    "search_terms": ["soft green", "natural film", "overcast"],
                    "summary": "阴天湿地，绿色为主，应保留自然层次。",
                    "confidence": 0.91,
                }
            }
        }

    def unload(self) -> None:
        pass


def test_qwen_scene_analysis_is_structured_and_cached_on_data_drive(
    tmp_path: Path,
) -> None:
    _FakeCritic.requests = 0
    raw = tmp_path / "photo.arw"
    raw.write_bytes(b"raw")
    data_dir = tmp_path / "data"
    first = QwenStyleSceneAnalyzer(data_dir, critic_factory=_FakeCritic)

    result = first.analyze([raw])
    cached = QwenStyleSceneAnalyzer(data_dir, critic_factory=_FakeCritic).analyze([raw])

    assert result["cached"] is False
    assert cached["cached"] is True
    assert cached["scene"]["search_terms"] == [
        "soft green",
        "natural film",
        "overcast",
    ]
    assert _FakeCritic.requests == 1
    cache_files = list((data_dir / "cache" / "ai" / "style-scene-v1").rglob("*.json"))
    assert len(cache_files) == 1


class _FakeDino:
    model = "fake-dinov2"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def select(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append("dino")
        return {
            "probes": {
                "representative": items[1]["path"],
                "brightest": items[1]["path"],
                "darkest": items[0]["path"],
                "basis": "dinov2_medoid",
            },
            "stage": {
                "status": "complete",
                "model": self.model,
                "used": True,
                "cached": True,
            },
        }

    def release(self) -> None:
        self.calls.append("release_dino")


class _FakeQwen:
    model = "fake-qwen3-vl"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def analyze(self, paths: list[Path]) -> dict[str, Any]:
        self.calls.append("qwen")
        assert paths[0].name == "b.arw"
        return {
            "scene": {
                "scene": "mountain lake",
                "weather": "clear",
                "time_of_day": "sunset",
                "subjects": ["mountain", "lake"],
                "dominant_colors": ["blue", "orange"],
                "mood": ["calm"],
                "avoid_effects": ["oversaturation"],
                "search_terms": ["golden hour", "natural landscape"],
                "summary": "日落湖景。",
                "confidence": 0.9,
            },
            "cached": False,
        }

    def release(self) -> None:
        self.calls.append("release_qwen")


class _FakeClip:
    model = "fake-clip"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def score_candidates(
        self,
        representative: Path,
        scene: dict[str, Any],
        entries: list[dict[str, Any]],
        *,
        limit: int,
    ) -> dict[str, float]:
        self.calls.append("clip")
        assert representative.name == "b.arw"
        assert scene["weather"] == "clear"
        assert limit == 12
        return {entry["preset_id"]: 0.9 - index * 0.01 for index, entry in enumerate(entries)}

    def release(self) -> None:
        self.calls.append("release_clip")


def test_production_cascade_calls_dino_then_qwen_then_clip(tmp_path: Path) -> None:
    calls: list[str] = []
    raws = [tmp_path / "a.arw", tmp_path / "b.arw"]
    for raw in raws:
        raw.write_bytes(b"raw")
    entries = [
        {"preset_id": f"look-{index}", "file_hash": f"hash-{index}"}
        for index in range(10)
    ]

    result = run_style_ai_cascade(
        {1: [{"path": str(path)} for path in raws]},
        entries,
        tmp_path / "data",
        dino_selector=_FakeDino(calls),
        scene_analyzer=_FakeQwen(calls),
        clip_retriever=_FakeClip(calls),
    )

    assert calls == [
        "dino",
        "release_dino",
        "qwen",
        "release_qwen",
        "clip",
        "release_clip",
    ]
    group = result["groups"]["1"]
    assert group["probes"]["basis"] == "dinov2_medoid"
    assert len(group["clip_scores"]) == 10
    assert all(group["stages"][name]["used"] for name in ("dinov2", "qwen3_vl", "clip"))
    assert all(result["stages"][name]["status"] == "complete" for name in ("dinov2", "qwen3_vl", "clip"))


class _UnavailableDino(_FakeDino):
    def select(self, _items: list[dict[str, Any]]) -> dict[str, Any]:
        raise AIStageUnavailable("DINO missing")


class _UnavailableQwen(_FakeQwen):
    def analyze(self, _paths: list[Path]) -> dict[str, Any]:
        raise AIStageUnavailable("Qwen missing")


class _UnavailableClip(_FakeClip):
    def score_candidates(self, *_args: Any, **_kwargs: Any) -> dict[str, float]:
        raise AIStageUnavailable("CLIP missing")


def test_cascade_never_hides_model_degradation(tmp_path: Path) -> None:
    calls: list[str] = []
    raw = tmp_path / "a.arw"
    raw.write_bytes(b"raw")

    result = run_style_ai_cascade(
        {"1": [{"path": str(raw)}]},
        [{"preset_id": "look", "file_hash": "hash"}],
        tmp_path / "data",
        seed_scenes={"1": {"summary": "existing scene"}},
        dino_selector=_UnavailableDino(calls),
        scene_analyzer=_UnavailableQwen(calls),
        clip_retriever=_UnavailableClip(calls),
    )

    stages = result["groups"]["1"]["stages"]
    assert stages["dinov2"]["status"] == "unavailable"
    assert stages["qwen3_vl"]["status"] == "degraded"
    assert stages["qwen3_vl"]["fallback"] == "existing_scene_metadata"
    assert stages["clip"]["status"] == "unavailable"
    assert stages["clip"]["fallback"] == "scene_metadata_recall"
    assert not any(stage["used"] for stage in stages.values())


class _DeterministicLocalClip(LocalClipStyleRetriever):
    def _ensure_model(self) -> None:
        self._model = object()

    def _image_features(self, _images) -> np.ndarray:
        return np.asarray([[1.0, 0.0]], dtype=np.float32)

    def _text_features(self, _texts) -> np.ndarray:
        return np.asarray([[1.0, 0.0]], dtype=np.float32)

    def _entry_features(self, entries):
        assert len(entries) == 2
        return (
            np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
            ["natural landscape", "neon portrait"],
        )


def test_local_clip_accepts_the_real_pil_preview_return_type(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    Image.new("RGB", (32, 24), (60, 100, 80)).save(source)
    retriever = _DeterministicLocalClip(tmp_path / "data")

    scores = retriever.score_candidates(
        source,
        {"search_terms": ["natural landscape"]},
        [
            {"preset_id": "natural", "file_hash": "a"},
            {"preset_id": "neon", "file_hash": "b"},
        ],
    )

    assert list(scores) == ["natural", "neon"]
    assert scores["natural"] > scores["neon"]


def test_clip_feature_api_accepts_transformers_5_pooled_output() -> None:
    pooled = np.asarray([[1.0, 2.0]], dtype=np.float32)

    assert _clip_feature_tensor(SimpleNamespace(pooler_output=pooled)) is pooled
    assert _clip_feature_tensor(pooled) is pooled
