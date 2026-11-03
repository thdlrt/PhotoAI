async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(()=>Boolean(state.token && state.bootstrap));
  await page.evaluate(async()=>{
    await openRun('20261010-001900-123456');
    if(!state.currentRun.input_root.includes('export-review-'))throw new Error('QA fixture required');
    await skipAutomaticCrop();
  });
  await page.locator('[data-color-mode="skip"]').click();
  await page.waitForFunction(()=>!state.developBusy && currentBaseComplete());
  await page.locator('#develop-next').click();
  await page.waitForFunction(()=>state.developStage==='style' && !state.developBusy);
  await page.locator('[data-style-choice][data-choice-id="natural"]').click();
  await page.waitForFunction(()=>!state.developBusy && state.developPlan.creative_style.global_selection?.status==='skipped');
  await page.locator('#develop-apply-style').click();
  await page.waitForFunction(()=>!state.developBusy && currentStyleComplete());
  await page.locator('.style-scope-option[data-style-scope="group"]').click();
  await page.waitForFunction(()=>!state.developBusy && currentStyleScope()==='group');
  if(!await page.locator('#develop-next').isDisabled())throw new Error('Missing group choice must not reuse global confirmation');
  await page.locator('[data-style-choice][data-choice-id="natural"]').click();
  await page.waitForFunction(()=>!state.developBusy && currentStyleComplete());
  const saved=await page.evaluate(()=>JSON.stringify(state.developPlan.creative_style.groups));
  await page.locator('.style-scope-option[data-style-scope="global"]').click();
  await page.waitForFunction(()=>!state.developBusy && currentStyleScope()==='global');
  await page.locator('#develop-apply-style').click();
  await page.waitForFunction(()=>!state.developBusy && currentStyleComplete());
  if(saved!==await page.evaluate(()=>JSON.stringify(state.developPlan.creative_style.groups)))throw new Error('Global confirmation replaced individual choices');
  await page.locator('.style-scope-option[data-style-scope="group"]').click();
  await page.waitForFunction(()=>!state.developBusy && currentStyleScope()==='group');
  await page.reload();
  await page.waitForFunction(()=>state.developStage==='style' && state.developPlan?.exists && !state.developBusy && currentStyleScope()==='group');
  if(saved!==await page.evaluate(()=>JSON.stringify(state.developPlan.creative_style.groups)))throw new Error('Group choice changed across reload');
  await page.locator('#develop-next').click();
  await page.waitForFunction(()=>location.hash.startsWith('#export/'));
  return await page.evaluate(async()=>{
    const spec=await api(`/api/runs/${state.currentRun.run_id}/exports/prepare`,{method:'POST',body:{base_revision:0,
      develop_revision:state.developPlan.revision,xmp:true,jpeg:false}});
    const jobs=await api('/api/jobs');if(jobs.length)throw new Error('No compute or export execution allowed');
    return {status:'passed',scope:currentStyleScope(),revision:state.developPlan.revision,
      global_preserved:state.developPlan.creative_style.global_selection.status==='skipped',
      groups:state.developPlan.creative_style.groups,export_spec:spec.export_spec_id,jobs:jobs.length,
      limitation:'isolated fixture; actual browser/API; export preparation only, no Lightroom execution'};
  });
}
