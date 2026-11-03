import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

// Exercise the real asynchronous controller with delayed HTTP responses. The
// renderer is replaced only inside this test; browser layout is tested separately.
const controls = [{disabled: false, isConnected: true}, {disabled: true, isConnected: true}];
const modal = {open: true, showModal() { this.open = true; }, close() { this.open = false; }};
const statusNode = {textContent: ''};
const panel = {querySelectorAll() { return controls; }, querySelector() { return statusNode; }};
const messages = [], requests = [], timers = new Map();
let nextTimer = 1, respond = async () => ({}), renders = 0;
const sandbox = vm.createContext({
  document: {querySelector: key => key === '#storyboard-dialog' ? modal : panel, addEventListener() {}},
  window: {addEventListener() {}}, console, URL, Blob,
  escapeHtml: text => text, toast: text => messages.push(text),
  api: (path, options) => { requests.push({path, options}); return respond(path, options); },
  setTimeout: (fn, delay) => { const id = nextTimer++; timers.set(id, {fn, delay}); return id; },
  clearTimeout: id => timers.delete(id),
  rendered: () => { renders++; },
});
const file = process.argv[2] || 'src/landscape_culler/static/toolbox-storyboard.js';
let source = fs.readFileSync(file, 'utf8');
const publicReturn = 'return {open,close,hasPending:()=>dirty||busy};';
assert.ok(source.includes(publicReturn));
source = source.replace(publicReturn, `return {open,close,hasPending:()=>dirty||busy,__test:{
  load,save,poll,home,onEditorInput,exportUrl,
  seed(value){workspace=value.workspace;proposal=value.proposal||null;job=value.job||null;dirty=Boolean(value.dirty);busy=false;historical=Boolean(value.historical);historyRows=[];activeView=null;sessionEpoch++;viewEpoch++;},
  state(){return {workspace,proposal,job,dirty,busy,historical};},
  quiet(){render=()=>{rendered();lockViewControls();};renderHome=()=>{rendered();lockViewControls();};collect=()=>{};showJob=()=>{lockViewControls();};}
}};`);
vm.runInContext(source, sandbox);
const ui = sandbox.PhotoStoryboard, test = ui.__test;
test.quiet();
const workspace = id => ({workspace_id: id, revision: 7, name: id, items: []});
const proposal = (id, revision = 2) => ({proposal_id: id, revision, workspace_revision: 7,
  title: id, brief: '', chapters: [], status: 'draft', is_latest: true});
function reset(extra = {}) {
  requests.length = 0; messages.length = 0; timers.clear(); renders = 0;
  modal.open = true; controls[0].disabled = false; controls[1].disabled = true;
  test.seed({workspace: workspace('A'), proposal: proposal('draft-A'), ...extra});
}
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => {resolve=yes;reject=no;}); return {promise,resolve,reject}; };

reset();
const delayedLoad = deferred();
respond = path => path.endsWith('/history') ? Promise.resolve({revisions:[{revision:2}]}) : delayedLoad.promise;
const pendingLoad = test.load('draft-B');
assert.equal(test.state().busy, true);
assert.equal(controls[0].disabled, true, 'Editing is disabled before a delayed load can replace the form');
assert.equal(await ui.open(workspace('other')), false, 'A second workspace transition cannot race the first');
assert.equal(await test.save('approved'), false, 'Approval cannot target a form being replaced');
assert.equal(requests.length, 2);
delayedLoad.resolve({proposal:proposal('draft-B')});
assert.equal(await pendingLoad, true);
assert.equal(test.state().proposal.proposal_id, 'draft-B');
assert.equal(controls[0].disabled, false);
assert.equal(controls[1].disabled, true, 'Pre-existing source-range locks survive loading');

reset({dirty:true,job:{job_id:'model-1',status:'running'}});
respond = async () => ({job:{job_id:'model-1',status:'done',proposal_id:'new-ai-draft'}});
await test.poll();
assert.equal(test.state().proposal.proposal_id, 'draft-A');
assert.equal(test.state().dirty, true, 'Model completion must not save or discard an in-progress human draft');
assert.equal(requests.length, 1, 'Completion does not navigate or issue a PATCH');

reset({job:{job_id:'model-2',status:'running'}});
const delayedPoll = deferred(); respond = () => delayedPoll.promise;
const pendingPoll = test.poll();
assert.equal(await ui.close(), true);
assert.equal(modal.open, false);
delayedPoll.resolve({job:{job_id:'model-2',status:'done',proposal_id:'new-ai-draft'}});
await pendingPoll;
assert.equal(test.state().job.status, 'running', 'A closed session ignores a late polling response');
assert.equal(timers.size, 0, 'Closing stops front-end polling without cancelling the backend job');

reset({dirty:true});
const delayedSave = deferred(); respond = () => delayedSave.promise;
const pendingSave = test.save('approved');
assert.equal(controls[0].disabled, true);
assert.equal(await test.load('draft-other'), false);
assert.equal(requests.length, 1);
assert.equal(requests[0].options.body.revision, 2);
assert.equal(requests[0].options.body.workspace_revision, 7);
delayedSave.reject(new Error('revision conflict'));
assert.equal(await pendingSave, false);
assert.equal(test.state().dirty, true);
assert.equal(test.state().busy, false);
assert.equal(controls[0].disabled, false);
assert.equal(controls[1].disabled, true, 'Failed saves must not unlock human-confirmed source intervals');

reset({historical:true});
respond = async () => { throw new Error('No HTTP expected'); };
assert.equal(await test.save('approved'), false);
assert.equal(requests.length, 0, 'Historical versions cannot be approved or edited in place');

// Native select controls fire input before change. Selecting history must not
// create an implicit draft revision and invalidate the just-approved version.
reset({proposal:proposal('approved-history',0)});
let serverRevision=0, serverStatus='draft';
respond = async (path, options) => {
  if(options?.method==='PATCH'){
    serverRevision++;
    serverStatus=options.body.status || 'draft';
    return {proposal:{...proposal('approved-history',serverRevision),status:serverStatus}};
  }
  if(path.endsWith('/history'))return {revisions:[{revision:1},{revision:0}]};
  return {proposal:{...proposal('approved-history',0),status:'draft',is_latest:false}};
};
assert.equal(await test.save('approved'),true);
assert.equal(serverRevision,1);
assert.equal(serverStatus,'approved');
requests.length=0;
test.onEditorInput({target:{id:'story-history',value:'0'}});
assert.equal(test.state().dirty,false,'History input must not mark the approved draft dirty');
await test.load('approved-history',0);
assert.equal(requests.length,2);
assert.ok(requests.every(request=>!request.options?.method||request.options.method==='GET'),'History navigation only reads');
assert.equal(serverRevision,1,'History navigation cannot create revision 2');
assert.equal(serverStatus,'approved','The current approved revision remains approved');
assert.equal(test.state().proposal.revision,0);
assert.equal(test.state().historical,true);
assert.equal(test.exportUrl('json'),'/api/review-workspaces/A/storyboards/approved-history/export?format=json&revision=0');
assert.equal(test.exportUrl('csv'),'/api/review-workspaces/A/storyboards/approved-history/export?format=csv&revision=0');

reset();
test.onEditorInput({target:{id:'story-title'}});
assert.equal(test.state().dirty,true,'Actual content edits must still require saving');

reset();
const delayedOpen = deferred();
respond = path => {
  if(path === '/api/review-workspaces/B') return delayedOpen.promise;
  if(path.endsWith('/storyboards')) return Promise.resolve({proposals:[]});
  if(path.endsWith('/storyboard-jobs/latest')) return Promise.resolve({job:{job_id:'recovered',status:'running'}});
  return Promise.resolve({job:{job_id:'recovered',status:'running'}});
};
const pendingOpen = ui.open(workspace('B'));
assert.equal(await ui.close(), false, 'Closing cannot race an in-flight view mutation');
assert.equal(await ui.open(workspace('C')), false);
delayedOpen.resolve(workspace('B'));
assert.equal(await pendingOpen, true);
await Promise.resolve();
assert.equal(test.state().workspace.workspace_id, 'B');
assert.equal(test.state().job.job_id, 'recovered');
assert.ok(requests.every(request => !request.path.includes('/C')));
console.log('PASS: delayed loads, approvals, polling completion, close/reopen and historical view races');
