from pathlib import Path

import pytest
from PIL import Image

from landscape_culler.creative_lut import CreativeLutEngine
from landscape_culler.export_state import (create_export_attempt, activate_export_attempt,
    record_export_result, finish_export_attempt, export_spec_path)
from landscape_culler.lut_export_worker import render_export_luts
from landscape_culler.util import read_json, write_json
from landscape_culler.web import _prepare_lut_export_task
from test_export_web import _ready_client, _prepare


def fixture(tmp_path, monkeypatch):
    client, data, raw, headers = _ready_client(tmp_path, monkeypatch)
    monkeypatch.setenv('PHOTO_AI_DATA_DIR', str(data))
    spec = _prepare(client, headers, xmp=False, jpeg=True)
    spec = read_json(export_spec_path(data, spec['export_spec_id']))
    source = tmp_path / 'invert.cube'
    source.write_text('LUT_1D_SIZE 2\n1 1 1\n0 0 0\n', encoding='utf-8')
    engine = CreativeLutEngine(tmp_path / 'luts', enforce_e_drive=False)
    lut = engine.import_lut(source)
    spec['items'][0]['recipe']['creative_style'] = dict(status='confirmed', look_kind='rendered_lut',
        lut_id=lut.lut_id, lut_hash=lut.lut_hash, strength=65, amount=65)
    execution = create_export_attempt(data, spec)
    activate_export_attempt(data, spec, attempt_id=execution['attempt_id'], job_id='lr-job', batch_id='lr-batch')
    jpeg = Path(spec['output_dir']) / 'base.jpg'
    jpeg.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (30,20), (20,80,190)).save(jpeg, 'JPEG', quality=95)
    batch = {'status':'complete', 'tasks':[{'photo_path':str(raw), 'status':'done',
             'result':{'jpeg_status':'done', 'jpeg_path':str(jpeg)}}]}
    task, ids, failures = _prepare_lut_export_task(spec, execution, batch)
    execution['lut_postprocess'] = dict(task_path=str(task),expected_item_ids=ids,preflight_failures=failures,
                                      job_id='lut-job',status='running')
    write_json(export_spec_path(data,spec['export_spec_id']), spec)
    jobs = client.app.state.jobs
    jobs.jobs['lr-job'] = dict(id='lr-job',status='completed',message='done')
    jobs.jobs['lut-job'] = dict(id='lut-job',status='failed',message='process exited')
    monkeypatch.setattr('landscape_culler.web.read_lightroom_batch_status', lambda *_:batch)
    return client,data,headers,spec,execution,task,engine,jpeg,batch


def test_service_recovers_completed_lut_when_worker_envelope_was_lost(tmp_path, monkeypatch):
    client,data,headers,spec,execution,task,engine,jpeg,batch = fixture(tmp_path,monkeypatch)
    render_export_luts(task,engine_factory=lambda _:engine)
    response = client.get(f"/api/exports/{spec['export_spec_id']}")
    assert response.status_code == 200 and response.json()['status'] == 'completed'
    assert response.json()['results']['jpeg']['succeeded'] == 1


@pytest.mark.parametrize('entry', ['export', 'job'])
def test_retry_uses_existing_base_without_lightroom_then_recovers_completion(tmp_path, monkeypatch, entry):
    client,data,headers,spec,execution,task,engine,jpeg,batch = fixture(tmp_path,monkeypatch)
    response = client.get(f"/api/exports/{spec['export_spec_id']}")
    assert response.json()['status'] == 'partial'
    calls=[]
    def start(kind,args,context):
        calls.append((kind,args,context))
        job = dict(id='retry-lut',kind=kind,status='running',title=context['title'])
        client.app.state.jobs.jobs[job['id']] = job
        return job
    monkeypatch.setattr(client.app.state.jobs,'start',start)
    monkeypatch.setattr('landscape_culler.web._lightroom_status',lambda *_:pytest.fail('LUT retry must not launch Lightroom'))
    client.app.state.jobs.jobs['lut-job'].update(kind='lut_export',context={'export_spec_id':spec['export_spec_id']})
    endpoint = f"/api/exports/{spec['export_spec_id']}/execute" if entry == 'export' else '/api/jobs/lut-job/retry'
    retry = client.post(endpoint,json={},headers=headers)
    assert retry.status_code == 200, retry.text
    kind,args,context = calls[0]
    assert kind == 'lut_export' and context['run_id'] == spec['run_id']
    updated = read_json(export_spec_path(data,spec['export_spec_id']))
    assert updated['executions'][-1]['engine'] == 'lut_only'
    new_task = Path(args[args.index('--task')+1])
    assert read_json(new_task)['items'][0]['jpeg_path'] == str(jpeg.resolve())
    before = jpeg.read_bytes()
    render_export_luts(new_task,engine_factory=lambda _:engine)
    client.app.state.jobs.jobs['retry-lut'].update(status='failed',message='lost final acknowledgement')
    done = client.get(f"/api/exports/{spec['export_spec_id']}").json()
    assert done['status'] == 'completed' and jpeg.read_bytes() != before
    assert len(calls) == 1


def test_cancelled_lightroom_does_not_start_new_lut_phase(tmp_path, monkeypatch):
    client,data,headers,spec,execution,task,engine,jpeg,batch = fixture(tmp_path,monkeypatch)
    execution.pop('lut_postprocess')
    write_json(export_spec_path(data,spec['export_spec_id']),spec)
    client.app.state.jobs.jobs['lr-job'].update(status='cancelled')
    monkeypatch.setattr(client.app.state.jobs,'start',lambda *_args,**_kwargs:pytest.fail('cancel must stop before next phase'))
    before = jpeg.read_bytes()
    response = client.get(f"/api/exports/{spec['export_spec_id']}").json()
    assert response['status'] == 'partial' and '取消' in response['message']
    assert jpeg.read_bytes() == before


def test_changed_base_cannot_be_used_for_lut_only_retry(tmp_path, monkeypatch):
    from landscape_culler.web import _reuse_lut_export_bases
    client,data,headers,spec,execution,task,engine,jpeg,batch = fixture(tmp_path,monkeypatch)
    record_export_result(data,spec,item_id=spec['items'][0]['item_id'],target='jpeg',succeeded=False,error='retry')
    finish_export_attempt(data,spec,attempt_id=execution['attempt_id'],status='failed')
    jpeg.write_bytes(b'user changed output')
    retry = create_export_attempt(data,spec)
    assert not _reuse_lut_export_bases(data,spec,retry)
    assert retry['engine'] == 'lightroom'


def test_worker_started_before_service_saved_phase_does_not_refreeze_rendered_jpeg(tmp_path, monkeypatch):
    client,data,headers,spec,execution,task,engine,jpeg,batch = fixture(tmp_path,monkeypatch)
    original_task = task.read_bytes()
    render_export_luts(task, engine_factory=lambda _:engine)
    completed = jpeg.read_bytes()
    execution.pop('lut_postprocess')
    recovered_task, ids, failures = _prepare_lut_export_task(spec, execution, batch)
    assert recovered_task == task and task.read_bytes() == original_task and not failures
    monkeypatch.setattr(engine,'render_jpeg',lambda *_args,**_kwargs:pytest.fail('completed JPEG must not receive LUT twice'))
    result = render_export_luts(recovered_task,engine_factory=lambda _:engine)
    assert result['status'] == 'completed' and jpeg.read_bytes() == completed
