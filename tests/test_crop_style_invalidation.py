import copy
from pathlib import Path

import pytest
from PIL import Image

from landscape_culler.develop import (confirm_all, create_original_develop_plan, skip_all_crops,
    update_develop_item, xmp_settings_for_recipe)
from landscape_culler.util import read_json, write_json
from test_develop import _style_plan


def fixture(tmp_path):
    preview = tmp_path / 'source.jpg'
    Image.new('RGB', (240, 160), '#64869a').save(preview)
    plan = _style_plan()
    selected = plan['creative_style']['groups']['1']
    selected.update(status='confirmed', manual_override=True, preset_id='uuid:style-1',
                    preset_hash='hash-style-1', amount=65, selected_preview_key='a'*64)
    plan['creative_style'].update(status='confirmed', scope='group', global_selection=copy.deepcopy(selected))
    plan['creative_style']['groups']['2'] = copy.deepcopy(selected)
    for index, group in [(0, 1), (1, 2)]:
        item = dict(index=index, group_id=group, path=str(tmp_path / f'{index}.ARW'),
            crop_id='original', angle=0, confirmed=True, crop_confirmed=True,
            base={}, style_id='lightroom', style_strength=100,
            crop_candidates=[{'id':'original','bounds':dict(left=0, top=0, right=1,bottom=1)},
                             {'id':'tight','bounds':dict(left=.2,top=.1,right=.8,bottom=.9)}])
        Path(item['path']).write_bytes(b'raw-untouched')
        if index == 0: plan['items'] = [item]
        else: plan['items'].append(item)
    write_json(tmp_path/'develop.json', plan)
    return plan, preview


def test_changed_crop_invalidates_affected_and_global_previews_preserves_manual_choice(tmp_path):
    plan, preview = fixture(tmp_path)
    other = copy.deepcopy(plan['creative_style']['groups']['2'])
    plan['items'][0]['_source_preview'] = str(preview)
    update_develop_item(plan, tmp_path, 0, crop_id='tight')
    for selected in [plan['creative_style']['groups']['1'], plan['creative_style']['global_selection']]:
        assert selected['status'] == 'pending'
        assert selected['recommendation_status'] == 'stale'
        assert selected['preset_id'] == 'uuid:style-1' and selected['amount'] == 65 and selected['manual_override']
        assert 'selected_preview_key' not in selected
        assert selected['top3'][0]['render_status'] == 'stale'
        assert 'preview_key' not in selected['top3'][0]
    assert plan['creative_style']['groups']['2'] == other
    confirm_all(plan, tmp_path)
    assert plan['creative_style']['status'] == 'pending'
    assert Path(plan['items'][0]['path']).read_bytes() == b'raw-untouched'


@pytest.mark.parametrize('operation', ['same-crop', 'confirm-only'])
def test_confirmation_and_same_geometry_keep_ready_style_evidence(tmp_path, operation):
    plan, preview = fixture(tmp_path)
    before = copy.deepcopy(plan['creative_style'])
    plan['items'][0]['_source_preview'] = str(preview)
    update_develop_item(plan, tmp_path, 0, **({'crop_id':'original'} if operation=='same-crop' else {'confirmed':True}))
    assert plan['creative_style'] == before


def test_skip_restores_zero_rotation_and_invalidates_changed_group(tmp_path):
    plan, preview = fixture(tmp_path)
    plan['items'][0].update(crop_id='tight', angle=3.5)
    skip_all_crops(plan, tmp_path, {0: preview, 1: preview})
    assert plan['items'][0]['angle'] == 0
    assert xmp_settings_for_recipe(plan['items'][0])['CropAngle'] == 0
    assert plan['creative_style']['groups']['1']['recommendation_status'] == 'stale'
    assert plan['creative_style']['groups']['2']['recommendation_status'] == 'complete'


def test_return_to_original_plan_keeps_choice_but_not_old_cropped_preview(tmp_path):
    plan, preview = fixture(tmp_path)
    plan['items'][0]['crop_id'] = 'tight'
    payload = {'run_id':plan['run_id'], 'results':[
        {'path':item['path'], 'preview':str(preview), 'rating':4, 'group_id':item['group_id']} for item in plan['items']]}
    restored = create_original_develop_plan(payload, tmp_path, 1, plan)
    assert restored['creative_style']['groups']['1']['recommendation_status'] == 'stale'
    assert restored['creative_style']['groups']['2']['recommendation_status'] == 'complete'
    assert restored['creative_style']['groups']['1']['preset_id'] == 'uuid:style-1'


def test_manual_natural_remains_confirmed_when_cropping_changes(tmp_path):
    plan, preview = fixture(tmp_path)
    plan['creative_style']['groups']['1'].update(status='skipped', preset_id=None, manual_override=True)
    plan['items'][0]['_source_preview'] = str(preview)
    update_develop_item(plan, tmp_path, 0, crop_id='tight')
    assert plan['creative_style']['groups']['1']['status'] == 'skipped'
    assert plan['creative_style']['status'] == 'confirmed'
