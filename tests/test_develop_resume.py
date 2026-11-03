from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from landscape_culler import develop
from landscape_culler.util import read_json, write_json


def _photos(tmp_path, count=3):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    results = []
    for index in range(count):
        preview = tmp_path / f"{index}.jpg"
        Image.new("RGB", (120, 80), (index * 60, 90, 120)).save(preview)
        raw = tmp_path / f"{index}.ARW"
        raw.write_bytes(bytes([index]))
        results.append({"path": str(raw), "preview": str(preview), "rating": 4,
                        "group_id": index + 1})
    return {"run_id": "resume-test", "results": results}, run_dir


def _crop(*_args, **_kwargs):
    return ([{"id": "original", "bounds": {"left": 0., "top": 0., "right": 1., "bottom": 1.}}],
            "original", {"warnings": ["preserved fallback warning"]})


def _records(run_dir):
    return sorted((run_dir / "develop-checkpoints").glob("*/*.json"))


def _legacy_checkpoint(run_dir):
    records = _records(run_dir)
    context = read_json(records[0])["context"]
    items = {}
    for path in records:
        record = read_json(path)
        assert record["context"] == context
        items[record["source_key"]] = record["item"]
        assert path.resolve().is_relative_to(run_dir.resolve())
        path.unlink()
    path = run_dir / "develop-checkpoint.json"
    write_json(path, dict(schema_version=1, context=context, items=items))
    return path


def test_failed_batch_resumes_completed_photos_and_keeps_previous_plan(tmp_path, monkeypatch):
    payload, run_dir = _photos(tmp_path)
    old_plan = run_dir / "develop.json"
    old_plan.write_bytes(b'{"manual":"original plan"}')
    calls = []
    def crop(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("second photo interrupted")
        return _crop()
    monkeypatch.setattr(develop, "smart_crop_candidates", crop)
    with pytest.raises(RuntimeError):
        develop.create_develop_plan(payload, run_dir, 1)
    assert old_plan.read_bytes() == b'{"manual":"original plan"}'
    checkpoint_before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in _records(run_dir)}
    assert develop.develop_resume_summary(payload, run_dir) == {"completed": 1, "analyzed": 1, "total": 3}
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == saved for p, saved in checkpoint_before.items())
    calls.clear()
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: (calls.append(1), _crop())[1])
    # Metadata changes do not invalidate the expensive image analysis.
    payload["results"][0].update(rating=5, group_id=9)
    events = []
    plan = develop.create_develop_plan(payload, run_dir, 2, progress=events.append)
    assert len(calls) == 2 and len(plan["items"]) == 3
    assert plan["items"][0]["rating"] == 5 and plan["items"][0]["group_id"] == 9
    assert plan["items"][0]["smart_crop"]["warnings"] == ["preserved fallback warning"]
    assert events[0]["completed"] == 1 and events[0]["overall_percent"] == 33.3
    assert all(event["overall_percent"] >= 33.3 for event in events)
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == saved for p, saved in checkpoint_before.items())


def test_preview_failure_resumes_ai_result_without_running_models_again(tmp_path, monkeypatch):
    payload, run_dir = _photos(tmp_path, 1)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    render = develop._render_preview
    def fail(*_args):
        raise OSError("preview interrupted")
    monkeypatch.setattr(develop, "_render_preview", fail)
    with pytest.raises(OSError):
        develop.create_develop_plan(payload, run_dir, 1)
    assert len(_records(run_dir)) == 1
    monkeypatch.setattr(develop, "_render_preview", render)
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: pytest.fail("AI repeated"))
    plan = develop.create_develop_plan(payload, run_dir, 1)
    Path(plan["items"][0]["preview_path"]).unlink()
    # A missing rendered thumbnail requires rendering only, no new model call.
    plan = develop.create_develop_plan(payload, run_dir, 1)
    assert Path(plan["items"][0]["preview_path"]).is_file()


def test_interrupted_jpeg_write_is_not_published_and_partial_legacy_preview_is_repaired(tmp_path, monkeypatch):
    payload, run_dir = _photos(tmp_path, 1)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    save = Image.Image.save
    def interrupted(image, path, **_kwargs):
        Path(path).write_bytes(b"partial JPEG")
        raise OSError("JPEG write interrupted")
    monkeypatch.setattr(Image.Image, "save", interrupted)
    with pytest.raises(OSError):
        develop.create_develop_plan(payload, run_dir, 1)
    assert not list((run_dir / "develop-previews").iterdir())
    monkeypatch.setattr(Image.Image, "save", save)
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: pytest.fail("AI repeated"))
    plan = develop.create_develop_plan(payload, run_dir, 1)
    target = Path(plan["items"][0]["preview_path"])
    target.write_bytes(b"legacy partial JPEG")
    assert develop.develop_resume_summary(payload, run_dir)["completed"] == 0
    repaired = develop.create_develop_plan(payload, run_dir, 1)["items"][0]["preview_path"]
    with Image.open(repaired) as image:
        image.load()
        assert image.size == (120, 80)


def test_default_preview_skips_identity_color_operations_and_keeps_pixels(monkeypatch):
    image = Image.new("RGB", (60, 40), (51, 92, 133))
    def unnecessary(*_args, **_kwargs):
        pytest.fail("identity preview should not perform color transformations")
    monkeypatch.setattr(develop.ImageEnhance, "Brightness", unnecessary)
    monkeypatch.setattr(develop.ImageEnhance, "Contrast", unnecessary)
    monkeypatch.setattr(develop.ImageEnhance, "Color", unnecessary)
    monkeypatch.setattr(develop.np, "asarray", unnecessary)
    recipe = {"style_id": "lightroom", "style_strength": 60, "base": {},
              "crop_id": "original", "crop_candidates": _crop()[0]}
    result = develop._apply_preview_recipe(image, recipe)
    assert result.tobytes() == image.tobytes()


@pytest.mark.parametrize("changed", ["source", "preview", "mode", "cloud_model"])
def test_changed_analysis_inputs_invalidate_saved_results(tmp_path, monkeypatch, changed):
    monkeypatch.setenv("PHOTO_AI_SMART_CROP_MODE", "full")
    payload, run_dir = _photos(tmp_path, 2)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan(payload, run_dir, 1)
    if changed == "source":
        Path(payload["results"][0]["path"]).write_bytes(b"changed raw")
    elif changed == "preview":
        Image.new("RGB", (121, 81), "red").save(payload["results"][0]["preview"])
    elif changed == "mode":
        monkeypatch.setenv("PHOTO_AI_SMART_CROP_MODE", "heuristic")
    else:
        from landscape_culler import vision_provider
        monkeypatch.setattr(vision_provider, "load_config", lambda _: {
            "mode": "cloud", "model": "different", "base_url": "https://example.test"})
    calls = []
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: (calls.append(1), _crop())[1])
    plan = develop.create_develop_plan(payload, run_dir, 2)
    assert len(calls) == (1 if changed in {"source", "preview"} else 2)
    if changed == "preview":
        with Image.open(plan["items"][0]["preview_path"]) as rendered:
            red, green, blue = rendered.getpixel((0, 0))
            assert red > 200 and green < 30 and blue < 30


def test_excluded_photo_is_not_resurrected_and_reordering_keeps_correct_source(tmp_path, monkeypatch):
    payload, run_dir = _photos(tmp_path)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan(payload, run_dir, 1)
    revised = copy.deepcopy(payload)
    revised["results"].reverse()
    revised["results"][1]["excluded"] = True
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: pytest.fail("AI repeated"))
    plan = develop.create_develop_plan(revised, run_dir, 2)
    assert [item["index"] for item in plan["items"]] == [0, 2]
    assert [item["path"] for item in plan["items"]] == [revised["results"][i]["path"] for i in [0, 2]]


def test_managed_service_and_worker_resolve_same_profile_for_resume(tmp_path, monkeypatch):
    payload, run_dir = _photos(tmp_path, 1)
    monkeypatch.setenv("PHOTO_AI_CONTENT_ROOT", str(tmp_path / "content"))
    write_json(tmp_path / "content/state/model-resources/settings.json", {"active_profile": "8gb"})
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan(payload, run_dir, 1)
    checkpoint = read_json(_records(run_dir)[0])
    assert checkpoint["context"]["model"] == "qwen3-vl:4b-instruct-q4_K_M"
    assert develop.develop_resume_summary(payload, run_dir)["completed"] == 1


def test_real_process_exit_preserves_checkpoint_for_next_process(tmp_path):
    payload, run_dir = _photos(tmp_path)
    write_json(tmp_path / "payload.json", payload)
    script = tmp_path / "resume-child.py"
    script.write_text('''
import os, sys
from pathlib import Path
from landscape_culler import develop
from landscape_culler.util import read_json
root = Path(sys.argv[1])
calls = 0
def crop(*args, **kwargs):
    global calls
    calls += 1
    if sys.argv[2] == "kill" and calls == 2:
        os._exit(17)
    with (root / "calls.txt").open("a") as f:
        f.write("call\\n")
    return ([{"id":"original","bounds":{"left":0,"top":0,"right":1,"bottom":1}}], "original", {})
develop.smart_crop_candidates = crop
develop.create_develop_plan(read_json(root / "payload.json"), root / "run", 1)
''', encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PHOTO_AI_SMART_CROP_MODE="heuristic")
    first = subprocess.run([sys.executable, str(script), str(tmp_path), "kill"], env=env, capture_output=True)
    assert first.returncode == 17
    assert len(_records(run_dir)) == 1
    second = subprocess.run([sys.executable, str(script), str(tmp_path), "resume"], env=env, capture_output=True)
    assert second.returncode == 0, second.stderr.decode("utf-8", errors="replace")
    assert (tmp_path / "calls.txt").read_text().splitlines() == ["call"] * 3
    assert len(read_json(run_dir / "develop.json")["items"]) == 3
