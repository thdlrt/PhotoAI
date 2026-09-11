from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import landscape_culler.smart_crop as smart


def _image() -> Image.Image:
    image = Image.new("RGB", (800, 500), "#7594ad")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 275, 800, 500), fill="#526247")
    draw.ellipse((575, 145, 705, 300), fill="#d29a55")
    return image


def _scene() -> dict:
    subject = {"left": 0.70, "top": 0.26, "right": 0.89, "bottom": 0.73}
    return {
        "scene_type": "wildlife",
        "composition": "thirds",
        "horizon_y": 0.55,
        "horizon_confidence": 0.9,
        "main_subject_boxes": [subject],
        "gaze_direction": "left",
        "negative_space_direction": "left",
        "sky_importance": 0.35,
        "ground_importance": 0.65,
        "water_importance": 0.0,
        "preferred_aspect": "3:2",
        "summary": "动物位于画面右侧并朝左",
        "subjects": [{"label": "animal", "score": 0.92, "box": subject, "strict": True}],
        "segmentation": {},
        "warnings": [],
        "engines": {},
    }


def test_dense_candidates_keep_strict_subject_and_horizon() -> None:
    scene = _scene()
    candidates = smart.generate_dense_candidates(_image(), scene)
    valid = [item for item in candidates[1:] if item["valid"]]

    assert len(candidates) > 40
    assert valid
    for item in valid:
        assert smart._coverage(scene["subjects"][0]["box"], item["bounds"]) >= 0.96
        assert item["bounds"]["top"] <= scene["horizon_y"] <= item["bounds"]["bottom"]


def test_subject_mask_coverage_is_used_instead_of_detection_box() -> None:
    scene = _scene()
    mask = np.zeros((100, 160), dtype=bool)
    mask[30:72, 130:145] = True
    integral, shape, area = smart._mask_integral(mask, 160, 100)
    subject = scene["subjects"][0]
    subject["_mask_integral"] = integral
    subject["_mask_shape"] = shape
    subject["_mask_grid_area"] = area

    crop = {"left": 0.0, "top": 0.0, "right": 0.86, "bottom": 1.0}
    box_coverage = smart._coverage(subject["box"], crop)
    _score, strict_coverage = smart._subject_score(crop, scene)

    assert box_coverage > 0.8
    assert strict_coverage < 0.8


def test_full_pipeline_uses_vlm_rank_and_preserves_reasons(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(smart, "analyze_scene", lambda *_args, **_kwargs: _scene())

    def fake_rank(image, dense, scene, data_dir, *, mode):
        del image, scene, data_dir, mode
        ranked = sorted(
            (item for item in dense if item.get("valid") and item["internal_id"] != "C000"),
            key=lambda item: item["rule_score"],
            reverse=True,
        )
        original = dense[0]
        original["score"] = 0.55
        for index, item in enumerate(ranked):
            item["score"] = 0.92 - index * 0.001
            item["vlm_score"] = item["score"]
            item["reasons"] = ["主体完整且视线空间自然"]
        return [*ranked, original], 0.88, "语义构图明显优于原图"

    monkeypatch.setattr(smart, "_rank_candidates", fake_rank)
    candidates, selected, analysis = smart.smart_crop_candidates(_image(), tmp_path / "run", mode="full")

    assert {item["id"] for item in candidates} == {"original", "balanced", "tight", "wide"}
    assert selected == "balanced"
    assert candidates[1]["label"] == "AI 推荐"
    assert "主体完整" in candidates[1]["reasons"][0]
    assert analysis["rank_confidence"] == 0.88
    assert analysis["dense_candidate_count"] > 40


def test_low_confidence_falls_back_to_original_and_cache_is_reused(tmp_path: Path, monkeypatch) -> None:
    calls = {"scene": 0}

    def fake_scene(*_args, **_kwargs):
        calls["scene"] += 1
        return _scene()

    monkeypatch.setattr(smart, "analyze_scene", fake_scene)

    def fake_rank(image, dense, scene, data_dir, *, mode):
        del image, scene, data_dir, mode
        ranked = sorted((item for item in dense if item.get("valid")), key=lambda item: item["rule_score"], reverse=True)
        for item in ranked:
            item["score"] = 0.9
        return ranked, 0.35, "模型意见不稳定"

    monkeypatch.setattr(smart, "_rank_candidates", fake_rank)
    first = smart.smart_crop_candidates(_image(), tmp_path / "run", mode="full")
    second = smart.smart_crop_candidates(_image(), tmp_path / "run", mode="full")

    assert first[1] == "original"
    assert second == first
    assert calls["scene"] == 1


def test_vlm_request_does_not_override_managed_ollama_endpoint(
    tmp_path: Path, monkeypatch
) -> None:
    constructed: list[dict] = []

    class FakeCritic:
        def __init__(self, *_args, **kwargs) -> None:
            constructed.append(kwargs)

        def ensure_ready(self) -> None:
            pass

        def _request(self, *_args, **_kwargs) -> dict:
            return {"message": {"content": "{}"}}

    monkeypatch.setenv("PHOTO_AI_OLLAMA_ENDPOINT", "http://127.0.0.1:49178")
    monkeypatch.setattr(smart, "OllamaGroupCritic", FakeCritic)

    assert smart._vlm_json([_image()], "test", {}, tmp_path) == {}
    assert len(constructed) == 1
    assert "endpoint" not in constructed[0]
