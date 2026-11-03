from pathlib import Path

import pytest

from landscape_culler import web
from landscape_culler.content_root import initialize_content_root
from landscape_culler.lightroom_bridge import PLUGIN_VERSION
from landscape_culler.util import read_json, write_json
from test_web import RUN_ID, _client, _prepare_style_plan


@pytest.mark.parametrize('kind', ['style_preview', 'lut_export'])
def test_exact_render_environment_never_resolves_clip_weights(tmp_path, monkeypatch, kind):
    layout = initialize_content_root(tmp_path/'content', apply_environment=False, persist_registry=False)
    manager = web.JobManager(layout.state, Path(__file__).parents[1], layout)
    monkeypatch.setattr(web, 'clip_model_reference', lambda *_: pytest.fail('Exact render must not resolve CLIP'))
    monkeypatch.setenv('PHOTO_AI_CLIP_MODEL', 'wrong-other-project')
    environment = manager._job_environment({'kind':kind,'context':{}})
    assert 'PHOTO_AI_CLIP_MODEL' not in environment
    assert environment['PHOTO_AI_CONTENT_ROOT'] == str(layout.root)


@pytest.mark.parametrize('condition', ['ready', 'missing', 'outdated', 'missing-python'])
def test_preview_runtime_validates_worker_without_model_readiness(tmp_path, monkeypatch, condition):
    layout = initialize_content_root(tmp_path/'content', apply_environment=False, persist_registry=False)
    manager = web.JobManager(layout.state, Path(__file__).parents[1], layout)
    engine = layout.runtimes/'engines/test'
    python = engine/'venv/Scripts/python.exe'
    if condition != 'missing-python':
        python.parent.mkdir(parents=True)
        python.write_bytes(b'test interpreter identity')
    monkeypatch.setattr(web, 'active_engine', lambda _: None if condition=='missing' else engine)
    monkeypatch.setattr(web, 'ai_worker_needs_update', lambda _: condition=='outdated')
    monkeypatch.setattr(web, 'ai_resources_status', lambda _: pytest.fail('Must not inspect model profiles'))
    before = {p:p.read_bytes() for p in layout.root.rglob('*') if p.is_file()}
    status = manager.preview_runtime_status()
    assert status['ready'] is (condition=='ready')
    assert status['managed'] is True
    assert {p:p.read_bytes() for p in layout.root.rglob('*') if p.is_file()} == before
    if condition == 'ready':
        command = manager._job_command('style_preview',['style-recommend','--preview-only'],'preview-no-models')
        assert command[0] == str(python)
    else:
        with pytest.raises(RuntimeError):
            manager._job_command('style_preview',['style-recommend','--preview-only'],'preview-no-models')
        assert not (manager.jobs_dir/'preview-no-models.spec.json').exists()


def preview_fixture(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    headers, revision = _prepare_style_plan(client, token=client.get('/api/bootstrap').json()['token'])
    plan_path = data/'runs'/RUN_ID/'develop.json'
    plan = read_json(plan_path)
    plan['creative_style'] = {'scope':'global','status':'pending','groups':{},'global_selection':{
        'status':'pending','top3':[{'preset_id':'test-look','preset_hash':'test-hash','amount':100}]}}
    write_json(plan_path, plan)
    executable = tmp_path/'Lightroom.exe'
    executable.write_bytes(b'test')
    monkeypatch.setattr(web, '_configured_style_catalog', lambda *_:{'default_pool':['test-look'],'entries':[
        {'preset_id':'test-look','file_hash':'test-hash','registration_status':'installed','supports_amount':True}]})
    monkeypatch.setattr(web, '_lightroom_status',lambda *_:{'configured':True,'plugin':{'points_to_this_bridge':True},
        'lightroom':{'compatible':True,'executable':str(executable)},'heartbeat':{'state':'online','plugin_version':PLUGIN_VERSION}})
    monkeypatch.setattr(web, 'active_model_profile_readiness', lambda *_:{'ready':False,'message':'DINO and CLIP weights absent'})
    calls=[]
    def start(kind,args,context):
        calls.append((kind,args,context))
        return {'id':'preview-queued','kind':kind,'status':'queued','context':context}
    monkeypatch.setattr(client.app.state.jobs,'start',start)
    body={'base_revision':revision,'scope':'global','preset_id':'test-look','preset_hash':'test-hash','amount':100}
    return client,data,raw,headers,body,calls


def test_manual_preview_and_retry_work_with_missing_ai_models_but_recommendation_still_blocks(tmp_path, monkeypatch):
    client,data,raw,headers,body,calls = preview_fixture(tmp_path,monkeypatch)
    assert client.get('/api/style-runtime').json()['ready'] is True
    response=client.post(f'/api/runs/{RUN_ID}/style-preview',headers=headers,json=body)
    assert response.status_code==200,response.text
    kind,args,context=calls[-1]
    assert kind=='style_preview' and '--preview-only' in args
    client.app.state.jobs.jobs['failed-preview']={'id':'failed-preview','kind':kind,'status':'failed','context':context,
        'retry_spec':{'kind':kind,'argv':args,'context':context}}
    response=client.post('/api/jobs/failed-preview/retry',headers=headers)
    assert response.status_code==200,response.text
    assert len(calls)==2
    blocked=client.post('/api/projects',headers=headers,json={'input_path':str(raw.parent)})
    assert blocked.status_code==409
    assert blocked.json()['detail']['code']=='model_profile_incomplete'
    assert raw.read_bytes()==b'raw'


def test_missing_preview_component_is_actionable_and_never_starts_a_job(tmp_path, monkeypatch):
    client,data,raw,headers,body,calls = preview_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(client.app.state.jobs,'preview_runtime_status',lambda:{'ready':False,'managed':True,'message':'运行组件缺失'})
    response=client.post(f'/api/runs/{RUN_ID}/style-preview',headers=headers,json=body)
    assert response.status_code==409
    assert response.json()['detail']['code']=='style_runtime_incomplete'
    assert calls==[]


def test_exact_preset_worker_skips_every_inference_stage(tmp_path, monkeypatch):
    from test_style_worker import _run, _catalog, _online, _FakeLightroom
    from landscape_culler import style_worker
    data, folder, raw = _run(tmp_path)
    fake = _FakeLightroom()
    catalog = _catalog(2)
    monkeypatch.setattr(style_worker, '_load_runtime_style_catalog', lambda _:catalog)
    monkeypatch.setattr(style_worker, '_read_runtime_preset_snapshot', lambda _:[
        {'scope':'catalog','uuid':entry['runtime_preset_uuid'],'name':entry['name']} for entry in catalog['entries']])
    def no_inference(*args, **kwargs):
        pytest.fail('Manual exact preview must not invoke model inference')
    result=style_worker.run_style_worker(folder,data,'manual-no-weights',3,scope='global',
        preset_id='uuid:p0',preset_hash='hash-0',amount=100,preview_only=True,
        bridge_status_reader=_online,batch_creator=fake.create,batch_waiter=fake.wait,
        scorer_factory=no_inference,cascade_runner=no_inference)
    selection=result['develop']['creative_style']['global_selection']
    assert selection['status']=='confirmed' and selection['preset_id']=='uuid:p0'
    assert Path(selection['selected_preview_path']).is_file()
    assert raw.read_bytes()==b'raw-test-data'
