async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(()=>Boolean(state.token&&state.bootstrap));
  await page.evaluate(async()=>{
    await openRun('20261010-001900-123456');
    if(!state.currentRun.input_root.includes('develop-recovery-'))throw new Error('QA fixture required');
    requireModelSetupUi=async()=>true;
  });
  let posts=0, progressReads=0;
  await page.route('**/api/runs/*/develop/progress',async route=>{
    progressReads++;
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(progressReads===1?
      {status:'idle',completed:0,analyzed:0,total:1}:
      {status:'completed',job_id:'qa-accepted-generation',completed:1,analyzed:1,total:1,overall_percent:100})});
  });
  await page.route('**/api/runs/*/develop',async route=>{
    if(route.request().method()!=='POST')return route.continue();
    posts++;
    const token=await page.evaluate(()=>state.token);
    // Commit an actual no-AI plan in the isolated Service, then lose the original response.
    const saved=await page.request.post(route.request().url().replace(/\/develop$/,'/develop/skip-analysis'),
      {headers:{'X-Photo-AI-Token':token},data:{base_revision:0}});
    if(!saved.ok())throw new Error('QA plan commit failed: '+await saved.text());
    await route.abort('failed');
  });
  await page.locator('#develop-open').click();
  await page.waitForFunction(()=>Boolean(state.developPlan?.exists&&!state.developPlan?.generating&&!state.developBusy));
  const stateAfter=await page.evaluate(()=>({planId:state.developPlan.plan_id,items:state.developPlan.items.length,
    progress:state.developProgress,route:location.hash}));
  if(posts!==1||stateAfter.items!==1||stateAfter.progress!==null)throw new Error('Completed plan was not recovered without resubmission');
  await page.screenshot({path:'output/playwright/refactor-round13-develop-recovery.png'});
  return {status:'passed',posts,progressReads,stateAfter,limit:'Actual browser and Service read-back; original-frame plan committed in QA; model readiness and job progress injected; no AI inference'};
}
