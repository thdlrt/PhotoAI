import pytest

from landscape_culler import develop, develop_checkpoint, util
from landscape_culler.util import read_json, write_json
from test_develop_resume import _crop, _photos, _records, _legacy_checkpoint


def test_legacy_resume_keeps_old_state_and_writes_only_new_photo(tmp_path, monkeypatch):
    payload, run = _photos(tmp_path, 3)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan({**payload, "results": payload["results"][:2]}, run, 0)
    legacy = _legacy_checkpoint(run)
    before = (legacy.read_bytes(), legacy.stat().st_mtime_ns)
    assert develop.develop_resume_summary(payload, run) == dict(completed=2, analyzed=2, total=3)
    assert not _records(run)
    calls = []
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: (calls.append(1), _crop())[1])
    plan = develop.create_develop_plan(payload, run, 1)
    assert len(plan["items"]) == 3 and len(calls) == 1
    assert len(_records(run)) == 1
    assert (legacy.read_bytes(), legacy.stat().st_mtime_ns) == before
    assert develop.develop_resume_summary(payload, run)["completed"] == 3


def test_switching_model_back_recovers_its_previous_analysis(tmp_path, monkeypatch):
    payload, run = _photos(tmp_path, 2)
    calls = []
    context = {"model": "A"}
    monkeypatch.setattr(develop, "_develop_checkpoint_context", lambda _: dict(context))
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: (calls.append(context["model"]), _crop())[1])
    develop.create_develop_plan(payload, run, 0)
    first = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in _records(run)}
    context["model"] = "B"
    assert develop.develop_resume_summary(payload, run)["analyzed"] == 0
    develop.create_develop_plan(payload, run, 0)
    context["model"] = "A"
    develop.create_develop_plan(payload, run, 0)
    assert calls == ["A", "A", "B", "B"]
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == data for p, data in first.items())


@pytest.mark.parametrize("damage", ["json", "key", "context", "schema"])
def test_damaged_record_invalidates_only_its_photo(tmp_path, monkeypatch, damage):
    payload, run = _photos(tmp_path, 3)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan(payload, run, 0)
    damaged, *other = _records(run)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in other}
    record = read_json(damaged)
    if damage == "json":
        damaged.write_text("{truncated", encoding="utf-8")
    else:
        record[{"key": "source_key", "context": "context", "schema": "schema_version"}[damage]] = "wrong"
        write_json(damaged, record)
    assert develop.develop_resume_summary(payload, run)["analyzed"] == 2
    calls = []
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: (calls.append(1), _crop())[1])
    develop.create_develop_plan(payload, run, 0)
    assert len(calls) == 1
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == data for p, data in before.items())


def test_completed_records_do_not_parse_legacy_aggregate(tmp_path, monkeypatch):
    payload, run = _photos(tmp_path, 2)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan(payload, run, 0)
    original = develop_checkpoint.read_json
    def read(path):
        assert path.name != "develop-checkpoint.json"
        return original(path)
    monkeypatch.setattr(develop_checkpoint, "read_json", read)
    assert develop.develop_resume_summary(payload, run)["completed"] == 2


def test_corrupt_new_record_falls_back_to_valid_legacy_without_rewriting(tmp_path, monkeypatch):
    payload, run = _photos(tmp_path, 1)
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    develop.create_develop_plan(payload, run, 0)
    record_path = _records(run)[0]
    record = read_json(record_path)
    legacy = _legacy_checkpoint(run)
    record["item"]["crop_id"] = "invalid"
    write_json(record_path, record)
    before = {p: p.read_bytes() for p in [legacy, record_path]}
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: pytest.fail("valid legacy AI should be reused"))
    assert develop.create_develop_plan(payload, run, 0)["items"][0]["crop_id"] == "original"
    assert all(p.read_bytes() == data for p, data in before.items())


def test_failed_preview_checkpoint_replace_keeps_durable_analysis(tmp_path, monkeypatch):
    payload, run = _photos(tmp_path, 1)
    old_plan = run / "develop.json"
    old_plan.write_bytes(b'{"manual":"preserved"}')
    monkeypatch.setattr(develop, "smart_crop_candidates", _crop)
    original = util._replace_with_retry
    def fail_preview_record(source, target):
        if "develop-checkpoints" in target.parts and target.exists():
            raise PermissionError("checkpoint update interrupted")
        return original(source, target)
    monkeypatch.setattr(util, "_replace_with_retry", fail_preview_record)
    with pytest.raises(PermissionError):
        develop.create_develop_plan(payload, run, 0)
    assert old_plan.read_bytes() == b'{"manual":"preserved"}'
    assert develop.develop_resume_summary(payload, run) == dict(completed=0, analyzed=1, total=1)
    assert not list(run.rglob("*.tmp"))
    monkeypatch.setattr(util, "_replace_with_retry", original)
    monkeypatch.setattr(develop, "smart_crop_candidates", lambda *a, **k: pytest.fail("durable AI must survive failed replace"))
    develop.create_develop_plan(payload, run, 0)
    assert develop.develop_resume_summary(payload, run)["completed"] == 1
