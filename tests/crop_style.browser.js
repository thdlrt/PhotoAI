async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(()=>Boolean(state.token && state.bootstrap));
  await page.evaluate(async()=>{
    await openRun('20261010-001900-123456');
    if(!state.currentRun.input_root.includes('export-review-'))throw new Error('QA fixture required');
    await goWorkflowStage('crop');
  });
  await page.locator('[data-develop-crop-choice="0"][data-crop-id="tight"]').click();
  await page.waitForFunction(()=>!state.developBusy && state.developPlan.items[0].crop_id==='tight');
  const invalidated=await page.evaluate(()=>{
    const selection=state.developPlan.creative_style.global_selection;
    if(selection.recommendation_status!=='stale'||selection.preset_id!=='qa-chosen'||selection.amount!==65)throw new Error('Stale evidence or manual choice mismatch');
    return {status:selection.recommendation_status,preset:selection.preset_id,amount:selection.amount};
  });
  await page.locator('#develop-next').click();
  await page.waitForFunction(()=>state.developStage==='base'&&!state.developBusy);
  await page.locator('#develop-next').click();
  await page.waitForFunction(()=>state.developStage==='style'&&!state.developBusy);
  if(!await page.locator('#style-groups').innerText().then(text=>text.includes('预览已过期')&&text.includes('需要重新预览')))throw new Error('Stale previews must not look like running renders');
  if(await page.locator('#style-groups img[src*="/style-preview/"]').count())throw new Error('Old creative preview is still displayed');
  if(!await page.locator('#develop-next').isDisabled())throw new Error('Unreviewed changed crop must not export the old recommendation');
  await page.screenshot({path:'output/playwright/refactor-round8-stale-preview.png'});
  let previewRequest=null;
  await page.route('**/api/runs/*/style-preview',async route=>{
    previewRequest=route.request().postDataJSON();
    await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'QA: inference deliberately not launched'})});
  });
  await page.evaluate(()=>{window.__qaReadiness=requireModelSetupUi;requireModelSetupUi=async()=>true;});
  await page.locator('[data-style-refresh="global"]').click();
  await page.waitForFunction(()=>!state.developBusy);
  await page.evaluate(()=>{requireModelSetupUi=window.__qaReadiness;delete window.__qaReadiness;});
  if(previewRequest?.preset_id!=='qa-chosen'||previewRequest?.amount!==65)throw new Error('Refresh must reuse exact manual choice');
  let dropped=0;
  await page.route('**/api/runs/*/develop/style/global',async route=>{
    if(route.request().method()==='PUT'&&dropped===0){
      const response=await route.fetch();if(response.status()!==200)throw new Error('Commit failed before drop');
      dropped++;await route.abort('failed');
    }else await route.continue();
  });
  await page.locator('[data-style-choice][data-choice-id="natural"]').click();
  await page.waitForFunction(()=>!state.developBusy && state.developPlan.creative_style.global_selection.status==='skipped');
  if(dropped!==1)throw new Error('Did not exercise committed-but-lost-response case');
  if(!await page.locator('#toast').innerText().then(text=>text.includes('已读取后台保存')))throw new Error('Committed choice was incorrectly reported as failed');
  await page.locator('#develop-apply-style').click();
  await page.waitForFunction(()=>!state.developBusy && currentStyleComplete());
  await page.locator('#develop-next').click();
  await page.waitForFunction(()=>location.hash.startsWith('#export/'));
  const final=await page.evaluate(async()=>{
    const spec=await api(`/api/runs/${state.currentRun.run_id}/exports/prepare`,{method:'POST',body:{base_revision:0,
      develop_revision:state.developPlan.revision,xmp:true,jpeg:false}});
    if((await api('/api/jobs')).length)throw new Error('QA must not start inference or Lightroom');
    return {spec:spec.export_spec_id,crop:state.developPlan.items[0].crop_id,natural:state.developPlan.creative_style.global_selection.status};
  });
  return {status:'passed',invalidated,previewRequest,droppedResponses:dropped,final,limitations:'synthetic photos and existing-look fixture; actual Service edits; AI readiness stubbed for exact-preview request; inference request intercepted; no Lightroom execution'};
}
