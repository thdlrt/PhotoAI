// Run only against output/refactor-export-review-server.py. Does not execute exports or launch Lightroom.
async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  return await page.evaluate(async()=>{
    const runs=['20261010-001900-123456','20261010-002000-123456'];
    const assert=(condition,message)=>{if(!condition)throw new Error(message);};
    for(const id of runs){
      await openRun(id);
      assert(state.currentRun.input_root.includes('export-review-'),'Requires isolated fixture');
      let plan=await api(`/api/runs/${id}/develop/skip-analysis`,{method:'POST',body:{base_revision:0}});
      plan=await api(`/api/runs/${id}/develop/options`,{method:'POST',body:{base_revision:plan.revision,mode:'skip'}});
    }
    await openRun(runs[0]);
    await loadExport();
    const directoryA=state.currentRun.input_root+'/custom-output';
    const frozen=await api(`/api/runs/${runs[0]}/exports/prepare`,{method:'POST',body:{
      base_revision:0,develop_revision:state.developPlan.revision,xmp:false,jpeg:true,jpeg_output_dir:directoryA}});
    await openRun(runs[1]);
    assert(document.querySelector('#export-directory').value==='','Previous project folder must clear');
    await loadExport();
    const directoryB=document.querySelector('#export-directory').value;
    assert(directoryB.includes('工程B') && !directoryB.includes('custom-output'),'B must use its own default folder');
    assert(state.exportSpec===null,'B must not receive A frozen export');
    await openRun(runs[0]);
    await loadExport();
    assert(state.exportSpec.export_spec_id===frozen.export_spec_id,'A export should restore after returning');
    assert(state.exportTargets.jpeg && !state.exportTargets.xmp,'Restore A frozen targets');
    assert(document.querySelector('#export-directory').value.replaceAll('\\','/').endsWith('/工程A/custom-output'),'Restore A custom folder');
    const count=(await api('/api/jobs')).length;
    assert(count===0,'This validation must not launch Lightroom or any export worker');
    return {status:'passed',A_restored:state.exportSpec.export_spec_id,B_default_directory:directoryB,jobs:count,
      targets:state.exportTargets,scope:'real Service APIs and browser routing; preparation only; no Lightroom execution'};
  });
}
