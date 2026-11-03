import pytest

from landscape_culler.lightroom_apply import LightroomApplyError
from landscape_culler.style_worker import StyleWorkerError, run_style_worker
from landscape_culler.util import read_json, write_json
from landscape_culler.web import _job_progress_plan
from test_style_worker import _run, _FakeLightroom, _FakeLutEngine, _online, _add_second_group


def fixture(tmp_path):
    data, run, raw = _run(tmp_path / "project" / ".runtime")
    return data, run, raw, _FakeLightroom(), _FakeLutEngine(tmp_path / "project")


def preview(data, run, lr, engine, *, revision=3, batch="lut-refresh", **kwargs):
    return run_style_worker(
        run, data, batch, revision, group_id=1, lut_id="cube-test", lut_hash="lut-hash",
        amount=65, preview_only=True, bridge_status_reader=_online,
        batch_creator=lr.create, batch_waiter=lr.wait, lut_engine_factory=lambda _: engine,
        scorer_factory=lambda *_: pytest.fail("manual LUT must not score"),
        cascade_runner=lambda **_: pytest.fail("manual LUT must not recommend"), **kwargs,
    )


@pytest.mark.parametrize("skip_base", [False, True])
def test_missing_base_builds_one_neutral_without_ai_and_preserves_manual_groups(tmp_path, skip_base):
    data, run, raw, lr, engine = fixture(tmp_path)
    original = raw.read_bytes()
    _add_second_group(run, raw.parent)
    plan = read_json(run / "develop.json")
    plan["creative_style"].update(scope="global", global_selection={"status": "skipped", "manual_override": True})
    other = {"status": "confirmed", "preset_id": "keep", "amount": 77, "manual_override": True}
    plan["creative_style"]["groups"] = {"2": other}
    if skip_base:
        plan["basic_color"]["status"] = "skipped"
    write_json(run / "develop.json", plan)
    result = preview(data, run, lr, engine)
    assert len(lr.tasks) == 1
    task = lr.tasks[0]
    assert task.photo_path == raw.resolve() and task.preset_uuid is None
    assert task.auto_tone is not skip_base and task.auto_white_balance is not skip_base
    selected = result["develop"]["creative_style"]
    assert selected["scope"] == "group" and selected["groups"]["2"] == other
    assert selected["global_selection"] == plan["creative_style"]["global_selection"]
    assert selected["groups"]["1"]["strength"] == 65
    assert selected["groups"]["1"]["lut_hash"] == "lut-hash"
    assert selected["groups"]["1"]["recommendation_status"] == "complete"
    assert raw.read_bytes() == original


def test_changed_base_refreshes_only_target_and_discards_old_ai_evidence(tmp_path):
    data, run, raw, lr, engine = fixture(tmp_path)
    preview(data, run, lr, engine)
    plan = read_json(run / "develop.json")
    plan["items"][0]["base"]["Exposure2012"] = 1.2
    selected = plan["creative_style"]["groups"]["1"]
    selected["recommended_preset_id"] = "obsolete-ai"
    selected["top3"].append({"preset_id": "old", "render_status": "ready", "preview_key": "a" * 64})
    write_json(run / "develop.json", plan)
    rec = read_json(run / "style-recommendations.json")
    old_key = rec["groups"][0]["neutral_preview_key"]
    other = {"group_id": 8, "probes": {}, "top3": [{"preview_key": "b" * 64}]}
    rec["groups"].append(other)
    rec["runtime_identity"] = {"catalog_version": "preserve-shared"}
    write_json(run / "style-recommendations.json", rec)
    result = preview(data, run, lr, engine, revision=4, batch="lut-changed-base")
    target = result["develop"]["creative_style"]["groups"]["1"]
    assert len(lr.tasks) == 1 and lr.tasks[0].style["Exposure2012"] == 1.2
    assert target["neutral_preview_key"] != old_key
    assert "recommended_preset_id" not in target
    assert target["top3"][1]["render_status"] == "stale"
    assert "preview_key" not in target["top3"][1]
    assert result["recommendation"]["groups"][0] == other
    assert result["recommendation"]["runtime_identity"] == {"catalog_version": "preserve-shared"}


@pytest.mark.parametrize("fault", ["source", "plan", "wrong_photo", "isolation"])
def test_rebuilt_base_rejects_changed_or_unverified_results(tmp_path, fault):
    data, run, raw, lr, engine = fixture(tmp_path)
    wait = lr.wait
    def bad_wait(*args, **kwargs):
        status = wait(*args, **kwargs)
        if fault == "source":
            raw.write_bytes(b"changed-test-source")
        elif fault == "plan":
            plan = read_json(run / "develop.json")
            plan["revision"] = 9
            write_json(run / "develop.json", plan)
        elif fault == "wrong_photo":
            status["tasks"][0]["photo_path"] = str(raw.with_name("another.ARW"))
        else:
            status["tasks"][0]["result"]["isolation_status"] = "not-removed"
        return status
    lr.wait = bad_wait
    with pytest.raises(StyleWorkerError):
        preview(data, run, lr, engine)
    assert not engine.render_calls
    assert not list((run / "style-previews").glob("*.jpg"))
    if fault == "plan":
        assert read_json(run / "develop.json")["revision"] == 9


def test_cancelled_neutral_is_reused_on_retry_without_lightroom_or_ai(tmp_path):
    data, run, raw, lr, engine = fixture(tmp_path)
    wait = lr.wait
    def cancel(*args, **kwargs):
        raise LightroomApplyError("cancelled")
    lr.wait = cancel
    with pytest.raises(StyleWorkerError, match="cancelled"):
        preview(data, run, lr, engine)
    assert not engine.render_calls
    status = wait(data, "lut-refresh", 10)
    status.update(status="cancelled", cancellation={"requested": True})
    revision = read_json(run / "develop.json")["revision"]
    result = run_style_worker(
        run, data, "lut-retry", revision, group_id=1, lut_id="cube-test", lut_hash="lut-hash",
        amount=65, preview_only=True, lut_engine_factory=lambda _: engine,
        batch_status_reader=lambda *_: status,
        bridge_status_reader=lambda *_: pytest.fail("safe cancelled neutral should be reused"),
        batch_creator=lambda *_args, **_kwargs: pytest.fail("must not repeat completed RAW render"),
    )
    assert result["status"] == "complete"
    assert result["develop"]["creative_style"]["groups"]["1"]["strength"] == 65


def test_lut_refresh_progress_exposes_conditional_lightroom_stage():
    plan = _job_progress_plan("style_preview", {"lut_id": "x", "may_rebuild_neutral": True})
    assert [node["key"] for node in plan] == ["launch", "process", "collect", "lut", "finalize"]
    assert sum(node["weight"] for node in plan) == 100


def test_neutral_cache_survives_interruption_before_plan_state_is_saved(tmp_path):
    data, run, raw, lr, engine = fixture(tmp_path)
    preview(data, run, lr, engine)
    rec = read_json(run / "style-recommendations.json")
    rec["groups"][0]["neutral_render_task"]["render_status"] = "pending"
    write_json(run / "style-recommendations.json", rec)
    lr.create = lambda *_args, **_kwargs: pytest.fail("verified cache must not be rendered twice")
    result = preview(data, run, lr, engine, revision=4, batch="lut-recover-file")
    assert result["status"] == "complete"
    assert len(engine.render_calls) == 1


def test_source_change_during_lut_render_does_not_confirm_choice(tmp_path):
    data, run, raw, lr, engine = fixture(tmp_path)
    render = engine.render_jpeg
    def changed_source(*args, **kwargs):
        result = render(*args, **kwargs)
        raw.write_bytes(b"changed-during-lut")
        return result
    engine.render_jpeg = changed_source
    with pytest.raises(StyleWorkerError, match="基础预览已过期"):
        preview(data, run, lr, engine)
    selected = read_json(run / "develop.json")["creative_style"]["groups"]["1"]
    assert selected.get("status") != "confirmed"
