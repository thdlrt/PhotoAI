// Isolated Service fixture only; exercises real clicks and persistence, without AI/Lightroom.
async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  const outcomes=[];
  for (const [id,mode] of [['20261010-001900-123456','skip'],['20261010-002000-123456','auto']]) {
    await page.evaluate(async ({id})=>{
      await openRun(id);
      if (!state.currentRun.input_root.includes('export-review-')) throw new Error('Requires isolated QA data');
      await skipAutomaticCrop();
    },{id});
    await page.locator(`[data-color-mode="${mode}"]`).click();
    await page.waitForFunction(mode=>!state.developBusy && state.developPlan.basic_color.status===(mode==='auto'?'enabled':'skipped'),mode);
    await page.locator('#develop-next').click();
    await page.waitForFunction(()=>state.developStage==='style' && !state.developBusy);
    await page.locator('[data-style-choice][data-choice-id="natural"]').click();
    await page.waitForFunction(()=>!state.developBusy && state.developPlan.creative_style.global_selection?.status==='skipped');
    if (await page.locator('#develop-apply-style').innerText() !== '确认当前外观') throw new Error('Manual choice needs correct confirmation label');
    await page.locator('#develop-apply-style').click();
    await page.waitForFunction(()=>!state.developBusy && currentStyleComplete());
    await page.locator('#develop-next').click();
    await page.waitForFunction(()=>location.hash.startsWith('#export/'));
    outcomes.push(await page.evaluate(async mode=>{
      const expected=mode==='auto'?'enabled':'skipped';
      if(state.developPlan.basic_color.status!==expected)throw new Error('Base choice changed');
      const spec=await api(`/api/runs/${state.currentRun.run_id}/exports/prepare`,{method:'POST',body:{
        base_revision:state.currentRun.review_revision,develop_revision:state.developPlan.revision,xmp:true,jpeg:false}});
      if((await api('/api/jobs')).length)throw new Error('Must not start compute or export');
      return {run:state.currentRun.run_id,base:expected,creative:state.developPlan.creative_style.status,spec:spec.export_spec_id};
    },mode));
  }
  return {status:'passed',outcomes,scope:'real UI/API natural selection and export preparation; no Lightroom execution'};
}
