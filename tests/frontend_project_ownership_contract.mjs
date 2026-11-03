import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const nodes=new Map();
const element=key=>{
  if(!nodes.has(key)) nodes.set(key,{value:'',classList:{contains(){return false;},add(){},remove(){},toggle(){}},
    addEventListener(){},textContent:'',innerHTML:'',setAttribute(){},style:{},close(){this.open=false;}});
  return nodes.get(key);
};
const timers=[];
const context=vm.createContext({document:{querySelector:element,querySelectorAll:()=>[],addEventListener(){}},
  location:{hash:'#export/A'},console,setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},
  clearTimeout(){},setInterval(){},clearInterval(){}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const run=code=>vm.runInContext(code,context);
const realPoll=run('pollJobs');
const calls=[],messages=[],jobs=[],opened=[];
context.record=(path,options)=>calls.push({path,...options});
context.message=value=>messages.push(value);
context.activated=job=>jobs.push(job);
context.opened=id=>opened.push(id);
run(`renderReview=()=>{}; renderDevelop=()=>{}; renderExport=()=>{}; renderJobBar=()=>{};
  renderProjects=()=>{}; renderProjectStart=()=>{}; renderToolbox=()=>{}; syncDevelopSummary=()=>{};
  refreshBootstrap=async()=>{}; toast=message; pollJobs=()=>{}; activateJob=activated;
  requireModelSetupUi=async()=>true; setView=()=>{};
  const makeRun=id=>({run_id:id,input_root:'C:/photos/'+id,review_revision:2,xmp_ready:true,results:[]});
  const makePlan=id=>({exists:true,revision:3,plan_id:id,eligible_count:1,confirmed_count:1,
    source_review_revision:2,crop:{status:'confirmed'},basic_color:{status:'skipped'},creative_style:{status:'skipped'}});
  state.currentRun=makeRun('A'); state.developPlan=makePlan('plan-A');
  state.exportTargets={xmp:true,jpeg:true}; $('#export-directory').value='C:/out/A';
  let releasePrepare; api=async(path,options)=>{record(path,options);
    if(path.endsWith('/prepare')) return new Promise(resolve=>{releasePrepare=resolve;});
    return {id:'job-A',kind:'lightroom_apply',status:'queued',context:{run_id:'A'}};};`);
const execution=run('executeExport()');
await run('executeExport()');
assert.equal(calls.length,1,'Double click must not create a second frozen export');
run(`state.currentRun=makeRun('B'); state.developPlan=makePlan('plan-B'); state.exportSpec={marker:'B'};
  $('#export-directory').value='C:/out/B'; releasePrepare({run_id:'A',export_spec_id:'spec-A',status:'prepared'});`);
await execution;
assert.equal(calls[0].path,'/api/runs/A/exports/prepare');
assert.equal(calls[0].body.jpeg_output_dir,'C:/out/A');
assert.equal(calls[1].path,'/api/exports/spec-A/execute');
assert.equal(run('state.exportSpec.marker'),'B');
assert.equal(jobs[0].context.run_id,'A');
assert.equal(run('exportStarting'),false);

run(`state.currentRun=makeRun('A'); state.developPlan=makePlan('plan-A');
  $('#export-directory').value='c:/OUT/a/';
  let failedSpec={run_id:'A',export_spec_id:'spec-A',status:'partial',review_revision:2,
    develop_revision:3,develop_plan_id:'plan-A',targets:{xmp:true,jpeg:true},output_dir:'C:/out/A'};`);
assert.equal(run('canRetryExport(failedSpec)'),true);
for(const change of ["review_revision:1","develop_revision:2","develop_plan_id:'old'","output_dir:'C:/elsewhere'","targets:{xmp:true,jpeg:false}"])
  assert.equal(run(`canRetryExport({...failedSpec,${change}})`),false,change);
run(`state.exportSpec=failedSpec; api=async(path,options)=>{record(path,options);
  if(options?.method==='POST') throw new Error('response lost');
  return {...failedSpec,status:'running'};};`);
await run('executeExport()');
assert.equal(run('state.exportSpec.status'),'running','Network error must not invent a worker failure');

for(const [operation,suffix,method] of [
  ['updateStyleGroup(1,{status:"skipped"})','groups/1','PUT'],
  ['updateStyleGlobal({status:"skipped"})','global','PUT'],
  ['setStyleScope("global")','scope','PUT'],
  ['confirmRecommendedStyles()','confirm-all','POST']]) {
  run(`state.currentRun=makeRun('A'); state.developPlan=makePlan('plan-A'); state.styleScope='group';
    state.developBusy=false; state.activeJob=null; var releaseStyle;
    api=(path,options)=>{record(path,options);return new Promise(resolve=>{releaseStyle=resolve;});};`);
  const pending=run(operation);
  assert.equal(calls.at(-1).path,`/api/runs/A/develop/style/${suffix}`);
  assert.equal(calls.at(-1).method,method);
  assert.equal(calls.at(-1).body.base_revision,3);
  run(`state.currentRun=makeRun('B'); state.developPlan=makePlan('plan-B'); state.developBusy=true;
    releaseStyle(makePlan('late-plan-A'));`);
  await pending;
  assert.equal(run('state.developPlan.plan_id'),'plan-B',operation);
  assert.equal(run('state.developBusy'),true,operation);
}
run(`state.currentRun=makeRun('A'); state.developBusy=false; let releaseRead;
  api=()=>new Promise(resolve=>{releaseRead=resolve;});`);
const reload=run(`reloadDevelopAfterStyleJob({kind:'style_preview',context:{run_id:'A'}})`);
run(`state.currentRun=makeRun('B'); state.developPlan=makePlan('plan-B'); releaseRead(makePlan('late-plan-A'));`);
assert.equal(await reload,false);
assert.equal(run('state.developPlan.plan_id'),'plan-B');

calls.length=0;
run(`state.currentRun=makeRun('A'); state.developPlan=makePlan('plan-A'); state.developBusy=false;
  let releaseReadiness; requireModelSetupUi=()=>new Promise(resolve=>{releaseReadiness=resolve;});
  api=async(path,options)=>{record(path,options);};`);
const recommend=run('recommendStyles()');
run(`state.currentRun=makeRun('B'); releaseReadiness(true);`);
await recommend;
assert.equal(calls.length,0,'A delayed readiness check must not launch a task on B');

// Returning to a project restores only that project's frozen export and folder.
run(`state.currentRun=makeRun('A'); state.developPlan=makePlan('plan-A'); state.exportSpec=null;
  api=async()=>({...failedSpec,status:'completed'});`);
await run('loadExport()');
assert.equal(run('state.exportSpec.export_spec_id'),'spec-A');
assert.equal(element('#export-directory').value,'C:/out/A');
run(`state.currentRun=makeRun('A'); state.exportSpec=null;
  api=()=>new Promise(resolve=>{releaseRead=resolve;});`);
const loading=run('loadExport()');
await new Promise(resolve=>setTimeout(resolve,0));
run(`state.currentRun=makeRun('B'); state.exportSpec={marker:'B'}; releaseRead(failedSpec);`);
await loading;
assert.equal(run('state.exportSpec.marker'),'B');

context.realPoll=realPoll;
run(`pollJobs=realPoll; state.currentRun=makeRun('B'); state.developBusy=false; state.handledJobs=new Set();
  location.hash='#export/B'; openRun=async id=>{opened(id);return state.currentRun;};
  const foreignJobs=[
    {id:'group-A',kind:'group',status:'completed',context:{source_run_id:'A'},result:{run_id:'new-A'}},
    {id:'export-A',kind:'lightroom_apply',status:'completed',context:{run_id:'A',export_spec_id:'spec-A'}},
    {id:'failed-A',kind:'lightroom_apply',status:'failed',context:{run_id:'A',export_spec_id:'spec-A'}}];
  api=async path=>{record(path);if(path==='/api/jobs')return foreignJobs;throw new Error('Unexpected foreign result read');};`);
calls.length=0;
await run('pollJobs()');
assert.equal(opened.length,0,'Foreign completion must not navigate away from B');
assert.equal(calls.some(call=>call.path.startsWith('/api/exports/')),false,'Foreign export must not overwrite B export state');
assert.equal(run('state.exportSpec.marker'),'B');

run(`state.handledJobs=new Set(); location.hash='#project/B';
  api=async()=>[{id:'own',kind:'score',status:'completed',context:{source_run_id:'B'},result:{run_id:'new-B'}}];`);
await run('pollJobs()');
assert.equal(opened.at(-1),'new-B','Own completed selection should still open in its project view');
const openCount=opened.length;
run(`state.handledJobs=new Set(); location.hash='#settings';`);
await run('pollJobs()');
assert.equal(opened.length,openCount,'Background completion must not interrupt settings');

calls.length=0;
timers.length=0;
run(`api=path=>{record(path);return new Promise(resolve=>{releaseRead=resolve;});};`);
const poll=run('pollJobs()');
await run('pollJobs()');
assert.equal(calls.length,1,'Poll calls must coalesce');
run('releaseRead([])');
await poll;
assert.ok(timers.some(timer=>timer.delay===0),'A poll requested during a fetch must run afterwards');
console.log('project ownership: style responses, export snapshots/retries, latest records, navigation and polling passed');

// A manual natural choice must beat a stale AI preset recommendation.
run(`state.currentRun=makeRun('A'); state.developPlan={...makePlan('plan-A'),color_mode:'style',
  creative_style:{scope:'global',status:'pending',groups:{},global_selection:{status:'skipped',manual_override:true,
    recommendation_status:'complete',recommended_kind:'preset',recommended_preset_id:'old-ai',
    top3:[{preset_id:'old-ai',preset_hash:'old-hash',amount:100}]}}};
  state.styleScope='global'; state.developBusy=false; state.activeJob=null;
  requestStylePreview=async()=>{throw new Error('Must not overwrite manual choice with AI preview');};
  api=async(path,options)=>{record(path,options);return state.developPlan;};`);
await run('confirmRecommendedStyles()');
assert.equal(calls.at(-1).path,'/api/runs/A/develop/style/confirm-all');
run(`state.developPlan=makePlan('plan-A'); state.developBusy=false;`);
await run('setColorMode("auto")');
assert.equal(calls.at(-1).body.stage,'basic');
await run('setColorMode("style")');
assert.equal(calls.at(-1).body.stage,'creative');


// Scope changes are authoritative only after a successful persisted response.
run(`state.currentRun=makeRun('A'); state.developPlan={...makePlan('plan-A'),
  creative_style:{scope:'global',status:'confirmed',groups:{},global_selection:{status:'skipped',manual_override:true}}};
  state.styleScope='global'; state.developBusy=false; state.activeJob=null;
  api=async(path,options)=>{record(path,options);throw new Error('scope write failed');};`);
assert.equal(await run('setStyleScope("group")'),null);
assert.equal(run('state.developPlan.creative_style.scope'),'global');
assert.equal(run('state.styleScope'),'global');
run(`api=async(path,options)=>{record(path,options);return {...state.developPlan,revision:4,
  creative_style:{...state.developPlan.creative_style,scope:'group',status:'pending'}};};`);
await run('setStyleScope("group")');
assert.equal(calls.at(-1).path,'/api/runs/A/develop/style/scope');
assert.equal(calls.at(-1).body.scope,'group');
assert.equal(run('state.developPlan.revision'),4);
assert.equal(run('state.styleScope'),'group');

// A dropped response after a committed write is reconciled, never resent.
for (const [path,changes,creative] of [
  ['scope',{scope:'group'},{scope:'group',status:'pending',groups:{}}],
  ['global',{status:'skipped',preset_id:null},{scope:'global',status:'pending',groups:{},global_selection:{status:'skipped',preset_id:null}}],
  ['groups/1',{status:'skipped'},{scope:'group',status:'confirmed',groups:{'1':{status:'skipped'}}}],
  ['confirm-all',{scope:'global'},{scope:'global',status:'confirmed',global_selection:{status:'skipped'}}],
]) {
  calls.length=0;
  context.recoveredCreative=creative;
  run(`state.currentRun=makeRun('A'); state.developPlan=makePlan('plan-A'); state.developBusy=false;
    api=async(path,options)=>{record(path,options);if(options?.method)throw new Error('lost response');
      return {...makePlan('plan-A'),revision:4,creative_style:recoveredCreative};};`);
  const actual=await run(`saveStylePlan(${JSON.stringify(path)},${JSON.stringify(changes)})`);
  assert.equal(actual.revision,4,path);
  assert.equal(calls.length,2,'one mutation plus one read-back only');
  assert.equal(calls[1].path,'/api/runs/A/develop');
  assert.match(messages.at(-1),/已读取后台保存/);
}

// A newer but different result is refreshed, never claimed as our requested choice.
run(`state.developPlan=makePlan('plan-A'); state.developBusy=false;
  api=async(path,options)=>{if(options?.method)throw new Error('conflict');return {...makePlan('plan-A'),revision:5,
    creative_style:{scope:'group',status:'pending',groups:{}}};};`);
assert.equal(await run('setStyleScope("global")'),null);
assert.equal(run('state.developPlan.revision'),5);
assert.equal(run('state.styleScope'),'group');
assert.equal(messages.at(-1),'conflict');

// Reconciliation arriving after navigation must leave the new project untouched.
run(`state.currentRun=makeRun('A'); state.developPlan=makePlan('plan-A'); state.developBusy=false;
  api=(path,options)=>{if(options?.method)return Promise.reject(new Error('lost response'));
    return new Promise(resolve=>{releaseRead=resolve;});};`);
const recovery=run('setStyleScope("global")');
await Promise.resolve();await Promise.resolve();
run(`state.currentRun=makeRun('B'); state.developPlan=makePlan('plan-B'); state.developBusy=true;
  releaseRead({...makePlan('plan-A'),revision:4,creative_style:{scope:'global',status:'confirmed'}});`);
await recovery;
assert.equal(run('state.developPlan.plan_id'),'plan-B');
assert.equal(run('state.developBusy'),true);

// Exact manual preset/strength is used for a stale-preview refresh, not old AI advice.
run(`state.currentRun=makeRun('A');state.developBusy=false;state.developPlan={...makePlan('plan-A'),creative_style:{scope:'global',
  global_selection:{status:'pending',recommendation_status:'stale',preset_id:'my-look',preset_hash:'my-hash',amount:65,
    recommended_preset_id:'different-ai-look'}}};
  requestStylePreview=async(group,options)=>{record('refresh-preview',{group,...options});return true;};`);
assert.equal(await run('refreshStyleSelection(null,"global")'),true);
assert.equal(calls.at(-1).presetId,'my-look');
assert.equal(calls.at(-1).amount,65);

// Exact preview asks only about its runtime, even when every AI profile is unavailable.
run(`state.currentRun=makeRun('A');state.developBusy=false;state.activeJob=null;
  state.modelResources={profiles:[{configured:true,ready:false,verified_count:0,model_count:7}]};
  api=async(path)=>{record(path);if(path==='/api/style-runtime')return {ready:true,managed:true};throw new Error('Unexpected model request');};`);
assert.equal(await run('requireStyleRuntimeUi()'),true);
assert.equal(calls.at(-1).path,'/api/style-runtime');

run(`let releaseRuntime;api=()=>new Promise(resolve=>{releaseRuntime=resolve;});`);
const runtimePending=run('requireStyleRuntimeUi()');
run(`state.currentRun=makeRun('B');releaseRuntime({ready:false,managed:true,message:'missing component'});`);
assert.equal(await runtimePending,false,'Late runtime check must not navigate B to resource setup');

// Retrying a notice for A must not replace B's export panel when the request returns.
run(`state.currentRun=makeRun('A');state.activeJob=null;state.jobRetryBusy=false;
  renderModelInstallProgress=()=>{}; reclassificationRunId=()=>null;
  const failedExport={id:'failed-A',kind:'lut_export',retryable:true,retry_mode:'export_failed',context:{export_spec_id:'export-A'}};
  let releaseExportRetry;api=async(path,options)=>{record(path,options);
    if(options?.method)return new Promise(resolve=>{releaseExportRetry=resolve;});
    return {export_spec_id:'export-A',run_id:'A'};};`);
const retryExport=run('retryFailedJob(failedExport)');
await Promise.resolve();await Promise.resolve();
run(`state.currentRun=makeRun('B');state.exportSpec={export_spec_id:'export-B',run_id:'B'};
  releaseExportRetry({id:'retry-A',kind:'lut_export',status:'queued',context:{run_id:'A'}});`);
await retryExport;
assert.equal(run('state.exportSpec.export_spec_id'),'export-B');
assert.equal(jobs.at(-1).id,'retry-A');
