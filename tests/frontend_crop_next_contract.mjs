import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const [appPath, templatePath] = process.argv.slice(2);
if (!appPath || !templatePath) throw new Error("app.js and index.html paths are required");

class FakeClassList {
  constructor(initial = "") {
    this.values = new Set(String(initial).split(/\s+/).filter(Boolean));
  }

  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
    if (enabled) this.values.add(name);
    else this.values.delete(name);
    return enabled;
  }
  toString() { return [...this.values].join(" "); }
}

class FakeElement {
  constructor(selector, className = "") {
    this.selector = selector;
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this.disabled = false;
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.checked = false;
    this.open = false;
    this._className = className;
    this.classList = new FakeClassList(className);
    this.span = selector === "#develop-flow-progress" ? new FakeElement(`${selector} > span`) : null;
  }

  get className() { return this._className; }
  set className(value) {
    this._className = String(value);
    this.classList = new FakeClassList(this._className);
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  dispatch(type) {
    const event = {
      target: this,
      preventDefault() {},
      stopPropagation() {},
      key: "",
      shiftKey: false,
    };
    return (this.listeners.get(type) || []).map((listener) => listener(event));
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  querySelector(selector) {
    if (selector === "span" && this.span) return this.span;
    return new FakeElement(`${this.selector} ${selector}`);
  }
  closest() { return null; }
  replaceChildren(node) { this.textContent = node?.textContent ?? String(node ?? ""); }
  showModal() { this.open = true; }
  close() { this.open = false; }
}

const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) {
    const className = selector === "#develop-flow-progress" ? "workflow-inline-progress hidden" : "";
    elements.set(selector, new FakeElement(selector, className));
  }
  return elements.get(selector);
}

const documentListeners = new Map();
const document = {
  activeElement: { tagName: "BODY" },
  querySelector: element,
  querySelectorAll: () => [],
  createTextNode: (textContent) => ({ textContent: String(textContent) }),
  addEventListener(type, listener) {
    const listeners = documentListeners.get(type) || [];
    listeners.push(listener);
    documentListeners.set(type, listeners);
  },
};
const location = { hash: "#develop/run-1/crop" };
const history = {
  pushState(_state, _title, hash) { location.hash = hash; },
  replaceState(_state, _title, hash) { location.hash = hash; },
};
const window = {
  addEventListener() {},
  scrollTo() {},
};

const sandbox = {
  console,
  document,
  history,
  location,
  window,
  fetch: async () => { throw new Error("unexpected fetch"); },
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
};
window.document = document;
window.history = history;
window.location = location;
window.window = window;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(appPath, "utf8"), sandbox, { filename: appPath });

const run = (source) => vm.runInContext(source, sandbox);
const clone = (value) => JSON.parse(JSON.stringify(value));
const nextTurn = () => new Promise((resolve) => setImmediate(resolve));
async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await nextTurn();
  }
  throw new Error(message);
}
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const views = [];
const toasts = [];
const requests = [];
let apiHandler = null;
sandbox.__setView = (...args) => views.push(args);
sandbox.__toast = (message) => toasts.push(String(message));
sandbox.__api = (path, options = {}) => {
  requests.push({ path, options });
  if (!apiHandler) throw new Error(`unexpected API request: ${path}`);
  return apiHandler(path, options);
};
run(`
  setView = (...args) => globalThis.__setView(...args);
  toast = (message) => globalThis.__toast(message);
  api = (...args) => globalThis.__api(...args);
`);

const basePlan = {
  exists: true,
  stale: false,
  source_review_revision: 7,
  revision: 3,
  plan_id: "develop-plan-1",
  eligible_count: 2,
  confirmed_count: 1,
  crop_skipped: false,
  color_enabled: true,
  color_mode: "pending",
  crop: { status: "pending" },
  basic_color: { status: "pending" },
  creative_style: { status: "skipped", groups: {} },
  items: [],
};
const confirmedPlan = {
  ...basePlan,
  revision: 4,
  confirmed_count: 2,
  crop: { status: "confirmed" },
};
const readyPlan = {
  ...confirmedPlan,
  revision: 5,
  color_mode: "auto",
  basic_color: { status: "enabled" },
};

function reset(plan) {
  views.length = 0;
  toasts.length = 0;
  requests.length = 0;
  element("#job-bar").className = "job-bar hidden";
  element("#job-progress-fill").style.width = "0%";
  run(`
    state.currentRun = {
      run_id: "run-1",
      review_revision: 7,
      xmp_ready: true,
      input_root: "X:\\\\photos\\\\demo",
      candidate_count: 2,
      results: [],
    };
    state.developStage = "crop";
    state.developBusy = false;
    state.developProgress = null;
    state.activeJob = null;
    state.jobs = [];
    state.modelResources = { profiles: [{ id: "16gb", label: "16GB 显存", configured: true, ready: true }] };
    state.developPlan = ${JSON.stringify(plan)};
    renderDevelop();
  `);
}

// The simplified workflow has one global progress surface and keeps project
// creation, scoring choices, processing, and XMP history in their intended steps.
const template = fs.readFileSync(templatePath, "utf8");
assert.match(template, /data-workflow-stage="base"[^>]*>[\s\S]*?<strong>基础调色<\/strong>/);
assert.match(template, /data-workflow-stage="style"[^>]*>[\s\S]*?<strong>创意外观<\/strong>/);
assert.doesNotMatch(template, /调色不会在本页写入照片|每组独立选择风格|读取风格库/);
const developViewStart = template.indexOf('<section id="view-develop"');
const exportViewStart = template.indexOf('<section id="view-export"');
const settingsViewStart = template.indexOf('<section id="view-settings"');
assert.ok(developViewStart >= 0 && exportViewStart > developViewStart && settingsViewStart > exportViewStart);
const developMarkup = template.slice(developViewStart, exportViewStart);
const exportMarkup = template.slice(exportViewStart, settingsViewStart);
assert.doesNotMatch(developMarkup, /id="export-(?:xmp|jpeg)"|data-export-target=/, "output targets belong only to the final export step");
assert.equal((template.match(/id="export-xmp"/g) || []).length, 1);
assert.equal((template.match(/id="export-jpeg"/g) || []).length, 1);
assert.match(exportMarkup, /id="export-xmp"/);
assert.match(exportMarkup, /id="export-jpeg"/);
assert.match(exportMarkup, /id="export-status" class="develop-output-hint hidden"/);
assert.match(developMarkup, /id="develop-flow-copy"><\/span>/);
assert.match(template, /id="job-progress-track"[^>]*role="progressbar"/);
assert.doesNotMatch(developMarkup, /id="develop-flow-progress"|id="style-progress"/);
assert.doesNotMatch(template, /id="train-form"|id="model-detail"|id="audit-button"|尚无可用模型|个人偏好/);
assert.doesNotMatch(template, /id="review-export"|直接导出/);
assert.match(template, /id="toolbox-xmp"/);
assert.match(developMarkup, /data-color-mode="auto"/);
assert.match(developMarkup, /data-color-mode="skip"/);
assert.match(developMarkup, /data-style-scope="global">全局统一/);
assert.match(developMarkup, /data-style-scope="group">按组设置/);
const cullForm = template.slice(template.indexOf('<form id="cull-form"'), template.indexOf("</form>", template.indexOf('<form id="cull-form"')));
assert.match(cullForm, /id="cull-path"/);
assert.doesNotMatch(cullForm, /id="scoring-mode"|id="retain-ratio"/);
const footerStart = template.indexOf('<div id="develop-flow" class="workflow-footer">');
const footerEnd = template.indexOf("</section>", footerStart);
assert.notEqual(footerStart, -1, "develop workflow footer is missing");
assert.notEqual(footerEnd, -1, "develop workflow footer is not closed");
const footerMarkup = template.slice(footerStart, footerEnd);
assert.doesNotMatch(footerMarkup, /role="progressbar"|develop-flow-progress/);

reset(basePlan);
run(`
  state.developPlan = { ...state.developPlan, generating: true };
  state.developBusy = true;
  state.developProgress = {
    status: "running",
    stage_label: "生成构图预览",
    current: 1,
    total: 2,
    overall_percent: 42,
    nodes: [],
  };
  renderDevelop();
`);
const globalProgress = element("#job-progress-track");
assert.equal(element("#job-bar").classList.contains("hidden"), false, "generation progress must use the global job bar");
assert.equal(globalProgress.getAttribute("aria-valuenow"), "42");
assert.equal(element("#job-progress-fill").style.width, "42%");
assert.equal(element("#develop-next").disabled, true);

// With pending crop choices, Next confirms them before entering the independent
// base-calibration stage. It must not silently choose a color mode.
reset(basePlan);
assert.equal(element("#develop-next").disabled, false);
assert.equal(element("#develop-next").textContent, "采用当前构图并继续");
const confirmRequest = deferred();
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/develop" && !options.method) return clone(basePlan);
  if (path === "/api/runs/run-1/develop/confirm-all" && options.method === "POST") return confirmRequest.promise;
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__advance = advanceFromCrop()");
await waitFor(
  () => requests.some((request) => request.path.endsWith("/confirm-all")),
  "advance did not request crop confirmation",
);
assert.equal(run("state.developStage"), "crop");
assert.equal(views.some(([, route]) => String(route).endsWith("/base")), false);
assert.equal(element("#develop-next").disabled, true);
assert.equal(
  element("#job-bar").classList.contains("hidden"),
  false,
  "saving crop choices must remain visible in the global job bar",
);

confirmRequest.resolve(clone(confirmedPlan));
await sandbox.__advance;
assert.equal(run("state.developStage"), "base");
assert.equal(views.filter(([, route]) => String(route).endsWith("/base")).length, 1);
assert.deepEqual(
  requests.filter((request) => request.options.method === "POST").map((request) => request.path),
  ["/api/runs/run-1/develop/confirm-all"],
);
assert.equal(element("#develop-next").disabled, true, "base calibration must be selected or skipped explicitly");

// If confirmation fails, the action is recoverable and never navigates away.
reset(basePlan);
const failedConfirm = deferred();
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/develop" && !options.method) return clone(basePlan);
  if (path.endsWith("/confirm-all")) return failedConfirm.promise;
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__advance = advanceFromCrop()");
await waitFor(() => requests.some((request) => request.path.endsWith("/confirm-all")), "confirmation was not requested");
failedConfirm.reject(new Error("构图确认保存失败"));
await sandbox.__advance;
assert.equal(run("state.developStage"), "crop");
assert.equal(run("state.developBusy"), false);
assert.equal(views.some(([, route]) => String(route).endsWith("/base")), false);
assert.ok(toasts.some((message) => message.includes("保存失败")));

// A fully confirmed plan enters base calibration without additional writes.
reset(readyPlan);
assert.equal(element("#develop-next").disabled, false);
assert.equal(element("#develop-next").textContent, "采用当前构图并继续");
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/develop" && !options.method) return clone(readyPlan);
  throw new Error(`unexpected write request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__advance = advanceFromCrop()");
await sandbox.__advance;
assert.equal(run("state.developStage"), "base");
assert.equal(requests.filter((request) => request.options.method === "POST").length, 0);
assert.equal(views.filter(([, route]) => String(route).endsWith("/base")).length, 1);

views.length = 0;
run("globalThis.__advance = goWorkflowStage('style')");
await sandbox.__advance;
assert.equal(run("state.developStage"), "style");
assert.equal(views.filter(([, route]) => String(route).endsWith("/style")).length, 1);
assert.equal(element("#develop-back").textContent, "上一步：基础调色");
assert.equal(element("#develop-next").textContent, "下一步：导出");

// Style candidates may only show their own completed Lightroom render. Pending
// and failed candidates remain honest placeholders and cannot be selected.
const stylePlan = {
  ...readyPlan,
  revision: 11,
  color_mode: "style",
  creative_style: {
    status: "pending",
    groups: {
      "7": {
        group_id: 7,
        status: "pending",
        recommendation_status: "rendering",
        preset_id: null,
        representative_index: 70,
        top3: [
          { lut_id: "lut:ready", lut_hash: "lut-hash-ready", preset_id: "uuid:ready", preset_hash: "hash-ready", label: "Ready", source: "Test", render_status: "ready", preview_url: "/style/ready.jpg", amount: 90, amount_supported: true, score: 0.8 },
          { preset_id: "uuid:pending", preset_hash: "hash-pending", label: "Pending", source: "Test", render_status: "pending", preview_url: "/style/must-not-show-pending.jpg" },
          { preset_id: "uuid:failed", preset_hash: "hash-failed", label: "Failed", source: "Test", render_status: "failed", preview_url: "/style/must-not-show-failed.jpg" },
        ],
      },
    },
  },
  items: [{ index: 70, group_id: 7, filename: "representative.ARW", preview_url: "/develop/representative.jpg" }],
};
run(`
  state.developBusy = false;
  state.activeJob = null;
  state.developPlan = ${JSON.stringify(stylePlan)};
  renderStyleGroups(state.developPlan);
`);
let styleMarkup = element("#style-groups").innerHTML;
assert.equal(element("#style-groups").classList.contains("group-table"), true);
assert.match(styleMarkup, /class="panel style-group style-group-row"[^>]*data-style-group="7"[^>]*data-representative-index="70"/);
assert.match(styleMarkup, /representative\.ARW · 1 张/);
assert.equal((styleMarkup.match(/class="style-choice /g) || []).length, 4, "one group row must compare Natural plus three filter candidates");
assert.match(styleMarkup, /src="\/develop\/representative\.jpg"/);
assert.equal((styleMarkup.match(/\/develop\/representative\.jpg/g) || []).length, 1, "only the natural card may use the representative preview");
assert.match(styleMarkup, /src="\/style\/ready\.jpg"/);
assert.doesNotMatch(styleMarkup, /must-not-show-pending/);
assert.doesNotMatch(styleMarkup, /must-not-show-failed/);
assert.match(styleMarkup, /data-preset-id="uuid:pending"[^>]*data-preview-ready="false"[^>]*disabled/);
assert.match(styleMarkup, /data-preset-id="uuid:failed"[^>]*data-preview-ready="false"[^>]*disabled/);
assert.match(styleMarkup, /等待 Lightroom 真实预览/);
assert.match(styleMarkup, /Lightroom 预览失败/);
assert.doesNotMatch(styleMarkup, /class="style-choice selected[^"]*"[^>]*data-choice-id="natural"/, "unfinished recommendations must not imply that natural is selected");

run(`
  state.developPlan.creative_style.groups["7"].recommendation_status = "complete";
  state.developPlan.creative_style.groups["7"].recommended_kind = "neutral";
  renderStyleGroups(state.developPlan);
`);
styleMarkup = element("#style-groups").innerHTML;
assert.match(styleMarkup, /class="style-choice selected[^"]*"[^>]*data-choice-id="natural"/, "a completed neutral recommendation may select the natural card");

run(`
  state.developPlan.creative_style.groups["7"].status = "pending";
  state.developPlan.creative_style.groups["7"].recommended_kind = "lut";
  state.developPlan.creative_style.groups["7"].recommended_lut_id = "lut:ready";
  state.developPlan.creative_style.groups["7"].amount_supported = false;
  renderStyleGroups(state.developPlan);
`);
styleMarkup = element("#style-groups").innerHTML;
assert.match(styleMarkup, /class="style-choice selected[^"]*"[^>]*data-choice-id="lut:ready"/);
assert.match(styleMarkup, /class="style-amount-tiers"/, "a pending recommendation must use its matching candidate capability");
assert.match(styleMarkup, /data-style-tier="50"[\s\S]*data-style-tier="100"[\s\S]*data-style-tier="150"/, "the UI must expose only the three fixed strength tiers");
assert.match(styleMarkup, /data-style-amount="7"[^>]*data-lut-id="lut:ready"[^>]*data-lut-hash="lut-hash-ready"/, "fixed tiers must carry the recommended resource identity");
assert.doesNotMatch(styleMarkup, /type="range"/, "the continuous strength slider must be removed");
assert.doesNotMatch(styleMarkup, /此预设未启用 Lightroom 强度调整/);

run(`
  state.developPlan.creative_style.groups["7"].status = "confirmed";
  state.developPlan.creative_style.groups["7"].lut_id = "lut:ready";
  state.developPlan.creative_style.groups["7"].amount = 55;
  state.developPlan.creative_style.groups["7"].amount_supported = false;
  renderStyleGroups(state.developPlan);
`);
styleMarkup = element("#style-groups").innerHTML;
assert.doesNotMatch(styleMarkup, /class="style-choice selected[^"]*"[^>]*data-choice-id="natural"/, "a manual preset selection must override an older neutral recommendation");
assert.match(styleMarkup, /class="style-choice selected[^"]*"[^>]*data-choice-id="lut:ready"[^>]*data-lut-id="lut:ready"/);
assert.doesNotMatch(styleMarkup, /class="style-amount-tiers"/, "a confirmed unsupported selection must hide the fixed strength tiers");
assert.match(styleMarkup, /此预设未启用 Lightroom 强度调整/);

// A legacy top-level `confirmed` flag is insufficient: every photo group must
// carry complete recommendation evidence or an explicit manual override.
const incompleteStylePlan = {
  ...stylePlan,
  creative_style: {
    status: "confirmed",
    groups: { "7": { status: "confirmed", manual_override: false } },
  },
};
assert.equal(run(`currentColorComplete(${JSON.stringify(incompleteStylePlan)})`), false);
const completeStylePlan = {
  ...incompleteStylePlan,
  creative_style: {
    status: "confirmed",
    groups: { "7": { status: "confirmed", manual_override: false, recommendation_status: "complete" } },
  },
};
assert.equal(run(`currentColorComplete(${JSON.stringify(completeStylePlan)})`), true);
const manualStylePlan = {
  ...incompleteStylePlan,
  creative_style: {
    status: "confirmed",
    groups: { "7": { status: "skipped", manual_override: true } },
  },
};
assert.equal(run(`currentColorComplete(${JSON.stringify(manualStylePlan)})`), true);

const globalStylePlan = {
  ...stylePlan,
  items: [
    { index: 70, group_id: 7, preview_url: "/develop/representative.jpg" },
    { index: 71, group_id: 8, preview_url: "/develop/brightest.jpg" },
    { index: 72, group_id: 9, preview_url: "/develop/darkest.jpg" },
  ],
  creative_style: {
    status: "confirmed",
    scope: "global",
    groups: {},
    global_selection: {
      ...stylePlan.creative_style.groups["7"],
      status: "confirmed",
      recommendation_status: "complete",
      recommended_kind: "preset",
      recommended_lut_id: null,
      recommended_preset_id: "uuid:ready",
      recommended_preset_hash: "hash-ready",
      lut_id: null,
      lut_hash: null,
      preset_id: "uuid:ready",
      preset_hash: "hash-ready",
      amount_supported: true,
      top3: stylePlan.creative_style.groups["7"].top3.map((choice, index) => index === 0 ? {
        ...choice,
        lut_id: null,
        lut_hash: null,
        preview_samples: [
          { role: "representative", index: 70, filename: "representative.ARW", preview_url: "/style/sample-representative.jpg" },
          { role: "brightest", index: 71, filename: "brightest.ARW", preview_url: "/style/sample-brightest.jpg" },
          { role: "darkest", index: 72, filename: "darkest.ARW", preview_url: "/style/sample-darkest.jpg" },
        ],
      } : choice),
    },
  },
};
assert.equal(run(`currentColorComplete(${JSON.stringify(globalStylePlan)})`), true);
assert.equal(run(`currentStyleScope(${JSON.stringify(globalStylePlan)})`), "global");
run(`state.developPlan = ${JSON.stringify(globalStylePlan)}; state.styleScope = "global"; renderStyleGroups(state.developPlan);`);
styleMarkup = element("#style-groups").innerHTML;
assert.equal(element("#style-groups").classList.contains("group-table"), false, "Global mode must keep its existing layout");
assert.match(styleMarkup, /<strong>全局统一<\/strong>/);
assert.match(styleMarkup, /应用于 3 张/);
assert.doesNotMatch(styleMarkup, /data-style-search-group|data-style-recommend-group/);
assert.match(styleMarkup, /data-style-scope="global"/);
assert.match(styleMarkup, /data-style-amount="global"/);
assert.match(styleMarkup, /统一效果抽查/);
assert.match(styleMarkup, /已用 Lightroom 刷新 3 张/);
assert.equal((styleMarkup.match(/\/style\/sample-/g) || []).length, 3, "global proof must show three exact Lightroom samples");

run(`
  state.currentRun = { run_id: "run-1", review_revision: 7, input_root: "X:\\\\photos\\\\demo", xmp_ready: true, candidate_count: 1, results: [] };
  state.developPlan = ${JSON.stringify(completeStylePlan)};
  state.developStage = "style";
  state.developBusy = false;
  state.activeJob = { id: "style-running", kind: "style_recommend", status: "running", context: { run_id: "run-1" } };
  renderDevelop();
`);
assert.equal(element("#develop-next").disabled, true, "a running style job must block export navigation");
run("state.activeJob = null; renderDevelop();");
assert.equal(element("#develop-next").disabled, false, "completed style evidence may proceed once no style job is active");
assert.equal(element("#style-recommend-all").classList.contains("hidden"), false, "Group mode must expose one all-groups action");
assert.equal(element("#style-recommend").classList.contains("hidden"), true, "the Global action must stay hidden in Group mode");
assert.equal(element("#style-recommend-all").textContent, "重新生成所有组");
assert.match(element("#style-group-batch-status").textContent, /已生成 1 \/ 1 组/);
run(`
  state.activeJob = { id: "style-all-running", kind: "style_recommend", status: "running", context: { run_id: "run-1", scope: "groups", group_count: 1 } };
  renderDevelop();
`);
assert.equal(element("#style-recommend-all").disabled, true);
assert.equal(element("#style-recommend-all").textContent, "正在生成所有组");
assert.equal(element("#style-group-batch-status").textContent, "逐组生成中");
run("state.activeJob = null;");

// Preserve real implementations before isolating the network/job contracts.
run(`
  globalThis.__realPollJobs = pollJobs;
  globalThis.__realRenderDevelop = renderDevelop;
  renderDevelop = () => globalThis.__styleRenderCalls += 1;
  renderJobBar = () => {};
  renderReview = () => {};
  renderExport = () => {};
  renderToolbox = () => {};
  pollJobs = () => { globalThis.__stylePollCalls += 1; };
`);
sandbox.__styleRenderCalls = 0;
sandbox.__stylePollCalls = 0;

function resetStyleActions(plan = stylePlan) {
  requests.length = 0;
  toasts.length = 0;
  apiHandler = null;
  run(`
    state.currentRun = { run_id: "run-1", review_revision: 7, results: [] };
    state.developPlan = ${JSON.stringify(plan)};
    state.developStage = "style";
    state.developBusy = false;
    state.activeJob = null;
    state.jobs = [];
    state.modelResources = { profiles: [{ id: "16gb", label: "16GB 显存", configured: true, ready: true }] };
    state.styleScope = "group";
    state.handledJobs = new Set();
  `);
}

// Recommendation endpoints return a normal job (`id`/`kind`), not `job_id`.
resetStyleActions();
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/style-recommendations" && options.method === "POST") {
    return {
      id: "job-recommend",
      kind: "style_recommend",
      title: "生成真实风格预览",
      status: "queued",
      context: { run_id: "run-1" },
      progress: { stage_label: "准备场景分析", overall_percent: 0 },
    };
  }
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__styleAction = recommendStyles(1, 'group')");
await sandbox.__styleAction;
assert.equal(run("state.activeJob.id"), "job-recommend");
assert.equal(run("state.activeJob.kind"), "style_recommend");
assert.equal(run("state.handledJobs.has('job-recommend')"), false);
assert.deepEqual(clone(requests.at(-1).options.body), { base_revision: 11, scope: "group", group_id: 1 });
assert.equal(
  toasts.some((message) => message.includes("正在生成") && message.includes("外观")),
  false,
  "the global green job bar must be the only in-progress surface",
);

resetStyleActions(globalStylePlan);
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/style-recommendations" && options.method === "POST") {
    return { id: "job-global", kind: "style_recommend", title: "生成统一外观", status: "queued", context: { run_id: "run-1" } };
  }
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__styleAction = recommendStyles(null, 'global')");
await sandbox.__styleAction;
assert.deepEqual(clone(requests.at(-1).options.body), { base_revision: 11, scope: "global", group_id: null });

resetStyleActions();
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/style-recommendations/groups" && options.method === "POST") {
    return {
      id: "job-all-groups",
      kind: "style_recommend",
      title: "为所有组生成外观",
      status: "queued",
      context: { run_id: "run-1", scope: "groups", group_id: null, group_count: 1 },
    };
  }
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__styleAction = recommendStylesForAllGroups()");
await sandbox.__styleAction;
assert.equal(requests.at(-1).path, "/api/runs/run-1/style-recommendations/groups");
assert.deepEqual(clone(requests.at(-1).options.body), { base_revision: 11 });
assert.equal(run("state.activeJob.id"), "job-all-groups");
assert.equal(run("state.activeJob.context.scope"), "groups");

// A fixed strength tier submits an exact Lightroom preview job. The same
// primitive also supports a preset selected from search, outside Top3.
const selectedStylePlan = {
  ...stylePlan,
  creative_style: {
    status: "pending",
    groups: {
      "7": {
        ...stylePlan.creative_style.groups["7"],
        status: "confirmed",
        recommendation_status: "complete",
        preset_id: "uuid:ready",
        preset_hash: "hash-ready",
        amount: 90,
        amount_supported: true,
      },
    },
  },
};
resetStyleActions(selectedStylePlan);
let previewJobNumber = 0;
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/style-preview" && options.method === "POST") {
    previewJobNumber += 1;
    return {
      id: `job-preview-${previewJobNumber}`,
      kind: "style_preview",
      title: "生成强度预览",
      status: "queued",
      context: { run_id: "run-1", group_id: 7 },
      progress: { stage_label: "Lightroom 重渲强度预览", overall_percent: 0 },
    };
  }
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__styleAction = requestStylePreview(7, { amount: 75 })");
assert.equal(await sandbox.__styleAction, true);
let previewRequest = requests.at(-1);
assert.equal(previewRequest.path, "/api/runs/run-1/style-preview");
assert.deepEqual(clone(previewRequest.options.body), {
  base_revision: 11,
  scope: "group",
  group_id: 7,
  preset_id: "uuid:ready",
  preset_hash: "hash-ready",
  amount: 75,
});
assert.equal(run("state.activeJob.kind"), "style_preview");

run("state.activeJob = null; state.jobs = [];");
run("globalThis.__styleAction = requestStylePreview(7, { presetId: 'uuid:search', presetHash: 'hash-search', amount: 100 })");
assert.equal(await sandbox.__styleAction, true);
previewRequest = requests.at(-1);
assert.deepEqual(clone(previewRequest.options.body), {
  base_revision: 11,
  scope: "group",
  group_id: 7,
  preset_id: "uuid:search",
  preset_hash: "hash-search",
  amount: 100,
});

run("state.activeJob = null; state.jobs = [];");
run("globalThis.__styleAction = requestStylePreview(7, { lutId: 'lut:search', lutHash: 'lut-hash-search', presetId: 'uuid:source', presetHash: 'hash-source', amount: 130 })");
assert.equal(await sandbox.__styleAction, true);
previewRequest = requests.at(-1);
assert.deepEqual(clone(previewRequest.options.body), {
  base_revision: 11,
  scope: "group",
  group_id: 7,
  lut_id: "lut:search",
  lut_hash: "lut-hash-search",
  amount: 130,
  strength: 130,
});

resetStyleActions(globalStylePlan);
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/style-preview" && options.method === "POST") {
    return { id: "job-global-preview", kind: "style_preview", title: "统一外观预览", status: "queued", context: { run_id: "run-1" } };
  }
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__styleAction = requestStylePreview(null, { amount: 60, scope: 'global' })");
assert.equal(await sandbox.__styleAction, true);
assert.deepEqual(clone(requests.at(-1).options.body), {
  base_revision: 11,
  scope: "global",
  group_id: null,
  preset_id: "uuid:ready",
  preset_hash: "hash-ready",
  amount: 60,
});

resetStyleActions(globalStylePlan);
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/develop/style/global" && options.method === "PUT") return clone(globalStylePlan);
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__styleAction = updateStyleGlobal({ status: 'skipped', amount: 0 })");
await sandbox.__styleAction;
assert.equal(requests.at(-1).path, "/api/runs/run-1/develop/style/global");
assert.deepEqual(clone(requests.at(-1).options.body), { base_revision: 11, status: "skipped", amount: 0 });

resetStyleActions(globalStylePlan);
run("state.styleScope = 'global'");
apiHandler = (path, options) => {
  if (path === "/api/runs/run-1/style-preview" && options.method === "POST") return { id: "job-global-adopt", kind: "style_preview", status: "queued", context: { run_id: "run-1" } };
  throw new Error(`unexpected API request: ${options.method || "GET"} ${path}`);
};
run("globalThis.__styleAction = confirmRecommendedStyles()");
await sandbox.__styleAction;
assert.equal(requests.at(-1).path, "/api/runs/run-1/style-preview");
assert.deepEqual(clone(requests.at(-1).options.body), { base_revision: 11, scope: "global", group_id: null, preset_id: "uuid:ready", preset_hash: "hash-ready", amount: 90 });

const appSource = fs.readFileSync(appPath, "utf8");
assert.doesNotMatch(appSource, /function (?:setColorEnabled|applyWithLightroom|saveXmpAtStage|submitTrain|startAudit)\b/);
assert.doesNotMatch(appSource, /data-export-writer|\.open-run|data-develop-style|data-develop-strength|尚无可用模型|个人偏好/);
assert.match(appSource, /updateDevelop\(Number\(cropChoice\.dataset\.developCropChoice\), \{ crop_id: cropChoice\.dataset\.cropId, confirmed: true \}\)/, "crop choices must save and confirm immediately");
assert.match(appSource, /retain_ratio: Number\(\$\("#retain-ratio"\)\.value \|\| 0\.30\)/);
assert.match(appSource, /mode: \$\("#scoring-mode"\)\.value === "fast" \? "fast" : "deep"/);
const styleChoiceHandler = appSource.slice(
  appSource.indexOf('const styleChoice = event.target.closest("[data-style-choice]")'),
  appSource.indexOf('const styleRecommendGroup = event.target.closest', appSource.indexOf('const styleChoice = event.target.closest("[data-style-choice]")')),
);
assert.match(styleChoiceHandler, /changes\.preset_id = null;[\s\S]*changes\.preset_hash = null;/, "selecting a LUT must clear stale preset identity");
assert.doesNotMatch(styleChoiceHandler, /if \(styleChoice\.dataset\.presetId\)/, "LUT provenance must not submit a second preset resource");

assert.match(
  appSource,
  /此预设由插件托管，Lightroom 仅支持 100%；可换原生预设调强度/,
  "plugin-managed presets must explain why fixed Amount tiers are unavailable",
);
const tierHandler = appSource.slice(
  appSource.indexOf('const styleTier = event.target.closest("[data-style-tier]")'),
  appSource.indexOf('const styleChoice = event.target.closest', appSource.indexOf('const styleTier = event.target.closest("[data-style-tier]")')),
);
assert.match(tierHandler, /requestStylePreview[\s\S]*amount: Number\(styleTier\.dataset\.styleTier\)/, "a fixed tier click must request one scoped exact preview job");
assert.doesNotMatch(appSource, /type="range"[^>]*data-style-amount/, "no continuous style strength control may remain");
const searchSelectionHandler = appSource.slice(
  appSource.indexOf('const styleSearchSelect = event.target.closest("[data-style-search-select]")'),
  appSource.indexOf('const selectInput = event.target.closest', appSource.indexOf('const styleSearchSelect = event.target.closest')),
);
assert.match(searchSelectionHandler, /requestStylePreview\(state\.styleSearchGroupId/);
assert.doesNotMatch(searchSelectionHandler, /updateStyleGroup/);

// Both style job kinds reload the develop plan after completion so fresh
// preview URLs and candidate statuses replace placeholders without navigation.
run(`
  pollJobs = globalThis.__realPollJobs;
  refreshBootstrap = async () => {};
`);
async function assertStyleCompletionReloads(kind) {
  resetStyleActions(selectedStylePlan);
  run("pollJobs = globalThis.__realPollJobs;");
  const completedJob = {
    id: `completed-${kind}`,
    kind,
    title: kind === "style_preview" ? "强度预览" : "风格推荐",
    status: "completed",
    message: "任务已完成。",
    context: { run_id: "run-1", group_id: 7 },
    result: { run_id: "run-1" },
  };
  const refreshed = { ...selectedStylePlan, revision: kind === "style_preview" ? 13 : 12 };
  apiHandler = (path) => {
    if (path === "/api/jobs") return [clone(completedJob)];
    if (path === "/api/runs/run-1/develop") return clone(refreshed);
    throw new Error(`unexpected API request: GET ${path}`);
  };
  await run("pollJobs()");
  assert.ok(requests.some((request) => request.path === "/api/runs/run-1/develop"), `${kind} completion did not reload develop`);
  assert.equal(run("state.developPlan.revision"), refreshed.revision);
}
await assertStyleCompletionReloads("style_recommend");
await assertStyleCompletionReloads("style_preview");

assert.match(appSource, /job\.result\?\.style_status === "partial"/);
assert.match(appSource, /\$\{job\.title\}部分完成：\$\{job\.message\}/);

console.log("crop-next frontend contract passed");
