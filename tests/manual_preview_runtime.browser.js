async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(()=>Boolean(state.token&&state.bootstrap));
  const gates=await page.evaluate(async()=>{
    const runtime=await api('/api/style-runtime');
    const resources=await api('/api/model-resources');
    if(!runtime.ready)throw new Error('Prepared real QA worker must be ready');
    if(resources.profiles.some(p=>p.ready))throw new Error('No model profile may be ready in this fixture');
    await openRun('20261010-001900-123456');
    if(!state.currentRun.input_root.includes('ai-worker-reuse-'))throw new Error('QA runtime fixture required');
    await goWorkflowStage('style');
    return {runtime,models:resources.profiles.map(p=>({id:p.id,ready:p.ready,verified:p.verified_count}))};
  });
  let submitted=null;
  let resolveSubmission;
  const submission=new Promise(resolve=>resolveSubmission=resolve);
  await page.route('**/api/runs/*/style-preview',async route=>{
    submitted=route.request().postDataJSON();
    await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'QA: Lightroom execution deliberately not launched'})});
    resolveSubmission();
  });
  await page.locator('[data-style-tier="50"]').click();
  await submission;
  await page.waitForFunction(()=>!state.developBusy);
  if(!page.url().includes('/style'))throw new Error('Missing AI weights must not send exact preview to setup');
  if(submitted?.preset_id!=='qa-chosen'||submitted?.amount!==50)throw new Error('Exact manual parameters not submitted');
  await page.screenshot({path:'output/playwright/refactor-round9-manual-runtime.png'});
  return {status:'passed',gates,submitted,limit:'real isolated Python/wheel and Service runtime checks; historical GPU/model self-test is a fixture; missing model weights; actual preview request intercepted; no Lightroom execution'};
}
