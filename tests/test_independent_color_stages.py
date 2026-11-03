import copy

import pytest

from landscape_culler.develop import update_color_mode, update_style_group, confirm_recommended_styles
from landscape_culler.util import read_json, write_json
from test_develop import _style_plan
from test_web import RUN_ID, _client
from test_style_worker import _run, _catalog, _online
from landscape_culler.style_worker import start_style_preview_batch


@pytest.mark.parametrize('base_mode', ['auto', 'skip'])
def test_natural_without_ai_preserves_base_and_freezes_matching_export(tmp_path, monkeypatch, base_mode):
    client, data, raw = _client(tmp_path, monkeypatch)
    headers = {'X-Photo-AI-Token': client.get('/api/bootstrap').json()['token']}
    prefix = f'/api/runs/{RUN_ID}'
    def save(path, body, method='post'):
        response = getattr(client, method)(prefix + path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        return response.json()
    plan = save('/develop/skip-analysis', {'base_revision': 0})
    plan = save('/develop/options', {'base_revision': plan['revision'], 'mode': base_mode, 'stage': 'basic'})
    plan = save('/develop/style/global', {'base_revision': plan['revision'], 'status': 'skipped'}, 'put')
    assert plan['basic_color']['status'] == ('enabled' if base_mode == 'auto' else 'skipped')
    plan = save('/develop/style/confirm-all', {'base_revision': plan['revision'], 'scope': 'global'})
    spec = save('/exports/prepare', {'base_revision': 0, 'develop_revision': plan['revision'], 'xmp': True, 'jpeg': False})
    frozen = read_json(data / 'exports' / (spec['export_spec_id'] + '.json'))
    recipe = frozen['items'][0]['recipe']
    assert bool(recipe['base']) == (base_mode == 'auto')
    assert recipe['creative_style']['status'] == 'skipped'
    assert recipe['creative_style']['manual_override'] is True
    assert client.get('/api/jobs').json() == []
    assert raw.read_bytes() == b'raw'
    before = {p: p.read_bytes() for p in (data / 'runs' / RUN_ID).glob('*.json')}
    assert client.get(prefix + '/develop').status_code == 200
    assert all(p.read_bytes() == b for p, b in before.items())


def test_switching_base_invalidates_pixels_but_keeps_manual_resource_choice(tmp_path):
    plan = _style_plan()
    update_style_group(plan, tmp_path, 1, preset_id='uuid:style-1', preset_hash='hash-style-1', amount=65)
    update_color_mode(plan, tmp_path, mode='skip', stage='basic')
    chosen = plan['creative_style']['groups']['1']
    assert chosen['preset_id'] == 'uuid:style-1' and chosen['manual_override']
    assert chosen['status'] == 'pending'
    assert chosen['recommendation_status'] == 'stale'
    assert chosen['top3'][0]['render_status'] == 'stale'
    assert 'preview_key' not in chosen['top3'][0]
    with pytest.raises(ValueError):
        confirm_recommended_styles(plan, tmp_path)
    assert plan['basic_color']['status'] == 'skipped'


@pytest.mark.parametrize('base_mode', ['auto', 'skip'])
def test_creative_activation_and_skip_do_not_change_base(tmp_path, base_mode):
    plan = _style_plan()
    update_color_mode(plan, tmp_path, mode=base_mode, stage='basic')
    base = copy.deepcopy(plan['basic_color'])
    update_color_mode(plan, tmp_path, mode='style', stage='creative')
    assert plan['basic_color'] == base
    update_color_mode(plan, tmp_path, mode='skip', stage='creative')
    assert plan['basic_color'] == base
    assert plan['creative_style']['status'] == 'skipped'
    assert plan['color_mode'] == base_mode


@pytest.mark.parametrize('skip_base', [False, True])
def test_preview_lightroom_task_respects_base_and_keeps_source_unchanged(tmp_path, skip_base):
    data, run, raw = _run(tmp_path)
    plan = read_json(run / 'develop.json')
    plan['basic_color']['status'] = 'skipped' if skip_base else 'enabled'
    write_json(run / 'develop.json', plan)
    before = (run / 'develop.json').read_bytes()
    captured = []
    def create_batch(_data, tasks, *, batch_id):
        captured.extend(tasks)
        return {'batch_id': batch_id, 'status': 'pending'}
    start_style_preview_batch(run, data, 'base-choice-test', 3, catalog=_catalog(),
                              status_reader=_online, batch_creator=create_batch)
    assert captured
    for task in captured:
        assert task.auto_tone is (not skip_base)
        assert task.auto_white_balance is (not skip_base)
        assert task.lens_profile is (not skip_base)
        assert task.remove_chromatic_aberration is (not skip_base)
    assert (run / 'develop.json').read_bytes() == before
    assert raw.read_bytes() == b'raw-test-data'

@pytest.mark.parametrize('base_status', ['enabled', 'skipped'])
def test_worker_recommendation_and_forced_preview_keep_base_through_merge(tmp_path, base_status):
    from test_style_worker import _FakeLightroom, _FakeScorer
    from landscape_culler.style_worker import run_style_worker
    from landscape_culler.develop import merge_confirmed_develop
    data, run, raw = _run(tmp_path)
    plan = read_json(run / 'develop.json')
    plan['basic_color']['status'] = base_status
    plan['items'][0].update(confirmed=True, crop_confirmed=True)
    write_json(run / 'develop.json', plan)
    catalog = _catalog(7)
    fake = _FakeLightroom()
    run_style_worker(run, data, 'stage-recommend', 3, scope='global', catalog=catalog,
        bridge_status_reader=_online, batch_creator=fake.create, batch_waiter=fake.wait, scorer_factory=_FakeScorer)
    assert read_json(run / 'develop.json')['basic_color']['status'] == base_status
    fake = _FakeLightroom()
    forced = catalog['entries'][-1]
    result = run_style_worker(run, data, 'stage-preview', 4, scope='global',
        preset_id=forced['preset_id'], preset_hash=forced['file_hash'], amount=65, preview_only=True,
        catalog=catalog, bridge_status_reader=_online, batch_creator=fake.create,
        batch_waiter=fake.wait, scorer_factory=lambda _: pytest.fail('No AI rerank for manual preview'))
    plan = result['develop']
    assert plan['basic_color']['status'] == base_status
    assert plan['creative_style']['status'] == 'confirmed'
    payload = merge_confirmed_develop(read_json(run / 'results.json'), plan, 0)
    recipe = payload['results'][0]['develop']
    assert bool(recipe['base']) == (base_status == 'enabled')
    assert recipe['creative_style']['preset_id'] == forced['preset_id']
    assert recipe['creative_style']['amount'] == 65
    assert raw.read_bytes() == b'raw-test-data'


def test_skipped_style_does_not_keep_an_inactive_lut_export_restriction():
    from landscape_culler.web import _selected_rendered_luts
    selection = {'status': 'confirmed', 'lut_id': 'test-lut'}
    plan = {'creative_style': {'status': 'confirmed', 'scope': 'global', 'groups': {}, 'global_selection': selection}}
    assert _selected_rendered_luts(plan) == [selection]
    plan['creative_style']['status'] = 'skipped'
    assert _selected_rendered_luts(plan) == []
