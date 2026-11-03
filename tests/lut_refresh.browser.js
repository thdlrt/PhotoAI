async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(()=>Boolean(state.token&&state.bootstrap));
  await page.evaluate(async()=>{
    await openRun('20261010-001900-123456');
    if(!state.currentRun.input_root.includes('lut-refresh-'))throw new Error('Isolated QA fixture required');
    await goWorkflowStage('style');
  });
  const requests=[];
  await page.unroute('**/api/style-runtime');
  await page.route('**/api/style-runtime',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ready:true,managed:false})}));
  await page.unroute('**/api/runs/*/style-preview');
  await page.route('**/api/runs/*/style-preview',async route=>{
    requests.push(route.request().postDataJSON());
    await route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:'QA: no Lightroom launch'})});
  });
  const first=page.waitForResponse(response=>response.url().endsWith('/style-preview'));
  await page.getByRole('button',{name:'重新预览所选外观'}).click();
  await first;
  await page.waitForFunction(()=>!state.developBusy);
  if(requests.length!==1||requests[0].lut_id!=='qa-lut'||requests[0].lut_hash!=='qa-lut-hash'||requests[0].strength!==65||requests[0].scope!=='group')throw new Error('Refresh did not preserve exact LUT identity/strength/scope');
  const second=page.waitForResponse(response=>response.url().endsWith('/style-preview'));
  await page.locator('[data-style-tier="50"]').click();
  await second;
  await page.waitForFunction(()=>!state.developBusy);
  if(requests.length!==2||requests[1].strength!==50||requests[1].lut_id!=='qa-lut')throw new Error('Stale LUT strength control is unavailable');
  if(await page.getByText('基础预览已过期，请重新运行 AI 推荐').count())throw new Error('Obsolete recommendation requirement');
  if(!page.url().includes('/style'))throw new Error('Unexpected redirect');
  await page.screenshot({path:'output/playwright/refactor-round10-lut-refresh.png'});
  return {status:'passed',requests,limit:'Actual browser and source Service; isolated fixture; runtime readiness injected and preview POST intercepted to avoid Lightroom execution'};
}
