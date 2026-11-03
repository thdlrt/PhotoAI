import copy
from pathlib import Path

import pytest

from landscape_culler.develop import merge_confirmed_develop, xmp_settings_for_recipe
from landscape_culler.lightroom_apply import LightroomApplyError
from landscape_culler.lightroom_export import export_tasks_from_spec
from landscape_culler.style_worker import StyleWorkerError, _base_hash, _lightroom_task, _group_inputs
from landscape_culler.util import read_json, write_json
from test_style_worker import _run


def tasks(run, raw, recipe):
    preview = _lightroom_task(dict(source_path=str(raw),task_id='preview',kind='neutral',amount=0),
                             recipe,run/'incoming',run.parent)
    export = export_tasks_from_spec(dict(targets=dict(xmp=False,jpeg=True),output_dir=str(run/'output'),
        items=[dict(path=str(raw),rating=4,develop={**recipe,'confirmed':True})]))[0]
    return preview, export


@pytest.mark.parametrize('base', [
    {'AutoTone':' YES ','WhiteBalance':' Auto ','LensProfileEnable':'on','AutoLateralCA':1.0},
    {'AutoTone':False,'WhiteBalance':'Auto','AutoWhiteBalance':False,'LensProfileEnable':0,'AutoLateralCA':'false'},
    {'AutoTone':True,'WhiteBalance':'As Shot','AutoWhiteBalance':'enabled','LensProfileEnable':True,'AutoLateralCA':True},
])
def test_preview_and_export_use_same_explicit_auto_controls_and_safe_settings(tmp_path, base):
    data,run,raw = _run(tmp_path)
    recipe = read_json(run/'develop.json')['items'][0]
    recipe['base'] = {**base,'Exposure2012':.4,'CropLeft':.9,'UnsafeSetting':42,'ProcessVersion':'bad'}
    recipe['angle'] = -2.75
    recipe['crop_candidates'] = [{'id':'tight','bounds':dict(left=.1,top=.2,right=.8,bottom=.9)}]
    recipe['crop_id'] = 'tight'
    before = copy.deepcopy(recipe)
    preview,export = tasks(run,raw,recipe)
    for key in ['auto_tone','auto_white_balance','lens_profile','remove_chromatic_aberration','crop','style']:
        assert getattr(preview,key) == getattr(export,key), key
    assert preview.crop == dict(left=.1,top=.2,right=.8,bottom=.9,angle=-2.75)
    assert preview.style == {'Exposure2012':.4}
    if base.get('AutoWhiteBalance') is False:
        assert preview.auto_white_balance is False
        assert 'WhiteBalance' not in xmp_settings_for_recipe(recipe)
    assert 'AutoWhiteBalance' not in xmp_settings_for_recipe(recipe)
    assert recipe == before and raw.read_bytes() == b'raw-test-data'


def test_missing_selected_crop_is_rejected_before_preview_instead_of_falling_back(tmp_path):
    data,run,raw = _run(tmp_path)
    recipe = read_json(run/'develop.json')['items'][0]
    recipe['crop_id'] = 'deleted-crop'
    with pytest.raises(StyleWorkerError,match='找不到'):
        _lightroom_task(dict(source_path=str(raw),task_id='preview'),recipe,run/'incoming',data)
    with pytest.raises(LightroomApplyError,match='找不到'):
        export_tasks_from_spec(dict(targets=dict(xmp=False,jpeg=True),output_dir=str(run/'output'),
            items=[dict(path=str(raw),rating=4,develop={**recipe,'confirmed':True})]))
    with pytest.raises(ValueError,match='找不到'):
        xmp_settings_for_recipe(recipe)


@pytest.mark.parametrize('style_id', ['golden','soft','clear','forest'])
def test_legacy_saved_style_is_kept_in_preview_lightroom_export_and_xmp(tmp_path, style_id):
    data,run,raw = _run(tmp_path)
    recipe = read_json(run/'develop.json')['items'][0]
    recipe.update(style_id=style_id,style_strength=65,base={'Contrast2012':95})
    preview,export = tasks(run,raw,recipe)
    xmp = xmp_settings_for_recipe(recipe)
    assert preview.style == export.style
    for key,value in preview.style.items():
        assert xmp[key] == value
    assert len(preview.style) > 1
    changed = copy.deepcopy(recipe)
    changed['style_strength'] = 100
    assert _base_hash(recipe) != _base_hash(changed)


def test_skip_base_does_not_reactivate_legacy_color_and_does_not_rewrite_saved_choice(tmp_path):
    data,run,raw = _run(tmp_path)
    plan = read_json(run/'develop.json')
    plan['basic_color']['status'] = 'skipped'
    plan['items'][0].update(style_id='golden',style_strength=65,confirmed=True,crop_confirmed=True)
    write_json(run/'develop.json',plan)
    before = (run/'develop.json').read_bytes()
    results = read_json(run/'results.json')
    _,_,sources = _group_inputs(plan,results,group_id=1)
    source = next(iter(sources.values()))
    preview,_ = tasks(run,raw,source)
    merged = merge_confirmed_develop(copy.deepcopy(results),plan,0)
    recipe = merged['results'][0]['develop']
    _,export = tasks(run,raw,recipe)
    assert preview.style == export.style == {}
    assert preview.auto_tone is export.auto_tone is False
    assert xmp_settings_for_recipe(recipe) == {'CropLeft':0.0,'CropTop':0.0,'CropRight':1.0,'CropBottom':1.0,'CropAngle':0.0,'HasCrop':'True'}
    assert (run/'develop.json').read_bytes() == before
    assert plan['items'][0]['style_id'] == 'golden'


@pytest.mark.parametrize('strength', [65, None])
def test_lut_strength_survives_frozen_recipe_without_legacy_amount_fallback(tmp_path, strength):
    data,run,raw = _run(tmp_path)
    plan = read_json(run/'develop.json')
    plan['items'][0].update(confirmed=True,crop_confirmed=True)
    plan['creative_style'] = dict(status='confirmed',scope='group',groups={'1':dict(status='confirmed',
        manual_override=True,look_kind='rendered_lut',lut_id='kept',lut_hash='a'*64,strength=strength,amount=100)})
    merged = merge_confirmed_develop(read_json(run/'results.json'),plan,0)
    frozen = merged['results'][0]['develop']['creative_style']
    assert frozen.get('strength', frozen['amount']) == (65 if strength is not None else 100)


def test_manual_white_balance_and_crop_precision_match_direct_xmp(tmp_path):
    data,run,raw = _run(tmp_path)
    recipe = read_json(run/'develop.json')['items'][0]
    recipe.update(base={'WhiteBalance':'Custom','Temperature':5600,'Tint':7},angle=1.257)
    recipe['crop_candidates'][0]['bounds']['left'] = .123456789
    preview,export = tasks(run,raw,recipe)
    xmp = xmp_settings_for_recipe(recipe)
    assert preview.style == export.style == {'WhiteBalance':'Custom','Temperature':5600,'Tint':7}
    assert preview.auto_white_balance is export.auto_white_balance is False
    assert preview.crop == export.crop
    assert preview.crop['left'] == xmp['CropLeft'] == .123457
    assert preview.crop['angle'] == xmp['CropAngle'] == 1.26
