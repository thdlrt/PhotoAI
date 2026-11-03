import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const elements = new Map();
function element(key) {
  if (!elements.has(key)) elements.set(key, {textContent:"", innerHTML:"", disabled:false,
    showModal(){this.open=true;}, close(){this.open=false;}, addEventListener(){}});
  return elements.get(key);
}
const context = vm.createContext({document:{querySelector:element,addEventListener(){}}, console,
  setTimeout,clearTimeout,setInterval,clearInterval});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"),context);
const run = code => vm.runInContext(code,context);
const requests = [], messages = [];
context.request = (path, options) => requests.push({path,...options});
context.message = value => messages.push(value);
run(`renderReview = () => { if (state.dialogIndex != null) updatePhotoDialog(state.dialogIndex); };
syncProjectRunStats = () => {}; toast = message;
state.currentRun = {run_id:'a',workflow_state:'scored',xmp_ready:true,review_revision:5,results:[
 {index:0,group_id:1,effective_rating:4,ai_rating:4,score:.9,filename:'A'},
 {index:1,group_id:1,effective_rating:3,ai_rating:3,score:.8,filename:'B'},
 {index:2,group_id:1,effective_rating:5,ai_rating:5,score:.99,filename:'removed',excluded:true}
]};
let serverRevision = 5;
api = async (path, options) => { request(path, options); await new Promise(resolve=>setTimeout(resolve,5));
 if(options.body.base_revision!==serverRevision) throw new Error('请刷新');
 return {review_revision:++serverRevision,manual_adjusted_count:1}; };
openPhoto(0); setRating(0,'0'); setRating(1,'4'); setRating(1,'5');`);
await run("ratingSave");
assert.deepEqual(requests.map(item=>item.body.base_revision), [5,6,7]);
assert.deepEqual(requests.map(item=>item.body.rating), [0,4,5]);
assert.equal(run("state.currentRun.results[1].effective_rating"),5);
assert.equal(run("state.currentRun.candidate_count"),1); // Removed photo is never counted.
assert.equal(element('#dialog-position').textContent,'组 1 · 1 / 2');
assert.equal(element('#dialog-next').disabled,false);
run('navigatePhotoGroup(1)');
assert.equal(run('state.dialogIndex'),1); // No skip after A was moved to the end of the grid.
assert.equal(element('#dialog-next').disabled,true);
run('openPhoto(1)');
assert.equal(element('#dialog-position').textContent,'组 1 · 1 / 2'); // New session uses current grid order.

// An in-flight response belongs to its captured project, not the visible project.
run(`let oldRun=state.currentRun; let release;
api = async (path,options)=>{request(path,options);await new Promise(resolve=>{release=resolve;});
 return {review_revision:9,manual_adjusted_count:2};}; setRating(0,'3');`);
await new Promise(resolve=>setTimeout(resolve,0));
run(`state.currentRun={run_id:'b',review_revision:20,results:[{index:0,effective_rating:0}]};
state.developPlan={id:'b-plan'}; release();`);
await run('ratingSave');
assert.equal(run('state.currentRun.results[0].effective_rating'),0);
assert.equal(run('state.currentRun.review_revision'),20);
assert.equal(run('state.developPlan.id'),'b-plan');
assert.equal(run('oldRun.results[0].effective_rating'),3);

// External edits refresh the affected project without deadlocking this queue.
run(`state.currentRun=oldRun; state.dialogIndex=null;
let externalEdit=true; api = async(path,options)=>{
 request(path,options);
 if(!options) return {...oldRun,review_revision:30};
 if(externalEdit){externalEdit=false;throw new Error('评分已更新，请刷新后重试');}
 return {review_revision:31,manual_adjusted_count:2};
}; setRating(0,'4'); setRating(1,'reset');`);
await run('ratingSave');
assert.equal(requests.at(-1).body.base_revision,30);
assert.equal(run('state.currentRun.results[1].effective_rating'),3);
assert.equal(messages.length,1);
run(`state.filter='candidates'`);
assert.equal(run('matchesFilter({effective_rating:0})'),false);
assert.equal(run('matchesFilter({effective_rating:3})'),true);
assert.equal(run('matchesFilter({effective_rating:5})'),true);

const transitions = [];
context.transition = revision => transitions.push(revision);
run(`let releaseSave;
loadDevelop = async () => transition(state.currentRun.review_revision);
api = async(path,options)=>{await new Promise(resolve=>{releaseSave=resolve;});
 return {review_revision:32,manual_adjusted_count:2};};
setRating(0,'4'); let transitionDone=goWorkflowStage('crop');`);
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(transitions.length,0);
run('releaseSave()');
await run('transitionDone');
assert.deepEqual(transitions,[32]);

run(`goWorkflowStage = async () => {};
api = async(path,options)=>{
 request(path,options);
 if(path.endsWith('/skip-analysis')) return {exists:true,revision:1,eligible_count:2,confirmed_count:2};
 await new Promise(resolve=>{releaseSave=resolve;});
 return {review_revision:33,manual_adjusted_count:2};};
setRating(0,'3'); let skipDone=skipAutomaticCrop();`);
await new Promise(resolve=>setTimeout(resolve,0));
assert.ok(!requests.at(-1).path.endsWith('/skip-analysis'));
run('releaseSave()');
await run('skipDone');
assert.equal(requests.at(-1).body.base_revision,33);
console.log('rating queue: rapid edits, stable navigation, project switching and revision conflict PASS');
