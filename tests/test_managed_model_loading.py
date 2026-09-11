from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from landscape_culler.model_resources import (
    MODEL_SPECS,
    clip_model_reference,
    local_hf_model_path,
    managed_hf_model_path,
)


def _managed_models(tmp_path: Path, resource_ids: list[str]) -> Path:
    content_root = tmp_path / "PhotoAI"
    models_root = content_root / "models"
    models_root.mkdir(parents=True)
    (content_root / "marker.json").write_text(
        json.dumps({"application": "PhotoAI"}), encoding="utf-8"
    )
    for resource_id in resource_ids:
        spec = MODEL_SPECS[resource_id]
        snapshot = (
            models_root
            / "huggingface"
            / "hub"
            / ("models--" + spec["repo_id"].replace("/", "--"))
            / "snapshots"
            / spec["revision"]
        )
        snapshot.mkdir(parents=True)
        for filename in spec["files"]:
            target = snapshot / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "{}" if filename == "config.json" else "fixture", encoding="utf-8"
            )
        assert not (snapshot.parent.parent / "refs" / "main").exists()
    return models_root


def _development_models(tmp_path: Path, resource_ids: list[str]) -> Path:
    checkout = tmp_path / "checkout"
    runtime_root = checkout / ".runtime"
    (checkout / "src" / "landscape_culler").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        "[project]\nname='photoai-test'\n", encoding="utf-8"
    )
    (checkout / "src" / "landscape_culler" / "model_resources.py").write_text(
        "# source checkout marker\n", encoding="utf-8"
    )
    for resource_id in resource_ids:
        spec = MODEL_SPECS[resource_id]
        snapshot = (
            runtime_root
            / "huggingface"
            / "hub"
            / ("models--" + spec["repo_id"].replace("/", "--"))
            / "snapshots"
            / spec["revision"]
        )
        snapshot.mkdir(parents=True)
        for filename in spec["files"]:
            target = snapshot / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "{}" if filename == "config.json" else "fixture", encoding="utf-8"
            )
    return runtime_root


class _FakeModel:
    def eval(self):
        return self

    def to(self, _device):
        return self


class _ProcessorLoader:
    calls: ClassVar[list[tuple[Path, dict[str, object]]]] = []

    @classmethod
    def from_pretrained(cls, path, **kwargs):
        cls.calls.append((Path(path), kwargs))
        return object()


class _ModelLoader:
    calls: ClassVar[list[tuple[Path, dict[str, object]]]] = []

    @classmethod
    def from_pretrained(cls, path, **kwargs):
        cls.calls.append((Path(path), kwargs))
        return _FakeModel()


def test_managed_snapshot_resolution_never_uses_refs_main(
    tmp_path: Path, monkeypatch
) -> None:
    models_root = _managed_models(tmp_path, ["dinov2-base"])
    monkeypatch.setenv("PHOTO_AI_MODELS_DIR", str(models_root))

    resolved = managed_hf_model_path("dinov2-base")

    assert resolved.name == MODEL_SPECS["dinov2-base"]["revision"]
    assert not (resolved.parent.parent / "refs" / "main").exists()


def test_managed_snapshot_missing_configuration_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("PHOTO_AI_MODELS_DIR", raising=False)
    monkeypatch.delenv("PHOTO_AI_LEGACY_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "unrelated-cache"))

    with pytest.raises(RuntimeError, match="受管模型目录尚未配置"):
        managed_hf_model_path("dinov2-base")


def test_source_checkout_can_resolve_explicit_legacy_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = _development_models(tmp_path, ["dinov2-base"])
    monkeypatch.delenv("PHOTO_AI_MODELS_DIR", raising=False)
    monkeypatch.delenv("PHOTO_AI_CONTENT_ROOT", raising=False)
    monkeypatch.setenv("PHOTO_AI_LEGACY_RUNTIME_ROOT", str(runtime_root))

    resolved = managed_hf_model_path("dinov2-base")

    assert resolved == local_hf_model_path(runtime_root, "dinov2-base")


def test_content_root_worker_cannot_use_legacy_development_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = _development_models(tmp_path, ["dinov2-base"])
    monkeypatch.delenv("PHOTO_AI_MODELS_DIR", raising=False)
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(tmp_path / "content"))
    monkeypatch.setenv("PHOTO_AI_LEGACY_RUNTIME_ROOT", str(runtime_root))

    with pytest.raises(RuntimeError, match="受管模型目录尚未配置"):
        managed_hf_model_path("dinov2-base")


def test_managed_clip_reference_never_falls_back_to_repo_id(tmp_path: Path) -> None:
    models_root = _managed_models(tmp_path, ["clip-vit-b32"])

    assert clip_model_reference(models_root) == str(
        managed_hf_model_path("clip-vit-b32", models_root=models_root)
    )

    missing_root = _managed_models(tmp_path / "missing", [])
    with pytest.raises(RuntimeError, match="固定本地快照缺失"):
        clip_model_reference(missing_root)


def test_dino_runtime_loads_exact_local_snapshot(tmp_path: Path, monkeypatch) -> None:
    from landscape_culler.features import FeatureExtractor

    models_root = _managed_models(tmp_path, ["dinov2-base"])
    monkeypatch.setenv("PHOTO_AI_MODELS_DIR", str(models_root))
    _ProcessorLoader.calls = []
    _ModelLoader.calls = []
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    fake_transformers = SimpleNamespace(
        AutoImageProcessor=_ProcessorLoader,
        AutoModel=_ModelLoader,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    FeatureExtractor(tmp_path / "cache")._ensure_model()

    expected = managed_hf_model_path("dinov2-base")
    assert _ProcessorLoader.calls == [(expected, {"local_files_only": True})]
    assert _ModelLoader.calls == [(expected, {"local_files_only": True})]


def test_smart_crop_loaders_use_only_exact_local_snapshots(
    tmp_path: Path, monkeypatch
) -> None:
    from landscape_culler import smart_crop

    resource_ids = ["grounding-dino-tiny", "segformer-b2", "sam2-hiera-tiny"]
    models_root = _managed_models(tmp_path, resource_ids)
    monkeypatch.setenv("PHOTO_AI_MODELS_DIR", str(models_root))
    _ProcessorLoader.calls = []
    _ModelLoader.calls = []
    fake_transformers = SimpleNamespace(
        AutoModelForZeroShotObjectDetection=_ModelLoader,
        AutoProcessor=_ProcessorLoader,
        SegformerForSemanticSegmentation=_ModelLoader,
        SegformerImageProcessor=_ProcessorLoader,
        Sam2Model=_ModelLoader,
        Sam2Processor=_ProcessorLoader,
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setattr(smart_crop, "_VISION_MODELS", smart_crop._VisionModels())
    monkeypatch.setattr(smart_crop, "_model_device", lambda: "cpu")

    smart_crop._load_object_model()
    smart_crop._load_segment_model()
    smart_crop._load_mask_model()

    expected = [managed_hf_model_path(resource_id) for resource_id in resource_ids]
    assert [path for path, _kwargs in _ProcessorLoader.calls] == expected
    assert [path for path, _kwargs in _ModelLoader.calls] == expected
    assert all(
        kwargs == {"local_files_only": True} for _, kwargs in _ProcessorLoader.calls
    )
    assert all(kwargs == {"local_files_only": True} for _, kwargs in _ModelLoader.calls)


def test_qrealign_receives_exact_local_model_path(tmp_path: Path, monkeypatch) -> None:
    from landscape_culler.general_aesthetic import GeneralAestheticScorer

    models_root = _managed_models(tmp_path, ["qrealign-mini"])
    monkeypatch.setenv("PHOTO_AI_MODELS_DIR", str(models_root))
    calls: list[tuple[str, dict[str, object]]] = []
    metric = object()
    fake_pyiqa = SimpleNamespace(
        create_metric=lambda name, **kwargs: (calls.append((name, kwargs)), metric)[1]
    )
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "pyiqa", fake_pyiqa)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    scorer = GeneralAestheticScorer(tmp_path / "state", device="cuda")
    scorer._ensure_metric()

    assert calls == [
        (
            "qrealign",
            {
                "device": "cuda",
                "model": str(managed_hf_model_path("qrealign-mini")),
            },
        )
    ]
    assert scorer._metric is metric
