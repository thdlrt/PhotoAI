import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const elements = new Map();
function element(key) {
  if (!elements.has(key)) elements.set(key, {
    style: {}, classList: {add() {}, remove() {}, toggle() {}, contains() {return false;}},
    textContent: "", innerHTML: "", disabled: false, addEventListener() {}, replaceChildren() {},
    setAttribute() {}, removeAttribute() {},
    showModal() {this.open = true;}, close() {this.open = false;},
  });
  return elements.get(key);
}
const context = vm.createContext({document: {querySelector: element, addEventListener() {}},
  console, setTimeout, clearTimeout, setInterval, clearInterval, location: {hash: "#review"}});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const run = code => vm.runInContext(code, context);
const realSetView = run('setView');
run(`state.currentRun = {run_id:"visible", input_root:"C:/photos/p3", review_revision:0,
  results:[{index:0, filename:"own.ARW"}]}; state.activeJob = {id:"other",kind:"develop",status:"running",
  context:{run_id:"other-run",input_root:"C:/photos/p1"}};`);
assert.equal(run("jobBlocksProject()"), false);
run("openDeletePhotos([0])");
assert.equal(element("#delete-photos-dialog").open, true);
assert.equal(run("Boolean(canDragGroups())"), true);
for (const root of ["c:\\PHOTOS\\p3\\", "C:/photos", "C:/photos/p3/child"]) {
  context.folder = root;
  run("state.activeJob.context.input_root = folder");
  assert.equal(run("jobBlocksProject()"), true);
}
run("state.activeJob.context = {run_id:'visible'}");
assert.equal(run("jobBlocksProject()"), true);
run("state.activeJob.context = {run_id:'legacy-other'}");
assert.equal(run("jobBlocksProject()"), true);
run("state.activeJob.status = 'cancelled'");
assert.equal(run("jobBlocksProject()"), false);
run(`state.activeJob = null; state.developProgress = {status:'running', job_id:'crop-job', completed:3, total:40, run_id:'visible'};
  renderModelInstallProgress = () => {}; modelInstallProgressSnapshot = () => null;
  renderReview = () => {}; renderToolbox = () => {}; renderDevelop = () => {}; pollJobs = () => {};
`);
run("renderJobBar()");
assert.equal(element("#job-cancel").textContent, "取消");
assert.equal(element("#job-cancel").disabled, false);
const requests = [];
context.record = (path, method) => requests.push([path, method]);
run(`api = async (path, options) => {record(path, options.method);
  return {id:'crop-job',kind:'develop',status:'cancelled',stage:'已取消',context:{run_id:'visible'}};};`);
await run("cancelActiveJob()");
assert.deepEqual(requests, [["/api/jobs/crop-job", "DELETE"]]);
assert.equal(run("state.developProgress.status"), "cancelled");
assert.equal(run("state.developProgress.completed"), 3);
assert.equal(element("#job-retry").textContent, "继续构图");
assert.equal(element("#job-cancel").textContent, "关闭");
run("state.developProgress = null; state.developFlowProgress = {status:'running'}; renderJobBar()");
assert.equal(element("#job-cancel").disabled, true); // Local setting saves have no cancellable worker.

// The long-running foreground HTTP request also reacts to expected cancellation.
let rejectWorker;
let startedResolve;
const started = new Promise(resolve => { startedResolve = resolve; });
context.apiMock = (path, options) => {
  if (options?.method === "POST") {
    startedResolve();
    return new Promise((_resolve, reject) => {rejectWorker = reject;});
  }
  if (options?.method === "DELETE") {
    rejectWorker(new Error("智能构图已取消；已有缓存会在下次继续复用。"));
    return {id:"foreground-job", kind:"develop", status:"cancelled", context:{run_id:"visible"}};
  }
  return {status:"cancelled", completed:3, total:40, run_id:"visible"};
};
run("api = async (...args) => apiMock(...args); requireModelSetupUi = async () => true; setView = () => {}; state.developFlowProgress = null;");
const generation = run("loadDevelop(true)");
await started;
run("state.developProgress.job_id = 'foreground-job'; state.developProgress.status = 'running'");
await run("cancelActiveJob()");
await generation;
assert.equal(run("state.developBusy"), false);
assert.equal(run("state.developProgress.status"), "cancelled");
assert.equal(run("state.developProgress.completed"), 3);
console.log("task controls: foreground cancellation, retained progress and project scope passed");

// Delayed responses must never replace a different project's plan or busy state.
run(`state.activeJob = null; state.currentRun = {run_id:'old',review_revision:0};
  state.developPlan = {exists:true,revision:0}; state.developProgress = null;
  let releaseDelayed; api = () => new Promise(resolve=>{releaseDelayed=resolve;});`);
let delayed = run('loadDevelop(false)');
await new Promise(resolve=>setTimeout(resolve,0));
run(`state.currentRun = {run_id:'new'}; state.developPlan = {plan_id:'new-plan'};
  state.developBusy = true; releaseDelayed({exists:true,plan_id:'old-plan'});`);
await delayed;
assert.equal(run('state.developPlan.plan_id'),'new-plan');
assert.equal(run('state.developBusy'),true);
run(`state.developBusy=false; state.developPlan={exists:true,revision:0};`);
delayed = run('updateDevelop(0,{crop_id:"tight"})');
await new Promise(resolve=>setTimeout(resolve,0));
run(`state.currentRun = {run_id:'third'}; state.developPlan = {plan_id:'third-plan'};
  state.developBusy=true; releaseDelayed({exists:true,plan_id:'new-plan'});`);
await delayed;
assert.equal(run('state.developPlan.plan_id'),'third-plan');
assert.equal(run('state.developBusy'),true);
console.log('project navigation: delayed crop loads and saves cannot contaminate another project');

for (const operation of ['confirmAllDevelop()', 'skipCropDevelop()', 'setColorMode("skip")', 'advanceFromCrop()']) {
  run(`state.currentRun={run_id:'pending',review_revision:0}; state.developBusy=false;
    state.developPlan={exists:true,revision:0}; state.developFlowProgress=null;
    api=()=>new Promise(resolve=>{releaseDelayed=resolve;});`);
  delayed=run(operation);
  await new Promise(resolve=>setTimeout(resolve,0));
  run(`state.currentRun={run_id:'viewed'}; state.developPlan={plan_id:'viewed-plan'};
    state.developBusy=true; state.developFlowProgress={run_id:'viewed'};
    releaseDelayed({exists:true,revision:1,plan_id:'pending-plan'});`);
  await delayed;
  assert.equal(run('state.developPlan.plan_id'),'viewed-plan',operation);
  assert.equal(run('state.developBusy'),true,operation);
  assert.equal(run('state.developFlowProgress.run_id'),'viewed',operation);
}

// Latest navigation wins even if earlier project loading takes longer.
run(`document.querySelectorAll=()=>[]; clearGroupingSelection=()=>{}; state.projects=[];
  $('#export-directory').value='C:/previous-project/output';
  let openResponses={}; api=path=>new Promise(resolve=>{openResponses[path]=resolve;});`);
const firstOpen=run('openRun("slow")');
await new Promise(resolve=>setTimeout(resolve,0));
const secondOpen=run('openRun("fast")');
await new Promise(resolve=>setTimeout(resolve,0));
run(`openResponses['/api/runs/fast']({run_id:'fast',results:[]});`);
await secondOpen;
run(`openResponses['/api/runs/slow']({run_id:'slow',results:[]});`);
await firstOpen;
assert.equal(run('state.currentRun.run_id'),'fast');
assert.equal(run('state.developBusy'),false);
assert.equal(run('state.developProgress'),null);
assert.equal(element('#export-directory').value,'');

// The project-list fetch precedes openRun and needs the same latest-navigation rule.
run(`openResponses={};`);
const slowProject=run('openProject("slow-project")');
await new Promise(resolve=>setTimeout(resolve,0));
const fastProject=run('openProject("fast-project")');
await new Promise(resolve=>setTimeout(resolve,0));
run(`openResponses['/api/projects/fast-project']({project_id:'fast-project',latest_run_id:'fast-run'});`);
await new Promise(resolve=>setTimeout(resolve,0));
run(`openResponses['/api/runs/fast-run']({run_id:'fast-run',results:[]});`);
await fastProject;
run(`openResponses['/api/projects/slow-project']({project_id:'slow-project',latest_run_id:'slow-run'});`);
await slowProject;
assert.equal(run('state.currentRun.run_id'),'fast-run');
assert.equal(run('state.currentProject.project_id'),'fast-project');
assert.equal(run("Boolean(openResponses['/api/runs/slow-run'])"),false);

context.actualSetView=realSetView;
context.window={scrollTo(){}};
context.history={replaceState(_state,_title,value){context.location.hash=value;}};
const leaving=run('openProject("leaving-project")');
await new Promise(resolve=>setTimeout(resolve,0));
run(`actualSetView('settings'); openResponses['/api/projects/leaving-project']({project_id:'leaving-project',latest_run_id:'leaving-run'});`);
await leaving;
assert.equal(context.location.hash,'#settings');
assert.equal(run('state.currentRun.run_id'),'fast-run');
