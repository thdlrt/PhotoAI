import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const elements=new Map(), timers=[];
const element=key=>{
  if(!elements.has(key))elements.set(key,{value:'',textContent:'',innerHTML:'',style:{},
    classList:{add(){},remove(){},toggle(){},contains(){return false;}},addEventListener(){},setAttribute(){},replaceChildren(){}});
  return elements.get(key);
};
const context=vm.createContext({console,document:{querySelector:element,querySelectorAll:()=>[],addEventListener(){}},
  location:{hash:'#review/A'},setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},
  clearTimeout:id=>{if(timers[id-1])timers[id-1].cancelled=true;},setInterval(){throw new Error('Overlapping interval poll is forbidden');},clearInterval(){}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const run=code=>vm.runInContext(code,context);
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
const tick=()=>{const timer=timers.find(t=>!t.cancelled&&!t.fired);assert.ok(timer,'expected scheduled poll');timer.fired=true;return timer.fn();};
run(`renderDevelop=()=>{};renderJobBar=()=>{};renderWorkflow=()=>{};syncDevelopSummary=()=>{};pollJobs=()=>{};
  requireModelSetupUi=async()=>true;setView=(name,route)=>{location.hash='#'+route;};
  const plan=id=>({exists:true,plan_id:id,revision:0,stale:false,eligible_count:1,confirmed_count:0,items:[],
    crop:{status:'pending'},basic_color:{status:'pending'},creative_style:{status:'pending'}});
  const reset=()=>{state.currentRun={run_id:'A',review_revision:0,candidate_count:1,input_root:'C:/QA/A'};
    state.activeJob=null;state.developBusy=false;state.developPlan=null;state.developProgress=null;location.hash='#review/A';};`);

// A lost POST response reads a new completed job and its plan, without a second POST.
run(`reset();let posts=0,reads=0;api=async(path,options)=>{
  if(options?.method){posts++;throw new Error('lost response');}
  if(path.endsWith('/progress'))return ++reads===1?{status:'idle',total:1}:{status:'completed',job_id:'new-job',completed:1,total:1};
  return plan('recovered');};`);
await run('loadDevelop(true)');
assert.equal(run('posts'),1);assert.equal(run('state.developPlan.plan_id'),'recovered');
assert.equal(run('state.developProgress'),null);

// A running accepted job is observed until it completes, without resubmission.
run(`reset();posts=0;reads=0;api=async(path,options)=>{
  if(options?.method){posts++;throw new Error('lost response');}
  if(path.endsWith('/progress')){reads++;return reads===1?{status:'idle',total:1}:
    {status:reads===2?'running':'completed',job_id:'new-running',completed:reads===2?0:1,total:1};}
  return plan('background-finished');};`);
const waiting=run('loadDevelop(true)');await flush();
assert.equal(run('state.developBusy'),true);await tick();await waiting;
assert.equal(run('posts'),1);assert.equal(run('state.developPlan.plan_id'),'background-finished');

// The previous completed task cannot prove a new request was accepted.
run(`reset();posts=0;api=async(path,options)=>{
  if(options?.method){posts++;throw new Error('request not accepted');}
  if(path.endsWith('/progress'))return {status:'completed',job_id:'old-job',total:1,completed:1};
  throw new Error('must not adopt old plan');};`);
await assert.rejects(run('loadDevelop(true)'),/request not accepted/);
assert.equal(run('posts'),1);

// Slow progress reads never overlap, and late responses cannot replace the completed view.
run(`reset();let finishPost,finishProgress;reads=0;api=(path,options)=>{
  if(options?.method)return new Promise(resolve=>finishPost=resolve);
  if(path.endsWith('/progress'))return ++reads===1?Promise.resolve({status:'idle',total:1}):new Promise(resolve=>finishProgress=resolve);
  throw new Error('unexpected');};`);
const loading=run('loadDevelop(true)');await flush();const slow=tick();await flush();
assert.equal(timers.filter(t=>!t.fired&&!t.cancelled).length,0);
run(`finishPost(plan('completed-before-late-poll'));`);await loading;
run(`finishProgress({status:'running',overall_percent:2});`);await slow;await flush();
assert.equal(run('reads'),2);assert.equal(run('state.developProgress'),null);
assert.equal(run('state.developPlan.plan_id'),'completed-before-late-poll');

// A newer load of the same project owns its plan and busy state.
run(`reset();let oldRead;api=()=>new Promise(resolve=>oldRead=resolve);`);
const oldLoad=run('loadDevelop(false)');await flush();
run(`api=async()=>plan('newer-view');`);await run('loadDevelop(false)');
run(`state.developBusy=true;oldRead(plan('outdated-view'));`);await oldLoad;
assert.equal(run('state.developPlan.plan_id'),'newer-view');assert.equal(run('state.developBusy'),true);
run(`renderDevelopGeneration({status:'interrupted',analyzed:2,completed:1,total:3});`);
assert.match(elements.get('#develop-grid').innerHTML,/已保留 2 张构图分析/);
assert.match(elements.get('#develop-grid').innerHTML,/1 张只需补生成预览/);
console.log('Develop recovery, serial polling, retained analysis and ownership PASS');
