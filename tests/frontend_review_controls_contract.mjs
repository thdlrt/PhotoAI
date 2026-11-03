import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {style: {setProperty(name, value) { this[name] = value; }},
    value: "", textContent: "", innerHTML: "", src: "", disabled: false, addEventListener() {}});
  return elements.get(selector);
}
const context = vm.createContext({document: {querySelector: element, addEventListener() {}}, console,
  setTimeout, clearTimeout, setInterval, clearInterval});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const run = code => vm.runInContext(code, context);
assert.match(run("photoScoreMarkup({score: .7243}, false)"), /综合分 72\.4/);
assert.match(run("photoScoreMarkup({score: 0}, false)"), /综合分 0\.0/);
assert.match(run("photoScoreMarkup({score: 1}, false)"), /综合分 100\.0/);
assert.equal(run("photoScoreMarkup({score: .7243}, true)"), "");
for (const score of ["undefined", "null", "NaN", "Infinity", "-.1", "1.1", "'0.7'"]) {
  assert.equal(run(`photoScoreMarkup({score: ${score}}, false)`), "");
}
run(`state.currentRun = { workflow_state: "scored", results: [
  {index: 0, group_id: 5, effective_rating: 0, score: .8, filename: "zero", preview_url: "zero"},
  {index: 1, group_id: 5, effective_rating: 4, score: .7, filename: "four", preview_url: "four"},
  {index: 2, group_id: 6, effective_rating: 5, score: .9, filename: "other", preview_url: "other"},
  {index: 3, group_id: 5, effective_rating: 5, score: .9, filename: "removed", excluded: true},
  {index: 4, group_id: 5, effective_rating: 5, score: .9, filename: "deleted", deleted: true, excluded: true}
] }; ratingControls = () => "rating";`);
run("updatePhotoDialog(1)");
assert.equal(element("#dialog-previous").disabled, true);
assert.equal(element("#dialog-next").disabled, false);
assert.equal(element("#dialog-position").textContent, "组 5 · 1 / 2");
run("navigatePhotoGroup(1)");
assert.equal(run("state.dialogIndex"), 0);
assert.equal(element("#dialog-next").disabled, true);
run("navigatePhotoGroup(1)");
assert.equal(run("state.dialogIndex"), 0); // Never cross into group 6.
run("navigatePhotoGroup(-1)");
assert.equal(run("state.dialogIndex"), 1);
run("state.groupingMode = true; updatePhotoDialog(0); navigatePhotoGroup(1)");
assert.equal(run("state.dialogIndex"), 1); // Grouping uses the visible index order.
run("updatePhotoDialog(2)");
assert.ok(element("#dialog-next").disabled && element("#dialog-previous").disabled);
run("setThumbnailSize(320)");
assert.equal(element("#review-workspace").style["--review-thumbnail-size"], "320px");
assert.equal(element("#review-workspace").style["--review-preview-height"], "274px");
run("setThumbnailSize(999)");
assert.equal(run("state.thumbnailSize"), 420);
run("setThumbnailSize(NaN)");
assert.equal(run("state.thumbnailSize"), 210);
const sizes = [];
context.recordSize = value => sizes.push(value);
run(`api = async (_path, options) => { recordSize(options.body.thumbnail_size); return {thumbnail_size: options.body.thumbnail_size}; };
  state.bootstrap = {preferences: {}}; setThumbnailSize(180); saveThumbnailSize(); setThumbnailSize(340); saveThumbnailSize();`);
await run("thumbnailSave");
assert.deepEqual(sizes, [180, 340]);
assert.equal(run(`reclassificationRunId({kind:"score", status:"failed", message:"照片目录内容已变化，请重新分类后再评分。", context:{source_run_id:"old-run"}})`), "old-run");
assert.equal(run(`reclassificationRunId({kind:"score", status:"failed", message:"照片在分类后发生变化，请重新分类：a.ARW", context:{source_run_id:"old-run"}})`), "old-run");
assert.equal(run(`reclassificationRunId({kind:"develop", status:"failed", message:"照片目录内容已变化", context:{source_run_id:"old-run"}})`), "");
assert.equal(run(`reclassificationRunId({kind:"score", status:"failed", message:"model unavailable", context:{source_run_id:"old-run"}})`), "");
assert.equal(run(`reclassificationRunId({kind:"score", status:"failed", requires_reclassification:false, message:"照片目录内容已变化", context:{source_run_id:"old-run"}})`), "");
const groupStarts = [];
const requests = [];
context.recordGroupStart = (path, body) => groupStarts.push([path, body]);
context.recordRequest = path => requests.push(path);
run(`renderReview = () => {}; renderJobBar = () => {}; renderModelInstallProgress = () => {};
  requireModelSetupUi = async () => true;
  state.currentRun = {run_id:"visible-run", project_id:"visible-project", retain_ratio:.25, scoring_mode:"fast"};
  startJob = async (path, body) => { recordGroupStart(path, body); return {id:"new-classification"}; };
  api = async (path) => { recordRequest(path); return {run_id:"old-run", project_id:"old-project", review_revision:17, retain_ratio:.4, scoring_mode:"deep"}; };`);
await run("reclassifyRun()");
await run(`retryFailedJob({id:"failed-score", kind:"score", status:"failed", retryable:true,
  message:"照片目录内容已变化，请重新分类后再评分。", context:{source_run_id:"old-run"}})`);
assert.deepEqual(groupStarts.map(([path]) => path), ["/api/projects/visible-project/group", "/api/projects/old-project/group"]);
assert.equal(groupStarts[0][1].mode, "fast");
assert.equal(groupStarts[1][1].retain_ratio, .4);
assert.deepEqual(requests, ["/api/runs/old-run"]); // Never repeat the stale scoring snapshot.
await run(`retryFailedJob({id:"old-delete-error", kind:"score", status:"failed", retryable:true,
  requires_reclassification:false, context:{source_run_id:"old-run"}})`);
assert.equal(groupStarts[2][0], "/api/runs/old-run/score");
assert.equal(groupStarts[2][1].base_revision, 17); // Fresh snapshot preserves subsequent edits.
run("state.activeJob = {id:'busy'}");
await run("reclassifyRun()");
assert.equal(groupStarts.length, 3);
console.log("review controls: score display, group boundaries, visible order, sizing and serial preference saves passed");
