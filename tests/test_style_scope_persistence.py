import copy

import pytest

from landscape_culler.develop import confirm_recommended_styles, merge_confirmed_develop
from landscape_culler.util import read_json, write_json
from test_develop import _style_plan
from test_job_photo_scope import active_job
from test_web import RUN_ID, _client


def setup_plan(tmp_path, monkeypatch):
    client, data, raw = _client(tmp_path, monkeypatch)
    headers = {'X-Photo-AI-Token': client.get('/api/bootstrap').json()['token']}
    prefix = f'/api/runs/{RUN_ID}/develop'
    plan = client.post(prefix + '/skip-analysis', headers=headers, json={'base_revision': 0}).json()
    plan = client.post(prefix + '/options', headers=headers,
        json={'base_revision': plan['revision'], 'mode': 'skip', 'stage': 'basic'}).json()
    plan = client.put(prefix + '/style/global', headers=headers,
        json={'base_revision': plan['revision'], 'status': 'skipped'}).json()
    plan = client.post(prefix + '/style/confirm-all', headers=headers,
        json={'base_revision': plan['revision'], 'scope': 'global'}).json()
    return client, data, raw, headers, prefix, plan


def test_scope_is_saved_reloaded_and_missing_group_choices_cannot_export(tmp_path, monkeypatch):
    client, data, raw, headers, prefix, plan = setup_plan(tmp_path, monkeypatch)
    folder = data / 'runs' / RUN_ID
    original = read_json(folder / 'develop.json')
    source = (folder / 'results.json').read_bytes()
    response = client.put(prefix + '/style/scope', headers=headers,
        json={'base_revision': plan['revision'], 'scope': 'group'})
    assert response.status_code == 200, response.text
    group = response.json()
    assert group['revision'] == plan['revision'] + 1
    assert group['creative_style']['scope'] == 'group'
    assert group['creative_style']['status'] == 'pending'
    assert client.get(prefix).json()['creative_style']['scope'] == 'group'
    rejected = client.post(f'/api/runs/{RUN_ID}/exports/prepare', headers=headers,
        json={'base_revision': 0, 'develop_revision': group['revision'], 'xmp': True, 'jpeg': False})
    assert rejected.status_code == 409
    response = client.put(prefix + '/style/scope', headers=headers,
        json={'base_revision': group['revision'], 'scope': 'global'})
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored['creative_style']['status'] == 'confirmed'
    assert restored['basic_color']['status'] == 'skipped'
    assert read_json(folder / 'develop.json')['creative_style']['global_selection'] == original['creative_style']['global_selection']
    accepted = client.post(f'/api/runs/{RUN_ID}/exports/prepare', headers=headers,
        json={'base_revision': 0, 'develop_revision': restored['revision'], 'xmp': True, 'jpeg': False})
    assert accepted.status_code == 200, accepted.text
    assert (folder / 'results.json').read_bytes() == source and raw.read_bytes() == b'raw'
    before = (folder / 'develop.json').read_bytes()
    client.get(prefix)
    assert (folder / 'develop.json').read_bytes() == before


@pytest.mark.parametrize('guard', ['stale', 'own-job', 'other-job', 'invalid-scope'])
def test_scope_write_has_revision_and_project_guards(tmp_path, monkeypatch, guard):
    client, data, raw, headers, prefix, plan = setup_plan(tmp_path, monkeypatch)
    path = data / 'runs' / RUN_ID / 'develop.json'
    before = path.read_bytes()
    if guard.endswith('job'):
        active_job(client, {'run_id': RUN_ID} if guard == 'own-job' else
                   {'run_id': 'other', 'input_root': str(tmp_path / 'other-photos')})
    response = client.put(prefix + '/style/scope', headers=headers,
        json={'base_revision': plan['revision'] - (guard == 'stale'),
              'scope': 'unexpected' if guard == 'invalid-scope' else 'group'})
    assert response.status_code == (200 if guard == 'other-job' else 422 if guard == 'invalid-scope' else 409), response.text
    if guard != 'other-job': assert path.read_bytes() == before
    assert raw.read_bytes() == b'raw'


def test_confirm_honors_requested_scope_atomically(tmp_path, monkeypatch):
    client, data, raw, headers, prefix, plan = setup_plan(tmp_path, monkeypatch)
    path = data / 'runs' / RUN_ID / 'develop.json'
    before = path.read_bytes()
    response = client.post(prefix + '/style/confirm-all', headers=headers,
        json={'base_revision': plan['revision'], 'scope': 'group'})
    assert response.status_code == 409, 'Cannot confirm global when user explicitly requested missing group selections'
    assert path.read_bytes() == before


def test_global_confirmation_does_not_replace_individual_choices(tmp_path):
    plan = _style_plan()
    plan['creative_style']['groups']['1'].update(status='skipped', manual_override=True, preset_id=None, reason='my natural choice')
    original_groups = copy.deepcopy(plan['creative_style']['groups'])
    selection = copy.deepcopy(original_groups['1'])
    selection.update(status='confirmed', preset_id='global-preset', preset_hash='global-hash', amount=100)
    plan['creative_style'].update(scope='global', global_selection=selection)
    confirm_recommended_styles(plan, tmp_path)
    assert plan['creative_style']['groups'] == original_groups
    payload = {'results': [{'rating': 4, 'group_id': 1}]}
    merged = merge_confirmed_develop(copy.deepcopy(payload), plan, 0)
    assert merged['results'][0]['develop']['creative_style']['preset_id'] == 'global-preset'
    # Switching back must restore the independent choice without rerunning AI.
    from landscape_culler.develop import update_style_scope
    update_style_scope(plan, tmp_path, scope='group')
    merged = merge_confirmed_develop(copy.deepcopy(payload), plan, 0)
    assert merged['results'][0]['develop']['creative_style']['status'] == 'skipped'
    assert merged['results'][0]['develop']['creative_style']['preset_id'] is None


def test_inactive_removed_groups_do_not_block_current_xmp_export():
    from landscape_culler.web import _selected_rendered_luts
    plan = {'items': [{'group_id': 1}], 'creative_style': {'status': 'confirmed', 'scope': 'group', 'groups': {
        '1': {'status': 'skipped', 'manual_override': True},
        '9': {'status': 'confirmed', 'lut_id': 'removed-group-lut'}}}}
    assert _selected_rendered_luts(plan) == []
    plan['items'].append({'group_id': 9})
    assert _selected_rendered_luts(plan)[0]['lut_id'] == 'removed-group-lut'
