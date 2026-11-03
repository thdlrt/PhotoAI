from pathlib import Path

import pytest
from PIL import Image

import landscape_culler.lut_export_worker as worker
from landscape_culler.creative_lut import CreativeLutEngine
from landscape_culler.util import full_fingerprint, read_json, write_json


def fixture(tmp_path, monkeypatch, count=2):
    data = tmp_path / "data"
    monkeypatch.setenv("PHOTO_AI_DATA_DIR", str(data))
    source = tmp_path / "invert.cube"
    source.write_text('LUT_1D_SIZE 2\n1 1 1\n0 0 0\n', encoding='utf-8')
    engine = CreativeLutEngine(tmp_path / "luts", enforce_e_drive=False)
    lut = engine.import_lut(source)
    output = tmp_path / "output"
    output.mkdir()
    items = []
    for index in range(count):
        jpeg = output / f"{index}.jpg"
        Image.new("RGB", (24, 16), (20 + index, 70, 180)).save(jpeg, "JPEG", quality=95)
        items.append(dict(item_id=str(index), jpeg_path=str(jpeg), lut_id=lut.lut_id,
            lut_hash=lut.lut_hash, strength=65, input_fingerprint=full_fingerprint(jpeg)))
    task = data / "exports/attempt/lut-export-task.json"
    write_json(task, dict(protocol=worker.LUT_EXPORT_PROTOCOL, export_spec_id="export-test", attempt_id="attempt-test",
                         output_root=str(output), jpeg_quality=90, items=items))
    return task, engine


def test_crash_after_first_replace_recovers_without_double_lut(tmp_path, monkeypatch):
    task, engine = fixture(tmp_path, monkeypatch)
    payload = read_json(task)
    paths = [Path(item['jpeg_path']) for item in payload['items']]
    originals = [path.read_bytes() for path in paths]
    replace = worker._atomic_replace_from_cache
    def crash(*args):
        replace(*args)
        raise KeyboardInterrupt('simulated process termination after commit')
    monkeypatch.setattr(worker, '_atomic_replace_from_cache', crash)
    with pytest.raises(KeyboardInterrupt):
        worker.render_export_luts(task, engine_factory=lambda _: engine)
    first = paths[0].read_bytes()
    assert first != originals[0] and paths[1].read_bytes() == originals[1]
    recovered = worker.verified_export_lut_results(task)
    assert [row['item_id'] for row in recovered['items']] == ['0']
    first_receipts = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                      for path in task.parent.glob('lut-export-checkpoints/**/*.json')}
    monkeypatch.setattr(worker, '_atomic_replace_from_cache', replace)
    rendered = []
    real_render = engine.render_jpeg
    def record(path, **kwargs):
        rendered.append(path)
        return real_render(path, **kwargs)
    monkeypatch.setattr(engine, 'render_jpeg', record)
    result = worker.render_export_luts(task, engine_factory=lambda _: engine)
    assert result['status'] == 'completed' and rendered == [paths[1]]
    assert paths[0].read_bytes() == first and paths[1].read_bytes() != originals[1]
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == before for path,before in first_receipts.items())
    assert worker.reusable_export_lut_inputs(task)['items'] == []
    assert len(worker.verified_export_lut_results(task)['items']) == 2


def test_change_during_render_is_not_overwritten(tmp_path, monkeypatch):
    task, engine = fixture(tmp_path, monkeypatch, 1)
    target = Path(read_json(task)['items'][0]['jpeg_path'])
    render = engine.render_jpeg
    def edited(path, **kwargs):
        result = render(path, **kwargs)
        Image.new('RGB', (20, 20), 'red').save(path)
        return result
    monkeypatch.setattr(engine, 'render_jpeg', edited)
    result = worker.render_export_luts(task, engine_factory=lambda _: engine)
    assert result['failed'] == 1 and '拒绝覆盖' in result['items'][0]['error']
    with Image.open(target) as image:
        assert image.size == (20, 20)
    assert not worker.verified_export_lut_results(task)['items']


@pytest.mark.parametrize('change', ['strength', 'attempt', 'output'])
def test_receipt_requires_same_task_and_unchanged_output(tmp_path, monkeypatch, change):
    task, engine = fixture(tmp_path, monkeypatch, 1)
    worker.render_export_luts(task, engine_factory=lambda _: engine)
    payload = read_json(task)
    if change == 'strength':
        payload['items'][0]['strength'] = 150
    elif change == 'attempt':
        payload['attempt_id'] = 'other-attempt'
    else:
        Path(payload['items'][0]['jpeg_path']).write_bytes(b'user-edited')
    write_json(task, payload)
    assert worker.verified_export_lut_results(task)['items'] == []


def test_partial_failure_keeps_success_receipt_and_only_unmodified_base_is_reusable(tmp_path, monkeypatch):
    task, engine = fixture(tmp_path, monkeypatch)
    render = engine.render_jpeg
    def fail_second(path, **kwargs):
        if path.name == '1.jpg':
            raise RuntimeError('test resource unavailable')
        return render(path, **kwargs)
    monkeypatch.setattr(engine, 'render_jpeg', fail_second)
    result = worker.render_export_luts(task, engine_factory=lambda _: engine)
    assert result['status'] == 'partial'
    assert [row['item_id'] for row in worker.verified_export_lut_results(task)['items']] == ['0']
    assert [row['item_id'] for row in worker.reusable_export_lut_inputs(task)['items']] == ['1']


def test_duplicate_ids_rejected_before_render(tmp_path, monkeypatch):
    task, engine = fixture(tmp_path, monkeypatch)
    payload = read_json(task)
    payload['items'][1]['item_id'] = payload['items'][0]['item_id']
    write_json(task, payload)
    with pytest.raises(worker.LutExportTaskError, match='重复'):
        worker.render_export_luts(task, engine_factory=lambda _: pytest.fail('must validate first'))


def test_wrong_render_identity_is_never_published(tmp_path, monkeypatch):
    from dataclasses import replace
    task, engine = fixture(tmp_path, monkeypatch, 1)
    target = Path(read_json(task)['items'][0]['jpeg_path'])
    before = target.read_bytes()
    render = engine.render_jpeg
    monkeypatch.setattr(engine, 'render_jpeg', lambda *args, **kwargs: replace(render(*args, **kwargs), strength=150))
    result = worker.render_export_luts(task, engine_factory=lambda _: engine)
    assert result['failed'] == 1 and target.read_bytes() == before
    assert not worker.verified_export_lut_results(task)['items']
