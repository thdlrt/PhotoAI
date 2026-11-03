import os
from pathlib import Path

import pytest

from landscape_culler import develop
from landscape_culler.util import read_json, write_json
from test_develop_resume import _photos, _crop, _records, _legacy_checkpoint
from test_web import _client, RUN_ID


def test_marker_wrapped_corrupt_jpeg_is_repaired_without_ai(tmp_path, monkeypatch):
    payload,run = _photos(tmp_path,1)
    monkeypatch.setattr(develop,'smart_crop_candidates',_crop)
    plan = develop.create_develop_plan(payload,run,0)
    preview = Path(plan['items'][0]['preview_path'])
    preview.write_bytes(b'\xff\xd8not a decodable JPEG\xff\xd9')
    summary = develop.develop_resume_summary(payload,run)
    assert summary == dict(completed=0,analyzed=1,total=1)
    assert develop.develop_resume_percent(summary) == 85.7
    monkeypatch.setattr(develop,'smart_crop_candidates',lambda *_a,**_k:pytest.fail('saved AI result must survive preview damage'))
    develop.create_develop_plan(payload,run,0)
    assert develop.develop_resume_summary(payload,run)['completed'] == 1


def test_old_preview_renderer_reuses_analysis_with_monotonic_resume_progress(tmp_path, monkeypatch):
    payload,run = _photos(tmp_path,3)
    monkeypatch.setattr(develop,'smart_crop_candidates',_crop)
    develop.create_develop_plan(payload,run,0)
    legacy = _legacy_checkpoint(run)
    checkpoint = read_json(legacy)
    for item in checkpoint['items'].values():
        item.pop('preview_version')
    write_json(run/'develop-checkpoint.json',checkpoint)
    before = (run/'develop-checkpoint.json').read_bytes()
    summary = develop.develop_resume_summary(payload,run)
    assert summary == dict(completed=0,analyzed=3,total=3)
    assert (run/'develop-checkpoint.json').read_bytes() == before
    monkeypatch.setattr(develop,'smart_crop_candidates',lambda *_a,**_k:pytest.fail('renderer update must not rerun AI'))
    events=[]
    develop.create_develop_plan(payload,run,0,events.append)
    percents=[event['overall_percent'] for event in events]
    assert percents[0] == 85.7 and percents[-1] == 100
    assert percents == sorted(percents)
    assert legacy.read_bytes() == before


def test_invalid_cached_crop_does_not_create_a_broken_plan(tmp_path, monkeypatch):
    payload,run = _photos(tmp_path,1)
    monkeypatch.setattr(develop,'smart_crop_candidates',_crop)
    develop.create_develop_plan(payload,run,0)
    record = _records(run)[0]
    checkpoint=read_json(record)
    checkpoint['item']['crop_id']='not-present'
    write_json(record,checkpoint)
    assert develop.develop_resume_summary(payload,run)['analyzed']==0
    calls=[]
    monkeypatch.setattr(develop,'smart_crop_candidates',lambda *_a,**_k:(calls.append(1),_crop())[1])
    assert develop.create_develop_plan(payload,run,0)['items'][0]['crop_id']=='original'
    assert len(calls)==1


def test_repeated_progress_reads_decode_unchanged_jpeg_once(tmp_path, monkeypatch):
    payload,run = _photos(tmp_path,1)
    monkeypatch.setattr(develop,'smart_crop_candidates',_crop)
    develop.create_develop_plan(payload,run,0)
    develop._decoded_checkpoint_jpeg.cache_clear()
    calls=[]
    original=develop.Image.open
    def open_image(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(develop.Image,'open',open_image)
    for _ in range(4):
        assert develop.develop_resume_summary(payload,run)['completed']==1
    assert len(calls)==1


def test_temporary_preview_lock_is_not_cached_as_permanent_failure(tmp_path,monkeypatch):
    payload,run=_photos(tmp_path,1)
    monkeypatch.setattr(develop,'smart_crop_candidates',_crop)
    develop.create_develop_plan(payload,run,0)
    develop._decoded_checkpoint_jpeg.cache_clear()
    original=develop.Image.open
    calls=[]
    def locked_once(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:
            raise PermissionError('temporary Windows sharing violation')
        return original(*args,**kwargs)
    monkeypatch.setattr(develop.Image,'open',locked_once)
    assert develop.develop_resume_summary(payload,run)['completed']==0
    assert develop.develop_resume_summary(payload,run)['completed']==1
    assert develop.develop_resume_summary(payload,run)['completed']==1
    assert len(calls)==2


@pytest.mark.parametrize('status',['failed','completed','interrupted','cancelled','missing'])
def test_progress_uses_terminal_job_state_and_read_only_saved_evidence(tmp_path,monkeypatch,status):
    client,data,raw=_client(tmp_path,monkeypatch)
    run=data/'runs'/RUN_ID
    plan=develop.create_develop_plan(read_json(run/'results.json'),run,0)
    progress=run/'develop-progress.json'
    write_json(progress,dict(status='running',completed=0,total=1))
    if status=='missing':
        os.utime(progress,(1,1))
    else:
        client.app.state.jobs.jobs['crop'] = dict(id='crop',kind='develop',status=status,created_at='2026-10-10',
            context={'run_id':RUN_ID},result={'plan_id':plan['plan_id']},message='test terminal')
    before={p:p.read_bytes() for p in run.glob('*.json')}
    response=client.get(f'/api/runs/{RUN_ID}/develop/progress').json()
    assert response['status']==('interrupted' if status=='missing' else status)
    assert response['completed']==1 and response['analyzed']==1 and response['overall_percent']==100
    assert response['cancellable'] is False
    assert all(p.read_bytes()==value for p,value in before.items())


def test_completed_job_cannot_claim_a_different_old_plan(tmp_path,monkeypatch):
    client,data,raw=_client(tmp_path,monkeypatch)
    run=data/'runs'/RUN_ID
    develop.create_develop_plan(read_json(run/'results.json'),run,0)
    write_json(run/'develop-progress.json',dict(status='running'))
    client.app.state.jobs.jobs['crop'] = dict(id='crop',kind='develop',status='completed',created_at='2026-10-10',
        context={'run_id':RUN_ID},result={'plan_id':'different'})
    assert client.get(f'/api/runs/{RUN_ID}/develop/progress').json()['status']=='failed'
