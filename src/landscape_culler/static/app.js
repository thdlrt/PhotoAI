const state = {
  token: "",
  bootstrap: null,
  projects: [],
  jobs: [],
  transactions: [],
  toolboxTransactions: [],
  rawJpegPlan: null,
  toolboxBusy: false,
  xmpCleanupTransactions: [],
  xmpCleanupPlan: null,
  xmpCleanupBusy: false,
  projectBusy: false,
  toolboxSection: "home",
  activeJob: null,
  jobNotice: null,
  jobRetryBusy: false,
  deleteProjectTarget: null,
  currentProject: null,
  currentRun: null,
  groupingMode: false,
  groupIndex: null,
  groupDialogIndices: [],
  batchGrouping: false,
  selectedIndices: new Set(),
  selectionAnchor: null,
  batchBusy: false,
  filter: "all",
  dialogIndex: null,
  rollbackId: null,
  developPlan: null,
  developStage: "crop",
  exportTargets: { xmp: true, jpeg: false },
  exportSpec: null,
  styleLibrary: null,
  styleScope: "global",
  styleSearchGroupId: null,
  developBusy: false,
  developProgress: null,
  developFlowProgress: null,
  lightroomStatus: null,
  lightroomBusy: false,
  lightroomMessage: null,
  lightroomAction: null,
  styleImportBusy: false,
  styleLibraryBusy: false,
  styleLibraryItemBusy: null,
  styleLibrarySection: "home",
  modelResources: null,
  modelProfile: null,
  modelResourceBusy: null,
  modelInstallHeartbeat: null,
  settingsTransferBusy: false,
  handledJobs: new Set(),
};

let mutationTokenRefresh = null;

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
const STYLE_AMOUNT_TIERS = [50, 100, 150];

function desktopInvoke() {
  const invoke = window.__TAURI__?.core?.invoke;
  return typeof invoke === "function" ? invoke : null;
}

async function ensureDesktopContentRoot(profileId = null) {
  if (state.bootstrap?.system?.content_root_configured !== false) return true;
  const invoke = desktopInvoke();
  if (!invoke) throw new Error("桌面数据目录尚未配置。 ");
  const selected = await invoke("ensure_content_root", { profileId });
  if (!selected) {
    toast("已取消选择，尚未下载或写入任何模型。 ");
    return false;
  }
  return true;
}

function bindDesktopFolderPickers() {
  const invoke = desktopInvoke();
  $$(".desktop-folder-picker").forEach((button) => {
    button.classList.toggle("hidden", !invoke);
    if (!invoke) return;
    button.addEventListener("click", async () => {
      const input = document.getElementById(button.dataset.folderTarget || "");
      if (!input || button.disabled) return;
      button.disabled = true;
      try {
        const selected = await invoke("pick_folder", {
          initialDirectory: input.value.trim() || null,
        });
        if (!selected) return;
        input.value = selected;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        if (input.id === "export-directory") state.exportSpec = null;
        input.focus();
      } catch (error) {
        toast(error?.message || String(error));
      } finally {
        button.disabled = false;
      }
    });
  });
}

function formatBytes(value) {
  let size = Number(value) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function progressNumber(source, ...keys) {
  for (const key of keys) {
    const value = source?.[key];
    if (value === null || value === undefined || value === "" || typeof value === "boolean") continue;
    const number = Number(value);
    if (Number.isFinite(number)) return Math.max(0, number);
  }
  return null;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total} 秒`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours
    ? `${hours} 小时 ${String(minutes).padStart(2, "0")} 分`
    : `${minutes} 分 ${String(remainder).padStart(2, "0")} 秒`;
}

function modelInstallProgressSnapshot(job, now = Date.now()) {
  if (job?.kind !== "model_download") return null;
  const progress = job.progress && typeof job.progress === "object" ? job.progress : {};
  const overallValue = progressNumber(progress, "overall_percent", "overall");
  const overall = overallValue === null ? null : Math.min(100, overallValue);
  const current = progressNumber(progress, "current");
  const total = progressNumber(progress, "total");
  const downloadedBytes = progressNumber(progress, "downloaded_bytes", "current_bytes", "bytes_downloaded")
    ?? (progress.unit === "B" ? current : null);
  const totalBytes = progressNumber(progress, "total_bytes", "bytes_total")
    ?? (progress.unit === "B" ? total : null);
  const bytesPerSecond = progressNumber(progress, "bytes_per_second", "download_speed_bps", "speed_bps");
  const etaSeconds = progressNumber(progress, "eta_seconds", "remaining_seconds")
    ?? (bytesPerSecond && totalBytes !== null && downloadedBytes !== null
      ? Math.max(0, (totalBytes - downloadedBytes) / bytesPerSecond)
      : null);
  const resumedBytes = progressNumber(progress, "resumed_bytes", "resume_bytes");
  let stagePercent = total !== null && total > 0
    ? progressNumber(progress, "stage_percent")
    : null;
  if (stagePercent === null && totalBytes && downloadedBytes !== null) stagePercent = downloadedBytes / totalBytes * 100;
  if (stagePercent === null && total && current !== null) stagePercent = current / total * 100;
  if (stagePercent !== null) stagePercent = Math.min(100, stagePercent);
  const startedAt = Date.parse(job.started_at || job.created_at || "");
  const finishedAt = Date.parse(job.finished_at || "");
  const elapsedSeconds = Number.isFinite(startedAt)
    ? Math.max(0, ((Number.isFinite(finishedAt) ? finishedAt : now) - startedAt) / 1000)
    : progressNumber(progress, "elapsed_seconds") || 0;
  const resource = String(progress.current_resource || progress.resource_label || progress.resource || "").trim();
  const detail = String(progress.detail || progress.action || job.message || "").trim();
  const signature = JSON.stringify([
    job.id, job.status, progress.stage_key, progress.stage_label, detail, resource,
    downloadedBytes, totalBytes, current, total, overall,
  ]);
  if (!state.modelInstallHeartbeat || state.modelInstallHeartbeat.id !== job.id || state.modelInstallHeartbeat.signature !== signature) {
    state.modelInstallHeartbeat = { id: job.id, signature, at: now };
  }
  const reportedUpdate = Date.parse(progress.heartbeat_at || progress.updated_at || job.updated_at || "");
  const heartbeatAt = Math.max(
    state.modelInstallHeartbeat.at,
    Number.isFinite(reportedUpdate) ? reportedUpdate : 0,
  );
  return {
    stageKey: String(progress.stage_key || progress.phase || ""),
    overall,
    stagePercent,
    stage: String(progress.stage_label || progress.phase_label || progress.phase || job.stage || (job.status === "queued" ? "等待开始" : "安装环境")),
    detail,
    resource,
    downloadedBytes,
    totalBytes,
    bytesPerSecond,
    etaSeconds,
    resumedBytes,
    elapsedSeconds,
    heartbeatAgeSeconds: Math.max(0, (now - heartbeatAt) / 1000),
    nodes: Array.isArray(progress.nodes) ? progress.nodes : [],
  };
}

function renderModelInstallProgress(job = state.activeJob || state.jobNotice) {
  const snapshot = modelInstallProgressSnapshot(job);
  const visible = Boolean(snapshot && ["queued", "running", "cancelling", "failed", "interrupted"].includes(job.status));
  $$("[data-model-install-target]").forEach((panel) => {
    panel.classList.add("hidden");
    panel.replaceChildren();
    panel.closest(".model-resource-row")?.classList.remove("installing", "install-failed");
  });
  if (!visible) return;
  const modelIds = new Set((state.modelResources?.resources || []).map((item) => item.id));
  const currentResource = (state.modelResources?.resources || []).find((item) => item.label === snapshot.resource || item.id === snapshot.resource);
  const target = modelIds.has(snapshot.stageKey) ? snapshot.stageKey : currentResource?.id || "runtime";
  const panel = document.querySelector(`[data-model-install-target="${CSS.escape(target)}"]`);
  if (!panel) return;
  const failed = ["failed", "interrupted"].includes(job.status);
  const cancelling = job.status === "cancelling";
  const row = panel.closest(".model-resource-row");
  row?.classList.add("installing");
  row?.classList.toggle("install-failed", failed);
  panel.classList.toggle("failed", failed);
  const age = Math.round(snapshot.heartbeatAgeSeconds);
  const detail = failed
    ? (job.message || `${snapshot.stage}未完成`)
    : cancelling
      ? "正在停止，已经完成的下载会保留"
      : snapshot.detail || (job.status === "queued" ? "正在启动安装进程" : "等待下一项安装信息");
  const metrics = [];
  metrics.push(failed ? "失败" : snapshot.overall === null ? "进行中" : `${Math.round(snapshot.overall)}%`);
  if (snapshot.downloadedBytes !== null) metrics.push(`${formatBytes(snapshot.downloadedBytes)}${snapshot.totalBytes ? ` / ${formatBytes(snapshot.totalBytes)}` : ""}`);
  if (snapshot.bytesPerSecond !== null) metrics.push(`${formatBytes(snapshot.bytesPerSecond)}/s`);
  if (snapshot.etaSeconds !== null) metrics.push(`剩余 ${formatDuration(snapshot.etaSeconds)}`);
  if (snapshot.resumedBytes > 0) metrics.push(`续传 ${formatBytes(snapshot.resumedBytes)}`);
  metrics.push(`已运行 ${formatDuration(snapshot.elapsedSeconds)}`);
  if (!failed && age >= 10) metrics.push(`${age} 秒未更新`);
  const percent = snapshot.stagePercent ?? snapshot.overall;
  const retryButton = failed && job.retryable
    ? `<button type="button" class="text-button" data-model-install-retry="${escapeHtml(job.id || "")}" ${state.jobRetryBusy ? "disabled" : ""}>${state.jobRetryBusy ? "正在重试…" : "重试"}</button>`
    : "";
  panel.innerHTML = `
    <div class="model-resource-progress-copy"><strong>${escapeHtml(snapshot.stage)}</strong><span title="${escapeHtml(detail)}">${escapeHtml(detail)}</span></div>
    <div class="model-resource-progress-actions"><div class="model-resource-progress-meta">${metrics.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</div>${retryButton}<button type="button" class="text-button" ${failed ? "data-model-install-dismiss" : "data-model-install-cancel"} ${cancelling ? "disabled" : ""}>${failed ? "关闭" : cancelling ? "停止中…" : "取消"}</button></div>
    <div class="model-resource-progress-track ${percent === null && !failed ? "indeterminate" : ""}" role="progressbar" aria-label="${escapeHtml(snapshot.stage)}" aria-valuemin="0" aria-valuemax="100"${percent === null ? "" : ` aria-valuenow="${Math.round(percent)}"`}><span${percent === null ? "" : ` style="width:${percent}%"`}></span></div>`;
  panel.classList.remove("hidden");
}

function modelSetupStatus() {
  const data = state.modelResources;
  const active = (data?.profiles || []).find((item) => item.configured) || null;
  if (active?.ready) return { ready: true, message: `${active.label} 模型与运行组件校验通过。`, profile: active };
  if (!data) return { ready: false, message: "无法读取模型资源，请进入设置检查。", profile: null };
  if (!active) return { ready: false, message: "选择 8GB 或 16GB 套装，一键安装运行组件和全部模型。", profile: null };
  const verified = Number(active.verified_count || 0);
  return { ready: false, message: `${active.label} 尚未完整：${verified}/${Number(active.model_count || 0)} 个模型校验通过。`, profile: active };
}

function renderModelSetupState() {
  const status = modelSetupStatus();
  const gate = $("#model-setup-gate");
  const projectGate = $("#project-model-setup-gate");
  gate.classList.toggle("hidden", status.ready);
  projectGate.classList.toggle("hidden", status.ready);
  $("#model-setup-copy").textContent = status.message;
  $("#project-model-setup-copy").textContent = status.message;
  const createButton = $("#cull-form button[type=submit]");
  createButton.disabled = state.projectBusy || !status.ready;
  const groupButton = $("#project-group-start");
  groupButton.disabled = Boolean(state.activeJob) || !status.ready;
  return status.ready;
}

async function requireModelSetupUi() {
  if (modelSetupStatus().ready) return true;
  toast(modelSetupStatus().message);
  await openModelResources("push");
  return false;
}

async function refreshMutationToken() {
  if (!mutationTokenRefresh) {
    mutationTokenRefresh = (async () => {
      const response = await fetch("/api/bootstrap", { cache: "no-store" });
      if (!response.ok) throw new Error("本地服务正在重新连接，请稍后再试。");
      const payload = await response.json();
      if (!payload?.token) throw new Error("本地服务正在重新连接，请稍后再试。");
      state.token = payload.token;
      return payload.token;
    })().finally(() => { mutationTokenRefresh = null; });
  }
  return mutationTokenRefresh;
}

async function api(path, options = {}) {
  const retryCount = Number(options.__tokenRetryCount || 0);
  const { __tokenRetryCount: _ignoredRetryCount, ...requestOptions } = options;
  const request = { ...requestOptions, headers: { ...(requestOptions.headers || {}) } };
  if (options.body && typeof options.body !== "string") {
    request.body = JSON.stringify(options.body);
    request.headers["Content-Type"] = "application/json";
  }
  const mutation = (request.method || "GET") !== "GET";
  if (mutation) request.headers["X-Photo-AI-Token"] = state.token;
  const response = await fetch(path, request);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = payload?.detail;
    const detailObject = detail && typeof detail === "object" && !Array.isArray(detail) ? detail : null;
    const message = Array.isArray(detail) ? detail.map((item) => item.msg).join("；") : detailObject?.message || detail || payload || `请求失败 (${response.status})`;
    const tokenExpired = mutation && response.status === 403 && /操作令牌.*失效/.test(String(message));
    if (tokenExpired && retryCount < 2) {
      await refreshMutationToken();
      return api(path, { ...requestOptions, __tokenRetryCount: retryCount + 1 });
    }
    if (tokenExpired) throw new Error("本地服务正在重新连接，请稍后再试。");
    if (detailObject?.code === "model_profile_incomplete") {
      queueMicrotask(() => openModelResources("push"));
    }
    if (detailObject?.code === "content_root_required" && desktopInvoke()) {
      const selected = await ensureDesktopContentRoot(null);
      if (!selected) throw new Error("需要先选择数据目录才能继续。 ");
      throw new Error("正在切换到正式数据目录，请稍候。 ");
    }
    throw new Error(message);
  }
  return payload;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 3200);
}

function renderSettingsTransferStatus(message = "", kind = "") {
  const status = $("#settings-transfer-status");
  status.textContent = message;
  status.classList.toggle("hidden", !message);
  status.classList.toggle("success", kind === "success");
  status.classList.toggle("error", kind === "error");
  $("#settings-export").disabled = state.settingsTransferBusy;
  $("#settings-import-open").disabled = state.settingsTransferBusy;
}

async function exportSettingsFile() {
  if (state.settingsTransferBusy) return;
  state.settingsTransferBusy = true;
  renderSettingsTransferStatus("正在整理可迁移设置…");
  try {
    const response = await fetch("/api/settings-transfer/export", { cache: "no-store" });
    const content = await response.text();
    if (!response.ok) {
      let message = content || "无法导出设置。";
      try { message = JSON.parse(content)?.detail || message; } catch (_error) { /* plain error */ }
      throw new Error(message);
    }
    const blob = new Blob([content], { type: "application/vnd.photoai.settings+json;charset=utf-8" });
    const link = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 10).replaceAll("-", "");
    link.href = URL.createObjectURL(blob);
    link.download = `PhotoAI-Settings-${stamp}.photoai-settings`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    renderSettingsTransferStatus("设置文件已导出。", "success");
  } catch (error) {
    renderSettingsTransferStatus(error.message, "error");
    toast(error.message);
  } finally {
    state.settingsTransferBusy = false;
    renderSettingsTransferStatus($("#settings-transfer-status").textContent, $("#settings-transfer-status").classList.contains("error") ? "error" : "success");
  }
}

async function importSettingsFile(file) {
  if (!file || state.settingsTransferBusy) return;
  if (!file.name.toLowerCase().endsWith(".photoai-settings")) return toast("请选择 .photoai-settings 文件。 ");
  if (file.size > 256 * 1024) return toast("设置文件不能超过 256 KB。 ");
  state.settingsTransferBusy = true;
  renderSettingsTransferStatus("正在校验并导入设置…");
  try {
    let settings;
    try { settings = JSON.parse(await file.text()); } catch (_error) { throw new Error("设置文件不是有效的 JSON。 "); }
    const result = await api("/api/settings-transfer/import", {
      method: "POST",
      body: { settings },
    });
    state.modelProfile = result.settings?.model_profile_preference || null;
    const exportDefaults = result.settings?.export_defaults || {};
    state.exportTargets = {
      xmp: exportDefaults.xmp !== false,
      jpeg: exportDefaults.jpeg === true,
    };
    await refreshBootstrap();
    const ignored = Number(result.ignored_fields?.length || 0);
    const message = `设置已导入，硬件和 Lightroom 已重新检测${ignored ? ` · 已忽略 ${ignored} 项机器数据` : ""}；模型不会自动下载。`;
    renderSettingsTransferStatus(message, "success");
    toast("设置已导入");
  } catch (error) {
    renderSettingsTransferStatus(error.message, "error");
    toast(error.message);
  } finally {
    $("#settings-import-file").value = "";
    state.settingsTransferBusy = false;
    const status = $("#settings-transfer-status");
    renderSettingsTransferStatus(status.textContent, status.classList.contains("error") ? "error" : "success");
  }
}

function setView(name, route = name, historyMode = "replace") {
  const previousView = document.querySelector(".view.active")?.id || "";
  const previousHash = location.hash;
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${name}`));
  const activeTab = ["project", "review", "develop", "export"].includes(name) ? "cull" : name === "resources" ? "settings" : name;
  $$(".tab").forEach((node) => node.classList.toggle("active", node.dataset.view === activeTab));
  const target = `#${route}`;
  if (location.hash !== target) history[historyMode === "push" ? "pushState" : "replaceState"](null, "", target);
  if (previousView !== `view-${name}` || previousHash !== target) window.scrollTo(0, 0);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function renderAlerts() {
  const system = state.bootstrap?.system;
  const messages = [];
  if (system?.photos_online === false) messages.push("默认照片目录当前不可用");
  const bar = $("#alert-bar");
  bar.textContent = messages.join(" · ");
  bar.classList.toggle("hidden", messages.length === 0);
}

function projectRow(project) {
  const latest = project.latest_result || {};
  const hasRun = Boolean(project.latest_run_id);
  const xmp = project.xmp_count ? ` · ${project.xmp_count} 个 XMP` : "";
  const excluded = Number(latest.excluded_count ?? project.excluded_count ?? 0);
  const active = Number(latest.active_image_count ?? project.active_image_count ?? project.image_count ?? 0);
  const photoCount = hasRun ? (excluded ? `${active} 张 · 移出 ${excluded} 张` : `${project.image_count} 张`) : "尚未分析";
  const status = !hasRun
    ? "待分组"
    : latest.workflow_state === "grouped"
    ? `${latest.group_count ?? 0} 组 · 待评分`
    : latest.needs_rescore
      ? `${latest.group_count ?? 0} 组 · 待重新评分`
      : `${latest.strong_count ?? 0} 强推荐 · ${latest.candidate_count ?? 0} 候选`;
  return `<article class="project-row">
    <div class="row-main"><strong>${escapeHtml(project.name)}</strong><small title="${escapeHtml(project.input_root)}">${escapeHtml(project.input_root)}</small></div>
    <span class="row-stat">${photoCount}${xmp}</span>
    <span class="row-stat">${status}</span>
    <div class="row-actions">
      <button class="button open-project" data-project-id="${escapeHtml(project.project_id)}">打开</button>
      <button class="button danger-quiet delete-project-open" data-project-id="${escapeHtml(project.project_id)}">删除</button>
    </div>
  </article>`;
}

function transactionRow(item) {
  const status = item.rollbackable ? `剩余 ${item.remaining_count} · 可回滚` : "已结束";
  return `<article class="transaction-row">
    <div class="row-main"><strong>正式写入 ${item.created_count} 个 XMP</strong><small>${formatDate(item.created_at)} · ${status}</small></div>
    <div class="row-actions">${item.rollbackable ? `<button class="button rollback-open" data-transaction-id="${escapeHtml(item.id)}">回滚</button>` : ""}</div>
  </article>`;
}

function renderProjects() {
  const markup = state.projects.length ? state.projects.map(projectRow).join("") : `<div class="empty">暂无工程</div>`;
  $("#recent-projects").innerHTML = markup;
  $("#toolbox-xmp").classList.toggle("hidden", state.transactions.length === 0);
  $("#toolbox-xmp-empty").classList.toggle("hidden", state.transactions.length !== 0);
  $("#toolbox-transactions").innerHTML = state.transactions.map((item) => transactionRow(item)).join("");
}

function rawJpegDirectionLabel(value) {
  return value === "raw" ? "以 RAW 为准" : value === "both" ? "双向清理" : "以成片为准";
}

function renderRawJpegLayout() {
  const separate = $("#raw-jpeg-layout").value === "separate";
  $("#raw-jpeg-mixed-field").classList.toggle("hidden", separate);
  $("#raw-jpeg-raw-field").classList.toggle("hidden", !separate);
  $("#raw-jpeg-jpeg-field").classList.toggle("hidden", !separate);
  $("#raw-jpeg-mixed-path").required = !separate;
  $("#raw-jpeg-raw-path").required = separate;
  $("#raw-jpeg-jpeg-path").required = separate;
}

function toolboxTransactionRow(item) {
  const issues = [
    item.conflict_count ? `冲突 ${item.conflict_count}` : "",
    item.modified_count ? `已变化 ${item.modified_count}` : "",
    item.lost_count ? `未找到 ${item.lost_count}` : "",
  ].filter(Boolean).join(" · ");
  const status = item.needs_attention
    ? `需要处理：${issues}`
    : item.rollbackable
      ? `回收区剩余 ${item.remaining_count} 个文件`
      : item.status === "rolled_back" ? `已撤销 · 恢复 ${item.restored_count} 个文件` : `已结束`;
  const xmpCopy = item.xmp_count ? ` · 含 ${item.xmp_count} 个 XMP` : "";
  return `<article class="transaction-row">
    <div class="row-main"><strong>${rawJpegDirectionLabel(item.direction)}</strong><small>${formatDate(item.created_at)} · ${status}${xmpCopy}${item.failed_count ? ` · 失败 ${item.failed_count}` : ""}</small></div>
    <div class="row-actions">${item.rollbackable ? `<button class="button raw-jpeg-rollback" data-transaction-id="${escapeHtml(item.transaction_id)}">撤销</button>` : ""}</div>
  </article>`;
}

function openToolboxSection(section = "home", historyMode = "push") {
  const validSections = new Set(["home", "raw-jpeg", "xmp-cleanup", "xmp-records"]);
  state.toolboxSection = validSections.has(section) ? section : "home";
  $$('[data-toolbox-panel]').forEach((node) => node.classList.toggle("hidden", node.dataset.toolboxPanel !== state.toolboxSection));
  const route = state.toolboxSection === "home" ? "toolbox" : `toolbox/${state.toolboxSection}`;
  setView("toolbox", route, historyMode);
  renderToolbox();
}

function renderToolbox() {
  const countCopy = (count, emptyCopy, populatedCopy) => count ? `${count} 条${populatedCopy}` : emptyCopy;
  $("#toolbox-raw-jpeg-count").textContent = countCopy(state.toolboxTransactions.length, "暂无操作记录", "操作记录");
  $("#toolbox-xmp-cleanup-count").textContent = countCopy(state.xmpCleanupTransactions.length, "暂无清除记录", "清除记录");
  $("#toolbox-xmp-record-count").textContent = countCopy(state.transactions.length, "暂无写入记录", "写入记录");
  renderRawJpegLayout();
  const previewButton = $("#raw-jpeg-preview");
  previewButton.disabled = state.toolboxBusy || state.xmpCleanupBusy || Boolean(state.activeJob);
  previewButton.textContent = state.toolboxBusy ? "正在扫描…" : "扫描预览";
  const plan = state.rawJpegPlan;
  const result = $("#raw-jpeg-result");
  if (!plan) {
    result.replaceChildren();
    result.classList.add("hidden");
  } else {
    const candidates = plan.candidates || [];
    const warnings = plan.warnings || [];
    const scanErrors = plan.scan_errors || [];
    const files = candidates.length ? `<div class="tool-file-list">${candidates.map((item) => `<div class="tool-file-row"><span class="tool-side">${item.side === "raw" ? "RAW" : "成片"}</span><code title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</code><small>${escapeHtml(item.relative_path)}${item.sidecars?.length ? ` · 同步 ${item.sidecars.length} 个 XMP` : ""}</small></div>`).join("")}</div>` : "";
    const warningMarkup = warnings.length ? `<details class="tool-warnings"><summary>${warnings.length} 条已跳过提醒</summary><div class="tool-warning-list">${warnings.slice(0, 100).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div></details>` : "";
    const scanErrorMarkup = scanErrors.length ? `<details class="tool-warnings" open><summary>${scanErrors.length} 个目录无法完整读取，本计划禁止执行</summary><div class="tool-warning-list">${scanErrors.slice(0, 100).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div></details>` : "";
    result.innerHTML = `<div class="panel tool-summary"><div class="detail-grid">
      <span><strong>${plan.paired_count}</strong><br>成对文件名</span>
      <span><strong>${plan.raw_only_count}</strong><br>仅 RAW</span>
      <span><strong>${plan.jpeg_only_count}</strong><br>仅成片</span>
      <span><strong>${plan.candidate_count}</strong><br>待移入回收区</span>
    </div></div>
    <div class="tool-plan-head"><span class="tool-plan-copy">${rawJpegDirectionLabel(plan.direction)} · ${formatBytes(plan.candidate_bytes)}${plan.sidecar_count ? ` · 同步 ${plan.sidecar_count} 个 XMP` : ""}${plan.candidates_truncated ? " · 列表仅显示前 500 个" : ""}</span>${plan.candidate_count && plan.complete ? `<button class="button danger raw-jpeg-execute-open" ${state.activeJob ? "disabled" : ""}>执行整理</button>` : `<span class="tool-plan-copy">${plan.complete ? "无需处理" : "扫描不完整"}</span>`}</div>
    ${files}${scanErrorMarkup}${warningMarkup}`;
    result.classList.remove("hidden");
  }
  const history = $("#raw-jpeg-history");
  history.classList.toggle("hidden", state.toolboxTransactions.length === 0);
  $("#raw-jpeg-transactions").innerHTML = state.toolboxTransactions.map(toolboxTransactionRow).join("");
  renderXmpCleanup();
}

function xmpCleanupTransactionRow(item) {
  const removedCount = Number(item.removed_count ?? item.moved_count ?? item.xmp_count ?? 0);
  const permanentlyDeleted = item.operation === "delete";
  const issues = [
    item.conflict_count ? `冲突 ${item.conflict_count}` : "",
    item.modified_count ? `已变化 ${item.modified_count}` : "",
    item.lost_count ? `未找到 ${item.lost_count}` : "",
  ].filter(Boolean).join(" · ");
  const status = item.needs_attention
    ? `需要处理：${issues}`
    : permanentlyDeleted
      ? item.failed_count ? `已永久删除 ${removedCount} 个，失败 ${item.failed_count} 个` : `已永久删除 ${removedCount} 个 XMP`
    : item.rollbackable
      ? `回收区剩余 ${item.remaining_count} 个 XMP`
      : item.status === "rolled_back" ? `已恢复 ${item.restored_count} 个 XMP` : "已结束";
  return `<article class="transaction-row">
    <div class="row-main"><strong>${permanentlyDeleted ? "永久删除" : "清除"} ${removedCount} 个 XMP</strong><small>${formatDate(item.created_at)} · ${status}${!permanentlyDeleted && item.failed_count ? ` · 失败 ${item.failed_count}` : ""}</small></div>
    <div class="row-actions">${!permanentlyDeleted && item.rollbackable ? `<button class="button xmp-cleanup-rollback" data-transaction-id="${escapeHtml(item.transaction_id)}">恢复</button>` : ""}</div>
  </article>`;
}

function renderXmpCleanup() {
  const previewButton = $("#xmp-cleanup-preview");
  previewButton.disabled = state.xmpCleanupBusy || state.toolboxBusy || Boolean(state.activeJob);
  previewButton.textContent = state.xmpCleanupBusy ? "正在扫描…" : "扫描 XMP";
  const plan = state.xmpCleanupPlan;
  const result = $("#xmp-cleanup-result");
  if (!plan) {
    result.replaceChildren();
    result.classList.add("hidden");
  } else {
    const candidates = plan.candidates || [];
    const scanErrors = plan.scan_errors || [];
    const xmpCount = Number(plan.xmp_count ?? candidates.length);
    const xmpBytes = Number(plan.xmp_bytes ?? 0);
    const files = candidates.length ? `<div class="tool-file-list">${candidates.map((item) => {
      const relative = String(item.relative_path || "");
      const sizeCopy = formatBytes(Number(item.size ?? item.bytes ?? 0));
      return `<div class="tool-file-row"><span class="tool-side">XMP</span><code title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</code><small>${relative ? `${escapeHtml(relative)} · ` : ""}${sizeCopy}</small></div>`;
    }).join("")}</div>` : "";
    const scanErrorMarkup = scanErrors.length ? `<details class="tool-warnings" open><summary>${scanErrors.length} 个目录无法完整读取，本计划禁止执行</summary><div class="tool-warning-list">${scanErrors.slice(0, 100).map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div></details>` : "";
    const action = xmpCount && plan.complete
      ? `<button class="button danger xmp-cleanup-execute-open" ${state.activeJob ? "disabled" : ""}>永久删除 XMP</button>`
      : `<span class="tool-plan-copy">${plan.complete ? "未找到 XMP" : "扫描不完整"}</span>`;
    result.innerHTML = `<div class="panel tool-summary"><div class="detail-grid">
      <span><strong>${xmpCount}</strong><br>找到 XMP</span>
      <span><strong>${formatBytes(xmpBytes)}</strong><br>总大小</span>
    </div></div>
    <div class="tool-plan-head"><span class="tool-plan-copy">将永久删除，不可恢复${plan.candidates_truncated ? " · 列表仅显示前 500 个" : ""}</span>${action}</div>
    ${files}${scanErrorMarkup}`;
    result.classList.remove("hidden");
  }
  const history = $("#xmp-cleanup-history");
  history.classList.toggle("hidden", state.xmpCleanupTransactions.length === 0);
  $("#xmp-cleanup-transactions").innerHTML = state.xmpCleanupTransactions.map(xmpCleanupTransactionRow).join("");
}

async function openProject(projectId, updateRoute = true) {
  const project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
  state.currentProject = project;
  const route = updateRoute ? `project/${projectId}` : location.hash.replace(/^#/, "") || `project/${projectId}`;
  if (project.latest_run_id) {
    await openRun(project.latest_run_id, route, updateRoute ? "push" : "replace");
    return;
  }
  state.currentRun = null;
  renderProjectStart();
  setView("project", route, updateRoute ? "push" : "replace");
}

function renderProjectStart() {
  const project = state.currentProject;
  if (!project) return;
  $("#project-start-title").textContent = project.name || "新工程";
  $("#project-start-path").textContent = project.input_root || "";
  const button = $("#project-group-start");
  button.disabled = Boolean(state.activeJob) || !modelSetupStatus().ready;
  button.textContent = state.activeJob?.kind === "group" ? "正在分析…" : "开始分析分组";
  renderModelSetupState();
}

function renderLightroom() {
  const box = $("#lightroom-detail");
  const status = state.lightroomStatus;
  const refreshButton = $("#lightroom-refresh");
  const autoButton = $("#lightroom-auto-configure");
  const manualButton = $("#lightroom-manual-configure");
  refreshButton.disabled = state.lightroomBusy;
  autoButton.disabled = state.lightroomBusy;
  manualButton.disabled = state.lightroomBusy || !$("#lightroom-path").value.trim();
  refreshButton.textContent = state.lightroomBusy && state.lightroomAction === "refresh" ? "正在刷新…" : "刷新连接";
  autoButton.textContent = state.lightroomBusy && state.lightroomAction === "auto" ? "正在配置…" : "自动识别并配置";
  manualButton.textContent = state.lightroomBusy && state.lightroomAction === "manual" ? "正在配置…" : "按此路径配置";
  const message = $("#lightroom-config-status");
  if (state.lightroomMessage?.text) {
    message.textContent = state.lightroomMessage.text;
    message.classList.toggle("success", state.lightroomMessage.kind === "success");
    message.classList.toggle("error", state.lightroomMessage.kind === "error");
    message.classList.remove("hidden");
  } else {
    message.textContent = "";
    message.classList.add("hidden");
    message.classList.remove("success", "error");
  }
  if (!status) {
    box.innerHTML = `<strong>Lightroom Classic</strong> · 状态暂不可用`;
    return;
  }
  const lightroom = status.lightroom || {};
  const plugin = status.plugin || {};
  const heartbeat = status.heartbeat || {};
  const online = heartbeat.state === "online";
  const selectedInstall = lightroom.installations?.find((item) => item.path === lightroom.executable) || null;
  const version = heartbeat.lightroom_version || selectedInstall?.version || "版本未知";
  const supportLevel = lightroom.support_level || selectedInstall?.support_level || "incompatible";
  const stateCopy = !lightroom.compatible
    ? "需要 14.3 或更高版本"
    : online ? "桥接已连接" : status.configured ? "插件未连接" : "需要准备插件";
  const supportCopy = lightroom.compatible && supportLevel !== "validated"
    ? online ? " · 插件实际连接已确认" : " · 此版本未完整验证，连接后按实际能力检查"
    : "";
  const catalogPath = typeof heartbeat.catalog_path === "string" ? heartbeat.catalog_path : "";
  const catalogOnSystemDrive = /^c:[\\/]/i.test(catalogPath);
  const locationCopy = online && catalogPath
    ? catalogOnSystemDrive
      ? `当前 Lightroom 目录位于 C 盘：${escapeHtml(catalogPath)}`
      : `当前 Lightroom 目录：${escapeHtml(catalogPath)}`
    : online ? "基础校准会直接进入当前 Lightroom 目录" : "插件安装在 Lightroom 用户插件目录，配置一次即可";
  const storageWarning = catalogOnSystemDrive
    ? `<br><small class="warning">目录和预览可能占用 C 盘，请先在 Lightroom 中迁移目录。</small>`
    : `<br><small>本工具数据位于你选择的数据目录；Lightroom 自身目录、预览和 Camera Raw 缓存位置由 Lightroom 设置决定。</small>`;
  box.innerHTML = `<div class="integration-row"><span><strong>Lightroom Classic ${escapeHtml(version)}</strong> · ${stateCopy}${supportCopy}<br><small title="${escapeHtml(catalogPath || plugin.source_dir || "")}">${locationCopy}</small>${storageWarning}</span></div>`;
  const configuredPath = status.settings?.executable_path || lightroom.executable || "";
  if (document.activeElement !== $("#lightroom-path")) $("#lightroom-path").value = configuredPath;
  manualButton.disabled = state.lightroomBusy || !$("#lightroom-path").value.trim();
}

function renderStyleLibrary() {
  const library = state.styleLibrary;
  const summary = $("#settings-style-summary");
  const files = [...$("#style-library-files").files];
  const importButton = $("#style-library-import");
  importButton.disabled = state.styleImportBusy || files.length === 0;
  importButton.textContent = state.styleImportBusy ? "正在上传…" : files.length ? `上传 ${files.length} 个文件` : "上传所选";
  if (!library) {
    summary.textContent = "尚未同步";
    renderStyleLibraryManager();
    return;
  }
  const sources = library.sources || {};
  summary.textContent = `${Number(sources.lightroom?.count || 0)} 个 Lightroom 预设 · ${Number(sources.user?.count || 0)} 个用户风格`;
  renderStyleLibraryManager();
}

function styleSourceLabel(item) {
  if (item.source_group === "user") return item.look_kind === "rendered_lut" ? "用户 LUT" : "用户上传";
  return "Lightroom 自动识别";
}

function renderStyleLibraryManager() {
  const list = $("#style-library-manager-list");
  if (!list) return;
  const library = state.styleLibrary || {};
  const sources = library.sources || {};
  const section = state.styleLibrarySection;
  const lightroom = sources.lightroom || { count: 0, enabled: true, ai_pool: 0 };
  const user = sources.user || { count: 0, enabled: true, ai_pool: 0 };
  $("#style-library-manager-home").classList.toggle("hidden", section !== "home");
  $("#style-library-source-view").classList.toggle("hidden", section === "home");
  $("#style-library-manager-summary").textContent = `${Number(lightroom.count || 0) + Number(user.count || 0)} 个风格 · 2 个来源`;
  [["lightroom", lightroom], ["user", user]].forEach(([key, source]) => {
    const node = $(`#style-source-${key}-state`);
    node.textContent = `${Number(source.count || 0)} 个 · ${source.enabled ? "已启用" : "已停用"}`;
    node.classList.toggle("enabled", Boolean(source.enabled));
  });
  if (section === "home") return;

  const isUser = section === "user";
  const source = isUser ? user : lightroom;
  $("#style-library-source-title").textContent = isUser ? "用户上传" : "从 Lightroom 导入";
  $("#style-library-source-summary").textContent = `${Number(source.count || 0)} 个风格 · ${source.enabled ? "整类已启用" : "整类已停用"}${Number(source.ai_pool || 0) ? ` · ${Number(source.ai_pool)} 个可供 AI 推荐` : ""}`;
  const toggle = $("#style-library-source-toggle");
  toggle.disabled = state.styleLibraryBusy;
  toggle.textContent = state.styleLibraryBusy ? "正在更新…" : source.enabled ? "停用整类" : "启用整类";
  toggle.dataset.source = section;
  toggle.dataset.enabled = source.enabled ? "false" : "true";
  const rescan = $("#style-library-source-rescan");
  rescan.classList.toggle("hidden", isUser);
  rescan.disabled = state.styleLibraryBusy;
  rescan.textContent = state.styleLibraryBusy ? "正在扫描…" : "重新扫描 Lightroom";
  $("#style-library-user-import").classList.toggle("hidden", !isUser);

  const query = $("#style-library-manager-search").value.trim().toLowerCase();
  const all = (library.presets || []).filter((item) => item.source_group === section);
  const matches = all.filter((item) => {
    const haystack = [item.label, item.source, item.group, item.category].filter(Boolean).join(" ").toLowerCase();
    return !query || haystack.includes(query);
  });
  const shown = matches.slice(0, 300);
  list.innerHTML = shown.map((item) => {
    const resourceId = escapeHtml(item.resource_id || "");
    const busy = state.styleLibraryItemBusy === item.resource_id;
    const toggleAction = `<button type="button" class="button" data-style-library-toggle="${resourceId}" data-hidden="${item.user_hidden ? "false" : "true"}" ${busy ? "disabled" : ""}>${busy ? "处理中…" : item.user_hidden ? "启用" : "停用"}</button>`;
    const deleteAction = isUser && item.can_delete
      ? `<button type="button" class="button danger" data-style-library-delete="${resourceId}" ${busy ? "disabled" : ""}>${busy ? "处理中…" : "删除"}</button>`
      : "";
    const status = item.user_hidden ? "已单独停用" : !source.enabled ? "来源已停用" : item.ai_enabled ? "AI 可用" : item.compatibility === "compatible" ? "已识别" : "不兼容";
    return `<article class="style-library-item ${source.enabled && !item.user_hidden ? "" : "inactive"}"><div class="style-library-item-copy"><strong>${escapeHtml(item.label || "未命名风格")}</strong><small>${escapeHtml(styleSourceLabel(item))} · ${escapeHtml(status)}</small></div><div class="style-library-item-actions">${toggleAction}${deleteAction}</div></article>`;
  }).join("") || `<div class="empty">${query ? "没有匹配的风格" : isUser ? "还没有上传风格" : "尚未扫描到 Lightroom 预设"}</div>`;
}

function selectedModelProfile() {
  const profiles = state.modelResources?.profiles || [];
  const preferred = state.modelProfile
    || state.bootstrap?.preferences?.model_profile_preference
    || state.modelResources?.settings?.active_profile
    || state.modelResources?.recommended_profile;
  const selected = profiles.find((item) => item.id === preferred) || profiles[0] || null;
  if (selected) state.modelProfile = selected.id;
  return selected;
}

function renderContentRootSettings() {
  const path = state.bootstrap?.system?.content_root || "";
  const label = $("#settings-content-root");
  if (label) {
    label.textContent = path || "跟随程序安装位置";
    label.title = path;
  }
  const button = $("#settings-content-root-change");
  if (button) button.classList.toggle("hidden", !desktopInvoke());
}

function renderModelResources() {
  renderModelInstallProgress();
  const data = state.modelResources;
  const settingsSummary = $("#settings-model-summary");
  if (!data) {
    settingsSummary.textContent = "尚未读取";
    return;
  }
  const activeProfile = (data.profiles || []).find((item) => item.configured);
  settingsSummary.textContent = activeProfile
    ? `${activeProfile.label} · ${activeProfile.ready ? "整套校验通过" : "需要补齐或修复"} · ${formatBytes(data.installed_bytes)}`
    : `${data.installed_count} 个模型已下载 · 尚未选择显存档位`;

  const selected = selectedModelProfile();
  if (!selected || !$("#model-profile-list")) return;
  const contentRootConfigured = state.bootstrap?.system?.content_root_configured !== false;
  $("#model-resource-storage").textContent = `模型与下载缓存：${data.runtime_root}`;
  const gpu = data.gpu || {};
  $("#model-gpu-summary").innerHTML = gpu.available
    ? `<strong>${escapeHtml(gpu.name || "NVIDIA GPU")}</strong><span>${Math.round(Number(gpu.memory_total_mib || 0) / 1024)}GB 显存 · 推荐 ${escapeHtml((data.profiles || []).find((item) => item.recommended)?.label || selected.label)}</span>`
    : `<strong>未检测到 NVIDIA 显卡</strong><span>安装模型后仍需可用的 CUDA 环境才能运行 AI。</span>`;

  const taskNote = $("#model-resource-task-note");
  if (state.activeJob?.kind && state.activeJob.kind !== "model_download") {
    taskNote.textContent = `AI 环境尚未开始：当前后台任务“${state.activeJob.title || "未命名任务"}”正在运行。AI 模型安装不依赖 Lightroom；停止当前任务后即可安装。`;
    taskNote.classList.remove("hidden");
    taskNote.classList.add("warning");
    taskNote.classList.remove("error", "success");
  } else if (state.modelResourceMessage) {
    taskNote.textContent = state.modelResourceMessage.text;
    taskNote.classList.remove("hidden");
    taskNote.classList.remove("warning");
    taskNote.classList.toggle("error", state.modelResourceMessage.kind === "error");
    taskNote.classList.toggle("success", state.modelResourceMessage.kind === "success");
  } else {
    taskNote.textContent = "";
    taskNote.classList.add("hidden");
    taskNote.classList.remove("warning", "error", "success");
  }

  const busy = Boolean(state.activeJob) || Boolean(state.modelResourceBusy);
  $("#model-profile-list").innerHTML = (data.profiles || []).map((profile) => {
    const isSelected = profile.id === selected.id;
    const action = profile.configured
      ? (profile.ready ? "重新校验配置" : "补齐并修复")
      : (profile.ready ? "使用此档" : "一键下载并配置");
    return `<article class="model-profile-card ${isSelected ? "selected" : ""}" data-model-profile="${escapeHtml(profile.id)}">
      <div class="model-profile-copy"><div><strong>${escapeHtml(profile.label)}</strong>${profile.recommended ? `<span class="resource-badge recommended">推荐</span>` : ""}${profile.configured ? `<span class="resource-badge active">当前</span>` : ""}</div><p>${escapeHtml(profile.description)}</p><small>${Number(profile.verified_count ?? profile.installed_count)}/${profile.model_count} 个校验通过 · 约 ${formatBytes(profile.estimated_bytes)}</small></div>
      <div class="model-profile-actions">${profile.id === "16gb" ? `<button type="button" class="button primary" data-model-offline-import="16gb" ${busy ? "disabled" : ""}>导入离线包</button>` : ""}<button type="button" class="button ${profile.id === "16gb" || profile.configured || profile.ready ? "" : "primary"}" data-model-configure="${escapeHtml(profile.id)}" ${busy ? "disabled" : ""}>${escapeHtml(action)}</button></div>
    </article>`;
  }).join("");

  const resources = new Map((data.resources || []).map((item) => [item.id, item]));
  const selectedResources = (selected.model_ids || []).map((id) => resources.get(id)).filter(Boolean);
  $("#model-resource-list-title").textContent = `${selected.label} 使用的模型`;
  $("#model-resource-list-summary").textContent = `${Number(selected.verified_count ?? selected.installed_count)}/${selected.model_count} 个校验通过 · 已占用 ${formatBytes(selected.installed_bytes)}`;
  const deleteProfile = $("#model-profile-delete");
  deleteProfile.disabled = busy || selected.installed_count === 0;
  deleteProfile.textContent = state.modelResourceBusy === `profile:${selected.id}` ? "正在删除…" : "删除此档模型";
  deleteProfile.dataset.profileId = selected.id;
  const componentRows = (data.components || []).map((item) => `<article class="model-resource-row" data-model-resource-row="runtime">
    <div class="model-resource-copy"><div><strong>${escapeHtml(item.label)}</strong><span class="resource-badge ${item.verified ? "active" : "missing"}">${item.verified ? "校验通过" : "将自动安装"}</span></div><p>运行本地视觉大模型；配置套装时自动下载到所选数据目录。</p><small>${contentRootConfigured ? `Ollama ${escapeHtml(item.version || "")} · ${escapeHtml(item.path || "")}` : "选择数据目录后自动安装"}</small></div>
    <div class="model-resource-inline-progress hidden" data-model-install-target="runtime" role="status" aria-live="polite"></div>
  </article>`).join("");
  $("#model-resource-list").innerHTML = componentRows + selectedResources.map((item) => `<article class="model-resource-row" data-model-resource-row="${escapeHtml(item.id)}">
    <div class="model-resource-copy"><div><strong>${escapeHtml(item.label)}</strong><span class="resource-badge ${item.verified ? "active" : "missing"}">${item.verified ? "校验通过" : item.installed ? "需要修复" : "未下载"}</span></div><p>${escapeHtml(item.purpose)}</p><small>${escapeHtml(item.source)} · ${item.installed ? formatBytes(item.installed_bytes) : `约 ${formatBytes(item.estimated_bytes)}`}${item.issues?.length ? ` · ${escapeHtml(item.issues[0])}` : ""}</small></div>
    ${item.installed ? `<button type="button" class="button danger-quiet" data-model-delete="${escapeHtml(item.id)}" ${busy ? "disabled" : ""}>删除</button>` : ""}
    <div class="model-resource-inline-progress hidden" data-model-install-target="${escapeHtml(item.id)}" role="status" aria-live="polite"></div>
  </article>`).join("");
  renderModelSetupState();
  renderModelInstallProgress();
}

async function refreshModelResources() {
  state.modelResources = await api("/api/model-resources");
  renderModelResources();
  return state.modelResources;
}

async function openModelResources(historyMode = "push") {
  setView("resources", "settings/resources", historyMode);
  if (!state.modelResources) {
    try { await refreshModelResources(); } catch (error) { toast(error.message); }
  } else {
    renderModelResources();
  }
}

async function configureModelProfile(profileId) {
  if (!profileId || state.activeJob || state.modelResourceBusy) return;
  state.modelProfile = profileId;
  renderModelResources();
  try {
    await startJob("/api/model-resources/configure", { profile_id: profileId });
    renderModelResources();
  } catch (_error) {
    renderModelResources();
  }
}

async function importOfflineModelBundle(profileId) {
  if (profileId !== "16gb" || state.activeJob || state.modelResourceBusy) return;
  const invoke = desktopInvoke();
  if (!invoke) {
    toast("离线资源包只能在桌面安装版中导入。");
    return;
  }
  const contentRootReady = await ensureDesktopContentRoot("16gb");
  if (!contentRootReady) return;
  try {
    const selected = await invoke("pick_offline_bundle");
    if (!selected) return;
    state.modelProfile = "16gb";
    await startJob("/api/model-resources/import-offline", { package_path: selected });
    renderModelResources();
  } catch (error) {
    toast(error?.message || String(error));
    renderModelResources();
  }
}

async function resumePendingModelInstall() {
  const url = new URL(location.href);
  const profileId = String(url.searchParams.get("install_profile") || "").toLowerCase();
  if (!["8gb", "16gb"].includes(profileId)) return false;
  url.searchParams.delete("install_profile");
  history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  await openModelResources("replace");
  await configureModelProfile(profileId);
  return true;
}

async function deleteModelResource(resourceId) {
  const item = (state.modelResources?.resources || []).find((resource) => resource.id === resourceId);
  if (!item || !item.installed || state.activeJob || state.modelResourceBusy) return;
  if (!confirm(`删除 ${item.label}？之后可以重新下载。`)) return;
  state.modelResourceMessage = null;
  state.modelResourceBusy = resourceId;
  renderModelResources();
  try {
    state.modelResources = await api(`/api/model-resources/${encodeURIComponent(resourceId)}`, { method: "DELETE" });
    state.modelResourceMessage = { kind: "success", text: `${item.label}已删除。` };
    toast(`${item.label}已删除`);
  } catch (error) {
    state.modelResourceMessage = { kind: "error", text: error.message };
    toast(error.message);
  } finally {
    state.modelResourceBusy = null;
    renderModelResources();
  }
}

async function deleteSelectedModelProfile() {
  const profile = selectedModelProfile();
  if (!profile || !profile.installed_count || state.activeJob || state.modelResourceBusy) return;
  if (!confirm(`删除 ${profile.label} 使用的已下载模型？共用模型也会删除，之后可以重新下载。`)) return;
  state.modelResourceMessage = null;
  state.modelResourceBusy = `profile:${profile.id}`;
  renderModelResources();
  try {
    state.modelResources = await api(`/api/model-resources/profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE" });
    state.modelResourceMessage = { kind: "success", text: `${profile.label}模型已删除。` };
    toast(`${profile.label}模型已删除`);
  } catch (error) {
    state.modelResourceMessage = { kind: "error", text: error.message };
    toast(error.message);
  } finally {
    state.modelResourceBusy = null;
    renderModelResources();
  }
}

function currentProgressJob() {
  const localProgress = state.developProgress || state.developFlowProgress;
  const foreground = localProgress ? {
    title: localProgress.title || (state.developProgress ? "分析智能构图" : "保存处理设置"),
    status: localProgress.status || "running",
    stage: localProgress.stage_label,
    message: localProgress.message,
    progress: localProgress,
    foreground: true,
    retryable: localProgress.status === "failed" && Boolean(state.currentRun),
    retry_mode: "develop",
    context: { run_id: localProgress.run_id || state.currentRun?.run_id },
  } : null;
  return state.activeJob || foreground || state.jobNotice;
}

function renderJobBar() {
  const job = currentProgressJob();
  renderModelInstallProgress(job);
  const bar = $("#job-bar");
  if (job?.kind === "model_download" && $("#view-resources")?.classList.contains("active")) {
    bar.classList.add("hidden");
    return;
  }
  const active = Boolean(job && ["queued", "running", "cancelling"].includes(job.status));
  const partialFailure = job?.status === "completed" && job?.result?.style_status === "partial";
  const failed = ["failed", "interrupted"].includes(job?.status) || partialFailure;
  if (!job || (!active && !failed)) {
    bar.classList.add("hidden");
    bar.classList.remove("failed");
    $("#job-live-detail").classList.add("hidden");
    return;
  }
  const progress = job.progress && typeof job.progress === "object" ? job.progress : null;
  const numberValue = (value) => {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  const overallValue = numberValue(progress?.overall_percent);
  const overall = overallValue === null ? null : Math.max(0, Math.min(100, overallValue));
  const currentValue = numberValue(progress?.current);
  const totalValue = numberValue(progress?.total);
  const cachedValue = numberValue(progress?.cached);
  const detail = [];
  if (currentValue !== null && totalValue !== null && totalValue > 0) {
    const current = Math.max(0, Math.min(totalValue, currentValue));
    detail.push(progress?.unit === "B"
      ? `${formatBytes(current)} / ${formatBytes(totalValue)}`
      : `${Math.round(current)} / ${Math.round(totalValue)}${progress?.unit ? ` ${progress.unit}` : ""}`);
  }
  if (cachedValue !== null && cachedValue > 0) detail.push(`缓存 ${Math.round(cachedValue)}`);
  const stage = failed
    ? (job.message || job.stage || "任务失败")
    : (progress?.stage_label || job.stage || job.message || (job.status === "queued" ? "等待开始" : "处理中"));
  $("#job-title").textContent = job.title || "后台任务";
  $("#job-stage").textContent = [stage, ...detail].filter(Boolean).join(" · ");
  const live = $("#job-live-detail");
  const installProgress = modelInstallProgressSnapshot(job);
  if (installProgress) {
    const pieces = [];
    if (installProgress.resource) pieces.push(`<span class="job-live-resource">${escapeHtml(installProgress.resource)}</span>`);
    if (installProgress.downloadedBytes !== null) {
      pieces.push(`<span>${escapeHtml(formatBytes(installProgress.downloadedBytes))}${installProgress.totalBytes ? ` / ${escapeHtml(formatBytes(installProgress.totalBytes))}` : ""}</span>`);
    }
    if (installProgress.bytesPerSecond !== null) pieces.push(`<span>${escapeHtml(formatBytes(installProgress.bytesPerSecond))}/s</span>`);
    if (installProgress.etaSeconds !== null) pieces.push(`<span>剩余 ${escapeHtml(formatDuration(installProgress.etaSeconds))}</span>`);
    pieces.push(`<span>已运行 ${escapeHtml(formatDuration(installProgress.elapsedSeconds))}</span>`);
    live.innerHTML = pieces.join("");
    live.classList.remove("hidden");
  } else {
    live.replaceChildren();
    live.classList.add("hidden");
  }

  const percent = $("#job-percent");
  const track = $("#job-progress-track");
  const fill = $("#job-progress-fill");
  percent.textContent = partialFailure ? "部分失败" : failed ? "失败" : (overall === null ? "进行中" : `${Math.round(overall)}%`);
  track.classList.toggle("indeterminate", overall === null);
  if (overall === null) {
    fill.style.width = "";
    track.removeAttribute("aria-valuenow");
  } else {
    fill.style.width = `${overall}%`;
    track.setAttribute("aria-valuenow", String(Math.round(overall)));
  }

  const nodes = $("#job-nodes");
  const stageNodes = Array.isArray(progress?.nodes) ? progress.nodes : [];
  if (stageNodes.length) {
    const validStatuses = new Set(["pending", "active", "completed", "failed"]);
    nodes.innerHTML = stageNodes.map((node) => {
      let status = validStatuses.has(node?.status) ? node.status : "pending";
      if (failed && status === "active") status = "failed";
      return `<span class="job-node ${status}">${escapeHtml(node?.label || node?.key || "阶段")}</span>`;
    }).join("");
    nodes.classList.remove("hidden");
  } else {
    nodes.replaceChildren();
    nodes.classList.add("hidden");
  }
  const retry = $("#job-retry");
  retry.textContent = state.jobRetryBusy
    ? "正在重试…"
    : partialFailure ? "重试失败组" : job.retry_mode === "export_failed" ? "重试失败项" : "重试";
  retry.disabled = state.jobRetryBusy;
  retry.classList.toggle("hidden", !failed || !job.retryable);
  $("#job-cancel").textContent = failed ? "关闭" : job.foreground ? "处理中" : "取消";
  $("#job-cancel").disabled = job.status === "cancelling" || Boolean(job.foreground && !failed);
  bar.classList.toggle("failed", failed);
  bar.classList.remove("hidden");
}

async function refreshBootstrap() {
  const data = await api("/api/bootstrap");
  state.bootstrap = data;
  state.token = data.token;
  try {
    state.lightroomStatus = await api("/api/lightroom/status");
  } catch (_error) {
    state.lightroomStatus = null;
  }
  try {
    state.styleLibrary = await api("/api/style-library");
  } catch (_error) {
    state.styleLibrary = null;
  }
  try {
    state.modelResources = await api("/api/model-resources");
  } catch (_error) {
    state.modelResources = null;
  }
  state.projects = data.projects || [];
  state.jobs = data.jobs || [];
  state.transactions = data.transactions || [];
  state.toolboxTransactions = data.toolbox_transactions || [];
  state.xmpCleanupTransactions = data.xmp_cleanup_transactions || [];
  const preferences = data.preferences || {};
  if (!state.modelProfile && preferences.model_profile_preference) state.modelProfile = preferences.model_profile_preference;
  if (!state.currentRun) {
    const workflow = preferences.workflow_defaults || data.defaults || {};
    $("#scoring-mode").value = workflow.mode === "fast" ? "fast" : "deep";
    $("#retain-ratio").value = Number(workflow.retain_ratio || 0.30).toFixed(2);
    const exportDefaults = preferences.export_defaults || {};
    state.exportTargets = {
      xmp: exportDefaults.xmp !== false,
      jpeg: exportDefaults.jpeg === true,
    };
  }
  state.activeJob = state.jobs.find((job) => ["queued", "running", "cancelling"].includes(job.status)) || null;
  if (!$("#cull-path").value) $("#cull-path").value = data.defaults.pending;
  if (!$("#raw-jpeg-mixed-path").value) $("#raw-jpeg-mixed-path").value = data.defaults.pending;
  if (!$("#raw-jpeg-raw-path").value) $("#raw-jpeg-raw-path").value = data.defaults.pending;
  if (!$("#raw-jpeg-jpeg-path").value) $("#raw-jpeg-jpeg-path").value = data.defaults.pending;
  if (!$("#xmp-cleanup-path").value) $("#xmp-cleanup-path").value = data.defaults.pending;
  renderAlerts();
  renderProjects();
  renderLightroom();
  renderStyleLibrary();
  renderContentRootSettings();
  renderModelResources();
  renderModelSetupState();
  renderJobBar();
  renderToolbox();
}

function warningText(item) {
  return (item.keywords || []).filter((value) => value.startsWith("AI|技术警告")).map((value) => value.split("|").at(-1)).join(" · ");
}

function ratingControls(item, compact = false) {
  const buttons = [0, 3, 4, 5].map((rating) => `<button class="rating-button ${item.effective_rating === rating ? "active" : ""}" data-rate-index="${item.index}" data-rating="${rating}" title="设为 ${rating} 星">${rating}</button>`).join("");
  const reset = item.manual_override ? `<button class="rating-button rating-reset" data-rate-index="${item.index}" data-rating="reset">恢复 AI</button>` : "";
  return `<div class="rating-buttons ${compact ? "compact" : ""}">${buttons}${reset}</div>`;
}

function matchesFilter(item) {
  if (state.filter === "all") return true;
  if (state.filter === "warning") return Boolean(warningText(item));
  const rating = Number(state.filter);
  if (rating === 4) return item.effective_rating >= 4;
  return item.effective_rating === rating;
}

function clearGroupingSelection() {
  state.selectedIndices.clear();
  state.selectionAnchor = null;
}

function renderReview() {
  const run = state.currentRun;
  $("#review-empty").classList.toggle("hidden", Boolean(run));
  $("#review-workspace").classList.toggle("hidden", !run);
  if (!run) return;
  const scored = run.workflow_state === "scored";
  const grouping = state.groupingMode || !scored || run.needs_rescore;
  const activeItems = run.results.filter((item) => !item.excluded);
  const excludedItems = run.results.filter((item) => item.excluded);
  const activeIndexes = new Set(activeItems.map((item) => item.index));
  state.selectedIndices = new Set([...state.selectedIndices].filter((index) => activeIndexes.has(index)));
  const batch = grouping && state.batchGrouping;
  $("#review-workspace").classList.toggle("has-batch", batch);
  $("#review-flow").classList.toggle("hidden", batch);
  $("#review-title").textContent = `${run.input_root.split(/[\\/]/).filter(Boolean).at(-1)} · ${activeItems.length} 张${excludedItems.length ? `（另移除 ${excludedItems.length} 张）` : ""}`;
  const mode = run.scoring_mode === "deep" ? "深度评分" : run.scoring_mode === "fast" ? "快速评分" : "旧版评分";
  $("#review-summary").textContent = grouping
    ? `${run.group_count || 0} 个相似组 · ${run.manual_group_adjusted_count || 0} 张人工调整${excludedItems.length ? ` · ${excludedItems.length} 张不参与评分` : ""}`
    : `${mode} · 跨组统一排名 · ${run.strong_count} 张 4–5 星 · ${run.candidate_count} 张候选 · ${run.manual_adjusted_count || 0} 张人工调星`;
  $("#review-toolbar").classList.toggle("hidden", grouping);
  $("#develop-open").classList.toggle("hidden", grouping || !run.xmp_ready);
  $("#score-run").classList.toggle("hidden", !grouping);
  $("#score-run").textContent = scored ? "重新 AI 选片" : "开始 AI 选片";
  $("#score-run").disabled = Boolean(state.activeJob) || state.batchBusy || activeItems.length === 0 || !modelSetupStatus().ready;
  $("#score-settings").classList.toggle("hidden", !grouping);
  $("#review-flow-copy").textContent = grouping
    ? "核对相似分组，确认后开始 AI 评分"
    : `${run.candidate_count} 张候选已就绪；继续完成可选处理并导出`;
  $("#grouping-toggle").classList.toggle("hidden", !scored || Boolean(run.needs_rescore));
  $("#grouping-toggle").textContent = grouping ? "返回审片" : "调整分组";
  $("#batch-select-toggle").classList.toggle("hidden", !grouping);
  $("#batch-select-toggle").textContent = batch ? "结束批量" : "批量选择";
  $("#batch-select-toggle").disabled = Boolean(state.activeJob) || state.batchBusy;
  const selectionToolbar = $("#selection-toolbar");
  selectionToolbar.classList.toggle("hidden", !batch);
  $("#selection-count").textContent = `已选 ${state.selectedIndices.size} 张`;
  const orderedGroups = [...new Set(activeItems.map((item) => Number(item.group_id)))].sort((a, b) => a - b);
  const selectedGroups = new Set(activeItems.filter((item) => state.selectedIndices.has(item.index)).map((item) => Number(item.group_id)));
  const firstGroup = orderedGroups[0];
  const lastGroup = orderedGroups.at(-1);
  $("#selection-all").disabled = state.batchBusy || state.selectedIndices.size === activeItems.length;
  $("#selection-clear").disabled = state.batchBusy || state.selectedIndices.size === 0;
  $("#selection-move").disabled = state.batchBusy || state.selectedIndices.size === 0;
  $("#selection-up").disabled = state.batchBusy || state.selectedIndices.size === 0 || [...selectedGroups].every((groupId) => groupId === firstGroup);
  $("#selection-down").disabled = state.batchBusy || state.selectedIndices.size === 0 || [...selectedGroups].every((groupId) => groupId === lastGroup);
  $("#selection-new-group").disabled = state.batchBusy || state.selectedIndices.size === 0;
  $("#selection-remove").disabled = state.batchBusy || state.selectedIndices.size === 0;
  const notice = $("#grouping-notice");
  notice.classList.toggle("hidden", !grouping);
  notice.textContent = run.needs_rescore
    ? "分组已修改，旧星级已暂停使用。重新评分后才能写入 XMP。"
    : scored
      ? "可批量移动或移出照片；移出只影响本工程，不会删除 RAW。"
      : "先核对分组；可批量移动或移出照片，确认后再进行 AI 评分。";
  const groups = new Map();
  activeItems.forEach((item) => {
    if (!groups.has(item.group_id)) groups.set(item.group_id, []);
    groups.get(item.group_id).push(item);
  });
  let visible = 0;
  const groupMarkup = [...groups.entries()].sort((a, b) => Number(a[0]) - Number(b[0])).map(([groupId, items]) => {
    const ordered = grouping
      ? items.sort((a, b) => a.index - b.index)
      : items.sort((a, b) => b.effective_rating - a.effective_rating || b.score - a.score);
    const cards = ordered.map((item) => {
      const show = grouping || matchesFilter(item);
      if (show) visible += 1;
      const warning = warningText(item);
      const status = grouping
        ? (item.manual_group_override ? `<span class="status-chip grouping">人工分组</span>` : "")
        : (item.manual_override ? `<span class="status-chip manual">你 ${item.effective_rating} · AI ${item.ai_rating}</span>` : "");
      const statusLine = status || (!grouping && item.existing_xmp) ? `<div class="photo-line">${status}${!grouping && item.existing_xmp ? `<span class="status-chip">已有 XMP</span>` : ""}</div>` : "";
      const warningLine = !grouping && warning ? `<div class="tags">${escapeHtml(warning)}</div>` : "";
      const selected = batch && state.selectedIndices.has(item.index);
      const selector = batch ? `<label class="card-select" title="选择 ${escapeHtml(item.filename)}"><input type="checkbox" data-select-index="${item.index}" ${selected ? "checked" : ""} ${state.batchBusy || state.activeJob ? "disabled" : ""}><span></span></label>` : "";
      const actionDisabled = state.activeJob || state.batchBusy;
      const actions = grouping && !batch ? `<div class="card-actions">
        <button class="move-group-button" data-move-group="${item.index}" ${actionDisabled ? "disabled" : ""}>移动到…</button>
        <button class="move-group-button" data-shift-index="${item.index}" data-shift-direction="previous" ${actionDisabled || Number(item.group_id) === firstGroup ? "disabled" : ""}>上移</button>
        <button class="move-group-button" data-shift-index="${item.index}" data-shift-direction="next" ${actionDisabled || Number(item.group_id) === lastGroup ? "disabled" : ""}>下移</button>
        <button class="move-group-button" data-new-group-index="${item.index}" ${actionDisabled ? "disabled" : ""}>新建组</button>
        <button class="move-group-button remove-item-button" data-exclude-index="${item.index}" ${actionDisabled ? "disabled" : ""}>移出</button>
      </div>` : "";
      return `<article class="photo-card ${grouping ? "grouping-card" : ""} ${selected ? "selected" : ""} ${show ? "" : "hidden-by-filter"}" data-rating="${item.effective_rating}" data-index="${item.index}" aria-selected="${selected}">
        ${selector}
        <button class="preview-button" data-open-photo="${item.index}"><img src="${item.preview_url}" loading="lazy" alt="${escapeHtml(item.filename)}"></button>
        <div class="photo-meta">
          <div class="photo-line"><strong title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</strong></div>
          ${actions}
          ${statusLine}
          ${grouping ? "" : ratingControls(item)}
          ${warningLine}
        </div>
      </article>`;
    }).join("");
    const groupVisible = grouping || items.some(matchesFilter);
    const selectGroup = batch ? `<button class="text-button select-group" data-select-group="${groupId}">选择本组</button>` : "";
    return `<section class="photo-group ${groupVisible ? "" : "hidden-by-filter"}"><div class="group-head"><strong>组 ${groupId}</strong><span>${items.length === 1 ? "单张" : `${items.length} 张相似照片`}</span>${selectGroup}</div><div class="filmstrip">${cards}</div></section>`;
  }).join("");
  const excludedMarkup = grouping && excludedItems.length ? `<details class="excluded-group">
    <summary>已移出工程 ${excludedItems.length} 张</summary>
    <div class="excluded-head"><span>这些照片不参与评分或 XMP，RAW 文件仍在原处。</span><button class="text-button" data-restore-all>全部恢复</button></div>
    <div class="filmstrip">${excludedItems.map((item) => `<article class="photo-card removed-card" data-index="${item.index}">
      <button class="preview-button" data-open-photo="${item.index}"><img src="${item.preview_url}" loading="lazy" alt="${escapeHtml(item.filename)}"></button>
      <div class="photo-meta"><div class="photo-line"><strong title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</strong><button class="move-group-button" data-restore-index="${item.index}">恢复</button></div></div>
    </article>`).join("")}</div>
  </details>` : "";
  const activeMarkup = groupMarkup || (grouping ? `<div class="empty">当前没有参与评分的照片</div>` : "");
  $("#review-groups").innerHTML = `${activeMarkup}${excludedMarkup}`;
  $("#visible-count").textContent = grouping || state.filter === "all" ? "" : `显示 ${visible} / ${activeItems.length}`;
  $("#develop-open").disabled = Boolean(state.activeJob) || Number(run.candidate_count || 0) < 1;
  renderWorkflow("review");
  if (state.dialogIndex != null) updatePhotoDialog(state.dialogIndex);
}

function syncProjectRunStats(run) {
  const stats = {
    candidate_count: run.candidate_count,
    strong_count: run.strong_count,
    manual_adjusted_count: run.manual_adjusted_count || 0,
    manual_group_adjusted_count: run.manual_group_adjusted_count || 0,
    active_image_count: run.active_image_count ?? run.results.filter((item) => !item.excluded).length,
    excluded_count: run.excluded_count || 0,
    group_count: run.group_count || 0,
    workflow_state: run.workflow_state,
    needs_rescore: Boolean(run.needs_rescore),
    xmp_ready: Boolean(run.xmp_ready),
  };
  const project = state.projects.find((item) => item.latest_run_id === run.run_id);
  if (project) {
    Object.assign(project, stats);
    if (project.latest_result) Object.assign(project.latest_result, stats);
  }
  renderProjects();
}

async function openRun(runId, route = `review/${runId}`, historyMode = "replace") {
  state.currentRun = await api(`/api/runs/${runId}`);
  const project = state.projects.find((item) => item.latest_run_id === runId);
  if (project) state.currentProject = project;
  else if (state.currentProject?.latest_run_id !== runId) state.currentProject = null;
  state.developPlan = null;
  state.developStage = "crop";
  const exportDefaults = state.bootstrap?.preferences?.export_defaults || {};
  state.exportTargets = {
    xmp: exportDefaults.xmp !== false,
    jpeg: exportDefaults.jpeg === true,
  };
  state.exportSpec = null;
  state.styleScope = "global";
  $("#scoring-mode").value = ["deep", "fast"].includes(state.currentRun.scoring_mode) ? state.currentRun.scoring_mode : "deep";
  $("#retain-ratio").value = Number(state.currentRun.retain_ratio || 0.30).toFixed(2);
  state.groupingMode = state.currentRun.workflow_state !== "scored" || Boolean(state.currentRun.needs_rescore);
  state.batchGrouping = false;
  state.batchBusy = false;
  state.groupDialogIndices = [];
  clearGroupingSelection();
  state.filter = "all";
  $$(".filter").forEach((node) => node.classList.toggle("active", node.dataset.filter === "all"));
  renderReview();
  setView("review", route, historyMode);
}

function currentProjectRoute() {
  if (state.currentProject?.project_id) return `project/${state.currentProject.project_id}`;
  const project = state.projects.find((item) => item.latest_run_id === state.currentRun?.run_id);
  return project ? `project/${project.project_id}` : `review/${state.currentRun?.run_id || ""}`;
}

function isStyleJob(job) {
  return ["style_recommend", "style_preview"].includes(job?.kind);
}

function styleJobMatchesCurrentRun(job) {
  if (!isStyleJob(job) || !state.currentRun) return false;
  const runId = job.context?.run_id ?? job.result?.run_id;
  return runId == null || String(runId) === String(state.currentRun.run_id);
}

function activateJob(job) {
  if (!job?.id || !job?.kind) throw new Error("后台任务响应无效，请重试。 ");
  state.jobNotice = null;
  state.jobs
    .filter((item) => !["queued", "running", "cancelling"].includes(item.status))
    .forEach((item) => state.handledJobs.add(item.id));
  state.handledJobs.delete(job.id);
  state.jobs = [job, ...state.jobs.filter((item) => item.id !== job.id)];
  state.activeJob = job;
  renderJobBar();
  if (state.currentProject && !state.currentProject.latest_run_id) renderProjectStart();
  renderReview();
  if (location.hash.replace(/^#/, "").startsWith("develop/")) renderDevelop();
  if ($("#view-export").classList.contains("active")) renderExport();
  pollJobs();
  return job;
}

async function reloadDevelopAfterStyleJob(job) {
  if (!styleJobMatchesCurrentRun(job)) return false;
  const plan = await api(`/api/runs/${state.currentRun.run_id}/develop`);
  state.developPlan = plan;
  syncDevelopSummary(plan);
  return true;
}

function currentCropComplete(plan = state.developPlan) {
  const status = plan?.crop?.status;
  if (["confirmed", "skipped"].includes(status)) return Boolean(plan?.exists) && !plan?.stale;
  return Boolean(plan?.exists) && !plan?.stale
    && Number(plan?.eligible_count || 0) > 0
    && Number(plan?.confirmed_count || 0) === Number(plan?.eligible_count || 0);
}

function currentColorMode(plan = state.developPlan) {
  if (["skip", "auto", "style"].includes(plan?.color_mode)) return plan.color_mode;
  if (plan?.creative_style?.status === "confirmed" || Object.keys(plan?.creative_style?.groups || {}).length) return "style";
  if (plan?.basic_color?.status === "enabled") return "auto";
  if (plan?.basic_color?.status === "skipped") return "skip";
  return "auto";
}

function currentStyleScope(plan = state.developPlan) {
  return plan?.creative_style?.scope === "global" ? "global" : "group";
}

function currentBaseComplete(plan = state.developPlan) {
  return ["enabled", "skipped"].includes(plan?.basic_color?.status);
}

function currentStyleComplete(plan = state.developPlan) {
  const mode = currentColorMode(plan);
  if (mode !== "style") return currentBaseComplete(plan);
  const creative = plan?.creative_style || {};
  if (creative.status !== "confirmed") return false;
  if (currentStyleScope(plan) === "global") {
    const selection = creative.global_selection;
    if (!selection || !["confirmed", "skipped"].includes(selection.status)) return false;
    return Boolean(selection.manual_override) || selection.recommendation_status === "complete";
  }
  const groups = creative.groups || {};
  const groupIds = [...new Set((plan?.items || []).map((item) => String(item.group_id)))];
  if (!groupIds.length) return false;
  return groupIds.every((groupId) => {
    const selection = groups[groupId];
    if (!selection || !["confirmed", "skipped"].includes(selection.status)) return false;
    return Boolean(selection.manual_override) || selection.recommendation_status === "complete";
  });
}

function currentColorComplete(plan = state.developPlan) {
  return currentBaseComplete(plan) && currentStyleComplete(plan);
}

function renderWorkflow(activeStage) {
  const run = state.currentRun;
  const plan = state.developPlan || run?.develop;
  const scored = Boolean(run?.xmp_ready);
  const hasCandidates = Number(run?.candidate_count || 0) > 0;
  const cropComplete = currentCropComplete(plan);
  const cropStale = Boolean(plan?.stale);
  const exportStatus = state.exportSpec?.status;
  const statuses = {
    review: scored ? "completed" : "pending",
    crop: cropStale ? "stale" : cropComplete ? (plan?.crop_skipped ? "skipped" : "completed") : "pending",
    base: currentBaseComplete(plan) ? (plan?.basic_color?.status === "skipped" ? "skipped" : "completed") : "pending",
    style: currentStyleComplete(plan) ? (currentColorMode(plan) === "style" ? "completed" : "skipped") : "pending",
    export: ["complete", "completed"].includes(exportStatus) ? "completed" : ["failed", "partial", "partial_failure"].includes(exportStatus) ? "stale" : "pending",
  };
  const order = { review: "1", crop: "2", base: "3", style: "4", export: "5" };
  $$(".workflow-step").forEach((node) => {
    const stage = node.dataset.workflowStage;
    const available = stage === "review"
      || (stage === "crop" && scored && hasCandidates)
      || (stage === "base" && scored && cropComplete)
      || (stage === "style" && scored && cropComplete && currentBaseComplete(plan))
      || (stage === "export" && (activeStage === "export" || (activeStage === "style" && cropComplete && currentColorComplete(plan))));
    const status = statuses[stage];
    node.disabled = !available;
    node.classList.toggle("active", stage === activeStage);
    node.classList.toggle("completed", status === "completed" && stage !== activeStage);
    node.classList.toggle("skipped", status === "skipped" && stage !== activeStage);
    node.classList.toggle("stale", status === "stale" && stage !== activeStage);
    node.querySelector("span").textContent = status === "completed" && stage !== activeStage
      ? "✓" : status === "skipped" && stage !== activeStage ? "—" : status === "stale" && stage !== activeStage ? "!" : order[stage];
    node.setAttribute("aria-current", stage === activeStage ? "step" : "false");
  });
}

function renderExport() {
  const run = state.currentRun;
  if (!run) return;
  const plan = state.developPlan;
  const candidates = run.results.filter((item) => !item.excluded && Number(item.effective_rating ?? item.rating) >= 3);
  const proprietary = new Set(["arw", "cr2", "cr3", "nef", "nrw", "orf", "rw2", "raf", "pef", "srw", "x3f"]);
  const sidecarCandidates = candidates.filter((item) => proprietary.has(String(item.filename || item.path || "").split(".").at(-1).toLowerCase()));
  const existingCount = sidecarCandidates.filter((item) => item.existing_xmp).length;
  const xmp = Boolean(state.exportTargets.xmp);
  const jpeg = Boolean(state.exportTargets.jpeg);
  const mode = currentColorMode(plan);
  const creativeGroups = Object.values(plan?.creative_style?.groups || {});
  const styledGroupCount = creativeGroups.filter((item) => item?.status === "confirmed" && (item?.lut_id || item?.preset_id)).length;
  const globalStyle = plan?.creative_style?.global_selection || {};
  const globalStyled = currentStyleScope(plan) === "global"
    && globalStyle.status === "confirmed"
    && Boolean(globalStyle.lut_id || globalStyle.preset_id);
  const styleSummary = mode !== "style" ? "自然"
    : currentStyleScope(plan) === "global" ? (globalStyled ? "全局统一" : "自然")
      : styledGroupCount ? `${styledGroupCount} 组` : "自然";
  const projectName = run.input_root.split(/[\\/]/).filter(Boolean).at(-1) || "工程";
  const outputDirectory = $("#export-directory");
  if (!outputDirectory.value) outputDirectory.value = `${String(run.input_root).replace(/[\\/]$/, "")}\\成片`;
  $("#export-directory-field").classList.toggle("hidden", !jpeg);
  $("#export-xmp").checked = xmp;
  $("#export-jpeg").checked = jpeg;
  $$('[data-export-target]').forEach((node) => node.classList.toggle("selected", node.dataset.exportTarget === "xmp" ? xmp : jpeg));
  $("#export-title").textContent = `${projectName} · 导出`;
  $("#export-summary").textContent = `${candidates.length} 张`;
  $("#export-back").textContent = "上一步：创意外观";
  $("#export-content").innerHTML = `<div class="export-summary-list">
      <div class="export-summary-row"><span>构图</span><strong>${plan?.crop?.status === "confirmed" ? "已确认" : "已跳过"}</strong></div>
      <div class="export-summary-row"><span>基础调色</span><strong>${plan?.basic_color?.status === "enabled" ? "Lightroom 自动" : "已跳过"}</strong></div>
      <div class="export-summary-row"><span>创意外观</span><strong>${styleSummary}</strong></div>
    </div>
    ${existingCount && xmp ? `<div class="export-impact warning">${existingCount} 张已有 XMP 将通过 Lightroom 安全更新；执行前会备份并记录文件指纹。</div>` : ""}`;

  const button = $("#export-action");
  button.classList.remove("completed");
  button.disabled = Boolean(state.activeJob) || candidates.length === 0 || (!xmp && !jpeg) || !currentColorComplete(plan);
  button.textContent = xmp && jpeg ? `保存 XMP 并导出 ${candidates.length} 张 JPEG`
    : jpeg ? `导出 ${candidates.length} 张 JPEG`
      : `保存 ${sidecarCandidates.length} 个 XMP`;
  const status = $("#export-status");
  status.className = "develop-output-hint hidden";
  const spec = state.exportSpec;
  if (["complete", "completed"].includes(spec?.status)) {
    status.classList.remove("hidden");
    status.classList.add("success");
    const xmpDone = Number(spec.results?.xmp?.succeeded || 0);
    const jpegDone = Number(spec.results?.jpeg?.succeeded || 0);
    // The controls may have been rehydrated while Lightroom was working.  A
    // completion summary must describe the frozen export specification, not
    // whichever checkboxes happen to be selected after the job finishes.
    const completedXmp = Boolean(spec.targets?.xmp);
    const completedJpeg = Boolean(spec.targets?.jpeg);
    status.innerHTML = `<strong>导出完成</strong><span>${completedXmp ? `XMP ${xmpDone} 个` : ""}${completedXmp && completedJpeg ? " · " : ""}${completedJpeg ? `JPEG ${jpegDone} 张` : ""}</span>`;
  } else if (["queued", "running", "cancelling"].includes(spec?.status)) {
    button.disabled = true;
  } else if (["failed", "partial", "partial_failure"].includes(spec?.status)) {
    status.classList.remove("hidden");
    status.classList.add("warning");
    const partial = ["partial", "partial_failure"].includes(spec.status);
    status.innerHTML = `<strong>${partial ? "部分完成" : "导出未完成"}</strong><span>${escapeHtml(spec.message || "再次执行只会重试失败项。")}</span>`;
    button.disabled = Boolean(state.activeJob);
    button.textContent = partial ? "重试失败项" : button.textContent;
  }
  renderWorkflow("export");
}

function renderDevelopGeneration(progress = {}) {
  const projectName = state.currentRun?.input_root.split(/[\\/]/).filter(Boolean).at(-1) || "工程";
  const total = Number(progress.total || state.developPlan?.eligible_count || 0);
  const current = Number(progress.current || 0);
  const filenameText = progress.filename ? ` · ${progress.filename}` : "";
  $("#develop-title").textContent = `${projectName} · 智能构图`;
  $("#develop-summary").textContent = total ? `${Math.max(1, current)} / ${total} 张${filenameText}` : "正在准备本地 AI";
  $("#develop-back").textContent = "上一步：选片";
  $("#develop-base-workspace").classList.add("hidden");
  $("#style-workspace").classList.add("hidden");
  $("#develop-grid").classList.remove("hidden");
  $("#develop-next").disabled = true;
  $("#develop-skip").disabled = true;
  $("#develop-flow-title").textContent = "";
  $("#develop-flow-copy").textContent = "";
  $("#develop-output-hint").className = "develop-output-hint hidden";
  $("#develop-output-hint").replaceChildren();
  $("#develop-grid").innerHTML = `<div class="empty">正在分析构图，完成后会自动显示结果。</div>`;
  renderJobBar();
  renderWorkflow("crop");
}

function renderStyleGroups(plan) {
  const container = $("#style-groups");
  const controlsBusy = state.developBusy || Boolean(state.activeJob);
  const groups = new Map();
  for (const item of plan.items || []) {
    const key = String(item.group_id);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  const creative = plan.creative_style || {};
  const scope = currentStyleScope(plan);
  container.classList.toggle("group-table", scope === "group");
  const entries = scope === "global"
    ? [["global", [...groups.values()].flat(), creative.global_selection || {}]]
    : [...groups.entries()].map(([groupId, items]) => [groupId, items, creative.groups?.[groupId] || {}]);
  container.innerHTML = entries.map(([entryId, items, selection]) => {
    const global = entryId === "global";
    const groupId = global ? null : entryId;
    const top3 = Array.isArray(selection.top3) ? selection.top3 : [];
    const representative = items.find((item) => Number(item.index) === Number(selection.representative_index)) || items[0];
    const neutral = {
      preset_id: null, lut_id: null,
      label: "自然",
      preview_url: selection.neutral_preview_url || representative?.preview_url,
      score: selection.neutral_score,
    };
    const choices = [neutral, ...top3.slice(0, 3)];
    while (!global && choices.length < 4) {
      choices.push({ placeholder: true, label: `候选 ${choices.length}` });
    }
    const recommendationComplete = selection.recommendation_status === "complete";
    const naturalSelected = creative.status === "skipped" || selection.status === "skipped"
      || (selection.status !== "confirmed"
        && recommendationComplete
        && selection.recommended_kind === "neutral");
    const selectedId = selection.status === "skipped" ? null
      : (selection.lut_id ?? selection.preset_id
        ?? (recommendationComplete ? (selection.recommended_lut_id ?? selection.recommended_preset_id) : null)
        ?? null);
    const recommendedId = recommendationComplete && ["preset", "lut"].includes(selection.recommended_kind)
      ? (selection.recommended_lut_id ?? selection.recommended_preset_id ?? null)
      : null;
    const recommendedChoice = recommendedId == null ? null : top3.find((choice) =>
      String(choice.lut_id ?? choice.preset_id ?? choice.id ?? "") === String(recommendedId)
    );
    const usesPendingRecommendation = selection.status !== "confirmed"
      && selection.status !== "skipped"
      && Boolean(recommendedChoice)
      && String(selectedId) === String(recommendedId);
    const cards = choices.map((choice, rank) => {
      if (choice.placeholder) {
        return `<div class="style-choice style-choice-empty" aria-label="${escapeHtml(choice.label)}等待生成">
          <div class="style-choice-preview style-choice-placeholder pending" role="img"><span>等待生成</span></div>
          <span class="style-choice-copy"><strong>${escapeHtml(choice.label)}</strong></span>
        </div>`;
      }
      const lutId = choice.lut_id ?? null;
      const presetId = choice.preset_id ?? choice.id ?? null;
      const choiceId = lutId ?? presetId;
      const selected = rank === 0 ? naturalSelected : Boolean(selectedId) && String(choiceId) === String(selectedId);
      const renderStatus = rank === 0 ? "ready" : String(choice.render_status || "pending").toLowerCase();
      const preview = rank === 0 ? (choice.preview_url || "") : (renderStatus === "ready" ? (choice.preview_url || "") : "");
      const previewReady = Boolean(preview) && (rank === 0 || renderStatus === "ready");
      const selectable = rank === 0 || previewReady;
      const disabled = controlsBusy || !selectable;
      const placeholderLabel = renderStatus === "failed"
        ? "Lightroom 预览失败"
        : renderStatus === "ready" ? "预览文件暂不可用" : "等待 Lightroom 真实预览";
      const previewMarkup = previewReady
        ? `<img class="style-choice-preview" src="${escapeHtml(preview)}" loading="lazy" alt="${escapeHtml(choice.label || choice.name || "风格预览")}">`
        : `<div class="style-choice-preview style-choice-placeholder ${renderStatus === "failed" ? "failed" : "pending"}" role="img" aria-label="${escapeHtml(placeholderLabel)}"><span>${escapeHtml(placeholderLabel)}</span></div>`;
      const recommendedCandidate = recommendationComplete
        && ["preset", "lut"].includes(selection.recommended_kind)
        && String(choiceId) === String(selection.recommended_lut_id ?? selection.recommended_preset_id ?? "");
      const badge = rank === 0
        ? (recommendationComplete && selection.recommended_kind === "neutral" ? "AI 推荐" : "")
        : recommendedCandidate ? "AI 推荐" : "";
      return `<button type="button" class="style-choice ${selected ? "selected" : ""} ${previewReady ? "preview-ready" : `preview-${renderStatus === "failed" ? "failed" : "pending"}`}" data-style-choice data-style-scope="${scope}" data-group-id="${escapeHtml(groupId ?? "")}" data-choice-id="${escapeHtml(choiceId || "natural")}" data-lut-id="${escapeHtml(lutId || "")}" data-lut-hash="${escapeHtml(choice.lut_hash || "")}" data-preset-id="${escapeHtml(presetId || "")}" data-preset-hash="${escapeHtml(choice.preset_hash || choice.hash || "")}" data-style-choice-amount="${Number(choice.strength ?? choice.amount ?? 100)}" data-preview-ready="${previewReady ? "true" : "false"}" aria-disabled="${disabled ? "true" : "false"}" ${disabled ? "disabled" : ""}>
        ${previewMarkup}
        <span class="style-choice-copy"><strong>${escapeHtml(choice.label || choice.name || "未命名外观")}</strong>${badge ? `<small>${escapeHtml(badge)}</small>` : ""}</span>
      </button>`;
    }).join("");
    const recommendationStatus = String(selection.recommendation_status || "").toLowerCase();
    const hasReadyCandidate = top3.some((choice) => choice?.render_status === "ready" && choice?.preview_url);
    const groupState = recommendationStatus === "failed" ? "预览失败"
      : recommendationStatus === "rendering" || (!hasReadyCandidate && top3.length) ? "渲染中"
        : selection.status === "confirmed" ? "已确认"
          : selection.status === "skipped" ? "自然" : "";
    // Before confirmation, capability and default strength belong to the
    // recommended Top 3 candidate. Once the user confirms or changes a look,
    // the persisted group selection is authoritative.
    const amountSource = usesPendingRecommendation ? recommendedChoice : selection;
    const amount = Number(amountSource.strength ?? amountSource.amount ?? 100);
    const amountSupported = Boolean(selectedId) && amountSource.amount_supported !== false;
    const amountNote = selectedId && !amountSupported
      ? (amountSource.amount_note || (amountSource.preset_scope === "plugin"
        ? "此预设由插件托管，Lightroom 仅支持 100%；可换原生预设调强度"
        : "此预设未启用 Lightroom 强度调整"))
      : "";
    const actions = global ? "" : `<div class="style-group-actions"><button type="button" class="text-button" data-style-recommend-group="${groupId}" ${controlsBusy ? "disabled" : ""}>AI 推荐</button><button type="button" class="text-button" data-style-search-group="${groupId}" ${controlsBusy ? "disabled" : ""}>搜索</button></div>`;
    const amountKey = global ? "global" : groupId;
    const amountLutId = amountSource.lut_id ?? recommendedChoice?.lut_id ?? null;
    const amountPresetId = amountLutId ? null : (amountSource.preset_id ?? recommendedChoice?.preset_id ?? null);
    const amountLutHash = amountSource.lut_hash ?? recommendedChoice?.lut_hash ?? null;
    const amountPresetHash = amountLutId ? null : (amountSource.preset_hash ?? recommendedChoice?.preset_hash ?? null);
    const tierSupported = amountSupported && !(global && Boolean(amountLutId));
    const tiers = tierSupported ? `<div class="style-amount-tiers" role="group" aria-label="外观强度">
      <span>强度</span>${STYLE_AMOUNT_TIERS.map((tier) => `<button type="button" class="style-amount-tier ${amount === tier ? "selected" : ""}" data-style-tier="${tier}" data-style-scope="${scope}" data-style-amount="${escapeHtml(amountKey)}" data-lut-id="${escapeHtml(amountLutId || "")}" data-lut-hash="${escapeHtml(amountLutHash || "")}" data-preset-id="${escapeHtml(amountPresetId || "")}" data-preset-hash="${escapeHtml(amountPresetHash || "")}" ${controlsBusy ? "disabled" : ""}>${tier === 50 ? "轻" : tier === 100 ? "标准" : "强"}<small>${tier}%</small></button>`).join("")}
    </div>` : "";
    const selectedChoice = selectedId == null ? null : top3.find((choice) =>
      String(choice.lut_id ?? choice.preset_id ?? choice.id ?? "") === String(selectedId)
    );
    const exactSamples = Array.isArray(selectedChoice?.preview_samples) && selectedChoice.preview_samples.length
      ? selectedChoice.preview_samples
      : (selectedId != null && Array.isArray(selection.selected_preview_samples) ? selection.selected_preview_samples : []);
    const probeItems = (selection.preview_probe_indices || []).map((probe) => {
      const item = items.find((value) => Number(value.index) === Number(probe.index));
      return item ? { ...probe, preview_url: item.preview_url } : null;
    }).filter(Boolean);
    const sampleValues = exactSamples.length ? exactSamples : probeItems;
    const roleLabels = { representative: "代表", brightest: "最亮", darkest: "最暗" };
    const coverage = global && sampleValues.length ? `<section class="style-global-coverage ${exactSamples.length ? "ready" : "pending"}">
      <div class="style-global-coverage-head"><strong>统一效果抽查</strong><span>${exactSamples.length ? `已用 Lightroom 刷新 ${sampleValues.length} 张` : "选择外观或强度后刷新真实预览"}</span></div>
      <div class="style-global-samples">${sampleValues.map((sample) => `<figure><img src="${escapeHtml(sample.preview_url || "")}" loading="lazy" alt="${escapeHtml(roleLabels[sample.role] || "抽查")}预览"><figcaption><strong>${escapeHtml(roleLabels[sample.role] || "抽查")}</strong><span>${escapeHtml(sample.filename || "")}</span></figcaption></figure>`).join("")}</div>
    </section>` : "";
    const representativeName = representative?.filename || "代表图";
    return `<article class="panel style-group ${global ? "style-global-group" : "style-group-row"}" data-style-group="${escapeHtml(amountKey)}"${global ? "" : ` data-representative-index="${escapeHtml(representative?.index ?? "")}"`}>
      <div class="style-group-head"><div class="style-group-copy"><strong>${global ? "全局统一" : `组 ${groupId}`}</strong><span title="${escapeHtml(representativeName)}">${global ? `应用于 ${items.length} 张` : `${escapeHtml(representativeName)} · ${items.length} 张`}${groupState ? ` · ${escapeHtml(groupState)}` : ""}</span></div>${actions}</div>
      <div class="style-choices">${cards}</div>
      <div class="style-group-controls">
        ${tiers}
        ${amountNote ? `<span class="muted style-amount-note">${escapeHtml(amountNote)}</span>` : ""}
      </div>
      ${coverage}
    </article>`;
  }).join("") || `<div class="empty">没有可调色的照片组</div>`;
}

function renderDevelop() {
  const plan = state.developPlan;
  renderJobBar();
  if (plan?.generating || (state.developBusy && state.developProgress?.status === "running")) {
    renderDevelopGeneration(state.developProgress || {});
    return;
  }
  if (!plan?.exists) {
    $("#develop-grid").innerHTML = `<div class="empty">尚未生成基础处理方案</div>`;
    return;
  }
  const stage = ["base", "style"].includes(state.developStage) ? state.developStage : "crop";
  const projectName = state.currentRun?.input_root.split(/[\\/]/).filter(Boolean).at(-1) || "工程";
  const styleJobRunning = styleJobMatchesCurrentRun(state.activeJob);
  const baseComplete = currentBaseComplete(plan);
  const styleComplete = currentStyleComplete(plan);
  const stageNames = { crop: "构图", base: "基础调色", style: "创意外观" };
  $("#develop-title").textContent = `${projectName} · ${stageNames[stage]}`;
  $("#develop-summary").textContent = stage === "crop"
    ? `${plan.eligible_count} 张`
    : stage === "style" ? `${new Set((plan.items || []).map((item) => item.group_id)).size} 组` : `${plan.eligible_count} 张`;
  $("#develop-back").textContent = stage === "crop" ? "上一步：选片" : stage === "base" ? "上一步：构图" : "上一步：基础调色";
  $("#develop-grid").classList.toggle("hidden", stage !== "crop");
  $("#develop-base-workspace").classList.toggle("hidden", stage !== "base");
  $("#style-workspace").classList.toggle("hidden", stage !== "style");
  $("#develop-next").disabled = state.developBusy || plan.stale
    || (stage === "base" && !baseComplete)
    || (stage === "style" && (styleJobRunning || !styleComplete));
  $("#develop-skip").disabled = state.developBusy || Boolean(state.activeJob) || plan.stale;
  $("#develop-skip").classList.toggle("hidden", stage === "base");
  const styleScope = currentStyleScope(plan);
  const hasStyleRecommendations = styleScope === "global"
    ? Boolean(plan.creative_style?.global_selection?.top3?.length || plan.creative_style?.global_selection?.recommendation_status === "complete")
    : Object.keys(plan.creative_style?.groups || {}).length > 0;
  $("#develop-apply-style").classList.toggle("hidden", stage !== "style" || currentColorMode(plan) !== "style");
  $("#develop-apply-style").disabled = state.developBusy || styleJobRunning || !hasStyleRecommendations;
  $("#develop-apply-style").textContent = styleScope === "global" ? "采用 AI 推荐" : "全部采用 AI 推荐";
  $("#develop-flow-title").textContent = "";
  $("#develop-flow-copy").textContent = stage === "crop"
    ? "裁剪选择会即时保存"
    : stage === "base"
      ? (baseComplete ? "选择已保存" : "请选择一种基础处理方式")
      : (styleComplete ? "外观选择已保存" : "选择 AI 推荐或保持自然");
  $("#develop-skip").textContent = stage === "crop" ? "保持原图并跳过" : "保持自然并跳过";
  $("#develop-next").textContent = stage === "crop"
    ? "采用当前构图并继续"
    : stage === "base" ? "下一步：创意外观" : "下一步：导出";
  const developHint = $("#develop-output-hint");
  developHint.className = `develop-output-hint ${plan.stale ? "warning" : "hidden"}`;
  developHint.innerHTML = plan.stale ? "<strong>方案已失效</strong><span>请重新分析。</span>" : "";
  renderWorkflow(stage);

  if (stage === "base") {
    $$("[data-color-mode]").forEach((node) => {
      const selected = node.dataset.colorMode === "auto"
        ? plan.basic_color?.status === "enabled"
        : plan.basic_color?.status === "skipped";
      node.classList.toggle("selected", selected);
      node.setAttribute("aria-pressed", selected ? "true" : "false");
      node.disabled = state.developBusy || Boolean(state.activeJob);
    });
    $("#develop-base-auto-state").textContent = plan.basic_color?.status === "enabled" ? "已选择" : "";
    $("#develop-base-skip-state").textContent = plan.basic_color?.status === "skipped" ? "已选择" : "";
    return;
  }

  if (stage === "style") {
    const styleBusy = state.developBusy || Boolean(state.activeJob);
    state.styleScope = styleScope;
    $$(".style-scope-option").forEach((node) => {
      const selected = node.dataset.styleScope === styleScope;
      node.classList.toggle("selected", selected);
      node.setAttribute("aria-pressed", selected ? "true" : "false");
      node.disabled = styleBusy;
    });
    $("#style-recommend").classList.toggle("hidden", styleScope !== "global");
    $("#style-recommend").disabled = styleBusy;
    $("#style-recommend").textContent = "AI 推荐统一外观";
    const groupIds = [...new Set((plan.items || []).map((item) => String(item.group_id)))];
    const generatedGroups = groupIds.filter((groupId) =>
      plan.creative_style?.groups?.[groupId]?.recommendation_status === "complete"
    ).length;
    const allGroupsJob = styleJobRunning && state.activeJob?.context?.scope === "groups";
    const recommendAll = $("#style-recommend-all");
    recommendAll.classList.toggle("hidden", styleScope !== "group");
    recommendAll.disabled = styleBusy || groupIds.length === 0;
    recommendAll.textContent = allGroupsJob
      ? "正在生成所有组"
      : generatedGroups === 0
        ? "一键为所有组生成"
        : generatedGroups < groupIds.length ? "补齐未完成组" : "重新生成所有组";
    const batchStatus = $("#style-group-batch-status");
    batchStatus.classList.toggle("hidden", styleScope !== "group" || (!allGroupsJob && generatedGroups === 0));
    batchStatus.textContent = allGroupsJob
      ? "逐组生成中"
      : `已生成 ${generatedGroups} / ${groupIds.length} 组`;
    renderStyleGroups(plan);
    return;
  }

  const cards = (plan.items || []).map((item) => {
    const cropButtons = (item.crop_candidates || []).map((crop) =>
      `<button type="button" class="button develop-crop-choice ${crop.id === item.crop_id ? "active" : ""}" data-develop-crop-choice="${item.index}" data-crop-id="${escapeHtml(crop.id)}" aria-pressed="${crop.id === item.crop_id ? "true" : "false"}" ${state.developBusy ? "disabled" : ""}>${escapeHtml(crop.label)}</button>`
    ).join("");
    const selectedCrop = (item.crop_candidates || []).find((crop) => crop.id === item.crop_id);
    const cropText = item.crop_id === "original" ? "保留原始构图" : (selectedCrop?.label || "智能构图");
    return `<article class="develop-card" data-develop-index="${item.index}">
      <img class="develop-preview" src="${item.preview_url}" loading="lazy" alt="${escapeHtml(item.filename)} 构图预览">
      <div class="develop-body">
        <div class="develop-name"><strong title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</strong><span class="status-chip">${item.rating} 星 · 组 ${item.group_id}</span></div>
        <div class="develop-controls">
          <div class="develop-crop-row"><span>构图</span><div class="develop-crop-buttons" role="group" aria-label="${escapeHtml(item.filename)} 裁剪方案">${cropButtons}</div></div>
        </div>
        <div class="develop-base">${escapeHtml(cropText)}</div>
      </div>
    </article>`;
  }).join("");
  $("#develop-grid").innerHTML = cards || `<div class="empty">没有 3 星以上照片</div>`;
}

function syncDevelopSummary(plan) {
  if (!state.currentRun) return;
  state.currentRun.develop = {
    exists: Boolean(plan?.exists), stale: Boolean(plan?.stale), revision: plan?.revision ?? null,
    eligible_count: Number(plan?.eligible_count || 0), confirmed_count: Number(plan?.confirmed_count || 0),
    plan_id: plan?.plan_id || null, crop_skipped: Boolean(plan?.crop_skipped), color_enabled: plan?.color_enabled !== false,
    crop: plan?.crop || { status: plan?.crop_skipped ? "skipped" : "pending" },
    basic_color: plan?.basic_color || { status: plan?.color_enabled === false ? "skipped" : "pending" },
    creative_style: plan?.creative_style || { status: "skipped", groups: {} },
  };
}

function stageFromDevelopRoute(route) {
  const parts = String(route || "").split("/");
  const stage = parts.at(-1);
  if (["style", "color"].includes(stage)) return "style";
  return stage === "base" ? "base" : "crop";
}

async function loadDevelop(force = false, route = null, requestedStage = null, createIfMissing = true, historyMode = "push") {
  const run = state.currentRun;
  if (!run) throw new Error("请先打开工程。 ");
  state.developBusy = true;
  let progressTimer = null;
  let enteredGeneration = false;
  try {
    let plan = force ? null : await api(`/api/runs/${run.run_id}/develop`);
    if (!plan?.exists || plan.stale || force) {
      if (!createIfMissing) throw new Error(plan?.stale ? "构图方案已经失效，请从选片页重新开始构图。" : "尚未开始构图，请从选片页进入。 ");
      if (!await requireModelSetupUi()) throw new Error("请先完成 AI 模型套装配置。 ");
      const expected = Number(plan?.eligible_count || run.candidate_count || 0);
      state.developStage = "crop";
      state.developProgress = { status: "running", stage_label: "准备智能构图", current: 0, total: expected, overall_percent: 0, nodes: [] };
      state.developPlan = { exists: true, generating: true, stale: false, eligible_count: expected, confirmed_count: 0, items: [] };
      enteredGeneration = true;
      setView("develop", `develop/${run.run_id}/crop`, historyMode);
      renderDevelop();
      const request = api(`/api/runs/${run.run_id}/develop`, {
        method: "POST",
        body: { base_revision: Number(run.review_revision) },
      });
      progressTimer = setInterval(async () => {
        try {
          const progress = await api(`/api/runs/${run.run_id}/develop/progress`);
          if (state.developBusy && progress?.status !== "idle") {
            state.developProgress = progress;
            renderDevelop();
          }
        } catch (_error) { /* keep the last visible progress */ }
      }, 650);
      plan = await request;
    }
    state.developProgress = null;
    state.developPlan = plan;
    state.styleScope = currentStyleScope(plan);
    syncDevelopSummary(plan);
    let stage = requestedStage || stageFromDevelopRoute(route);
    const cropComplete = Number(plan?.eligible_count || 0) > 0
      && Number(plan?.confirmed_count || 0) === Number(plan?.eligible_count || 0)
      && !plan?.stale;
    if (["base", "style"].includes(stage) && !cropComplete) stage = "crop";
    if (stage === "style" && !currentBaseComplete(plan)) stage = "base";
    state.developStage = stage;
    if (!enteredGeneration || location.hash.replace(/^#/, "").startsWith(`develop/${run.run_id}/`)) {
      setView("develop", `develop/${run.run_id}/${stage}`, historyMode);
    }
  } catch (error) {
    state.developProgress = {
      ...(state.developProgress || {}),
      status: "failed",
      stage_label: "构图分析失败",
      message: error.message,
      run_id: run.run_id,
    };
    renderDevelop();
    throw error;
  } finally {
    if (progressTimer) clearInterval(progressTimer);
    state.developBusy = false;
    renderDevelop();
  }
}

async function updateDevelop(index, changes) {
  if (!state.currentRun || !state.developPlan || state.developBusy) return;
  state.developBusy = true;
  renderDevelop();
  try {
    const plan = await api(`/api/runs/${state.currentRun.run_id}/develop/${index}`, {
      method: "PATCH",
      body: { base_revision: Number(state.developPlan.revision), ...changes },
    });
    state.developPlan = plan;
    syncDevelopSummary(plan);
  } catch (error) {
    toast(error.message);
    if (error.message.includes("刷新") || error.message.includes("重新生成")) await loadDevelop(false, null, state.developStage);
  } finally {
    state.developBusy = false;
    renderDevelop();
  }
}

async function confirmAllDevelop(announce = true) {
  if (!state.currentRun || !state.developPlan || state.developBusy) return null;
  state.developBusy = true;
  state.developFlowProgress = { status: "running", stage_label: "正在核对最新构图", overall_percent: 20 };
  renderDevelop();
  try {
    let plan = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const latest = await api(`/api/runs/${state.currentRun.run_id}/develop`);
      if (!latest?.exists || latest.stale) throw new Error("构图方案已经变化，请重新分析后再继续。 ");
      state.developPlan = latest;
      syncDevelopSummary(latest);
      state.developFlowProgress = { status: "running", stage_label: "正在保存当前构图", overall_percent: 65 };
      renderDevelop();
      try {
        plan = await api(`/api/runs/${state.currentRun.run_id}/develop/confirm-all`, {
          method: "POST",
          body: { base_revision: Number(latest.revision) },
        });
        break;
      } catch (error) {
        if (attempt > 0 || (!error.message.includes("更新") && !error.message.includes("刷新"))) throw error;
      }
    }
    if (!plan) throw new Error("构图确认未完成，请重试。 ");
    state.developPlan = plan;
    syncDevelopSummary(plan);
    if (announce) toast(`已保存 ${plan.confirmed_count} 张构图`);
    return plan;
  } catch (error) {
    toast(error.message);
    return null;
  } finally {
    state.developBusy = false;
    state.developFlowProgress = null;
    renderDevelop();
  }
}

async function advanceFromCrop() {
  if (!state.currentRun || state.developBusy) return;
  state.developBusy = true;
  state.developFlowProgress = { status: "running", stage_label: "正在读取最新构图状态", overall_percent: 10 };
  renderDevelop();
  try {
    let plan = await api(`/api/runs/${state.currentRun.run_id}/develop`);
    state.developPlan = plan;
    syncDevelopSummary(plan);
    if (!plan?.exists || plan.stale) {
      toast("构图方案已经变化，请重新分析后再继续。");
      return;
    }
    state.developBusy = false;
    const allConfirmed = Number(plan.confirmed_count || 0) === Number(plan.eligible_count || 0);
    if (!allConfirmed) {
      plan = await confirmAllDevelop(false);
      if (!plan || Number(plan.confirmed_count || 0) !== Number(plan.eligible_count || 0)) return;
    }
    await goWorkflowStage("base");
  } finally {
    state.developBusy = false;
    state.developFlowProgress = null;
    renderDevelop();
  }
}

async function skipCropDevelop() {
  if (!state.currentRun || !state.developPlan || state.developBusy) return;
  state.developBusy = true;
  renderDevelop();
  try {
    const plan = await api(`/api/runs/${state.currentRun.run_id}/develop/skip-crop`, {
      method: "POST",
      body: { base_revision: Number(state.developPlan.revision) },
    });
    state.developPlan = plan;
    syncDevelopSummary(plan);
    toast("已保留全部原始构图");
  } catch (error) {
    toast(error.message);
    throw error;
  } finally {
    state.developBusy = false;
    renderDevelop();
  }
}

async function setColorMode(mode) {
  if (!state.currentRun || !state.developPlan || state.developBusy) return false;
  if (currentColorMode(state.developPlan) === mode && currentColorComplete(state.developPlan)) {
    renderDevelop();
    return true;
  }
  state.developBusy = true;
  state.developFlowProgress = { status: "running", stage_label: mode === "style" ? "正在准备创意外观" : "正在保存基础调色", overall_percent: 85 };
  renderDevelop();
  try {
    let plan = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const latest = attempt === 0
        ? state.developPlan
        : await api(`/api/runs/${state.currentRun.run_id}/develop`);
      state.developPlan = latest;
      syncDevelopSummary(latest);
      try {
        plan = await api(`/api/runs/${state.currentRun.run_id}/develop/options`, {
          method: "POST",
          body: { base_revision: Number(latest.revision), mode, color_enabled: mode !== "skip" },
        });
        break;
      } catch (error) {
        if (attempt > 0 || (!error.message.includes("更新") && !error.message.includes("刷新"))) throw error;
      }
    }
    if (!plan) throw new Error("设置保存失败，请重试。 ");
    state.developPlan = plan;
    state.styleScope = currentStyleScope(plan);
    syncDevelopSummary(plan);
    return true;
  } catch (error) {
    toast(error.message);
    return false;
  } finally {
    state.developBusy = false;
    state.developFlowProgress = null;
    renderDevelop();
  }
}

function setStyleScope(scope) {
  if (!state.developPlan || state.developBusy || state.activeJob) return;
  const nextScope = scope === "global" ? "global" : "group";
  if (currentStyleScope(state.developPlan) === nextScope) return;
  state.styleScope = nextScope;
  state.developPlan.creative_style ||= { status: "pending", groups: {} };
  state.developPlan.creative_style.scope = nextScope;
  state.developPlan.creative_style.status = "pending";
  renderDevelop();
}

async function recommendStyles(groupId = null, scope = state.styleScope) {
  if (!state.currentRun || !state.developPlan || state.developBusy || state.activeJob) return;
  if (!await requireModelSetupUi()) return;
  const resolvedScope = scope === "global" ? "global" : "group";
  if (resolvedScope === "group" && groupId == null) {
    toast("请从具体照片组启动 AI 推荐。 ");
    return;
  }
  if (currentColorMode(state.developPlan) !== "style") {
    const enabled = await setColorMode("style");
    if (!enabled || !state.developPlan || state.activeJob) return;
  }
  state.developBusy = true;
  renderDevelop();
  try {
    const job = await api(`/api/runs/${state.currentRun.run_id}/style-recommendations`, {
      method: "POST",
      body: {
        base_revision: Number(state.developPlan.revision),
        scope: resolvedScope,
        group_id: resolvedScope === "group" ? Number(groupId) : null,
      },
    });
    activateJob(job);
  } catch (error) {
    toast(error.message);
  } finally {
    state.developBusy = false;
    renderDevelop();
  }
}

async function recommendStylesForAllGroups() {
  if (!state.currentRun || !state.developPlan || state.developBusy || state.activeJob) return;
  if (!await requireModelSetupUi()) return;
  const groupIds = new Set((state.developPlan.items || []).map((item) => Number(item.group_id)));
  if (!groupIds.size) {
    toast("没有可生成外观的照片组。 ");
    return;
  }
  if (currentColorMode(state.developPlan) !== "style") {
    const enabled = await setColorMode("style");
    if (!enabled || !state.developPlan || state.activeJob) return;
  }
  state.styleScope = "group";
  state.developPlan.creative_style ||= { status: "pending", groups: {} };
  state.developPlan.creative_style.scope = "group";
  state.developPlan.creative_style.status = "pending";
  state.developBusy = true;
  renderDevelop();
  try {
    const job = await api(`/api/runs/${state.currentRun.run_id}/style-recommendations/groups`, {
      method: "POST",
      body: { base_revision: Number(state.developPlan.revision) },
    });
    activateJob(job);
  } catch (error) {
    toast(error.message);
  } finally {
    state.developBusy = false;
    renderDevelop();
  }
}

async function requestStylePreview(groupId, { lutId = null, lutHash = null, presetId = null, presetHash = null, amount = null, scope = null } = {}) {
  if (!state.currentRun || !state.developPlan || state.developBusy || state.activeJob) return false;
  if (!await requireModelSetupUi()) return false;
  const resolvedScope = scope === "global" || (scope == null && state.styleScope === "global") ? "global" : "group";
  if (currentColorMode(state.developPlan) !== "style") {
    const enabled = await setColorMode("style");
    if (!enabled || !state.developPlan || state.activeJob) return false;
  }
  const selection = resolvedScope === "global"
    ? (state.developPlan.creative_style?.global_selection || {})
    : (state.developPlan.creative_style?.groups?.[String(groupId)] || {});
  const recommendedId = selection.recommended_lut_id || selection.recommended_preset_id;
  const recommendedChoice = (selection.top3 || []).find((choice) =>
    String(choice.lut_id || choice.preset_id || "") === String(recommendedId || "")
  ) || {};
  const resolvedLutId = lutId || selection.lut_id || recommendedChoice.lut_id;
  const resolvedLutHash = lutHash || selection.lut_hash || recommendedChoice.lut_hash;
  // A calibrated LUT is the authoritative creative resource. Some catalog
  // results retain their source preset metadata for provenance; never submit
  // both pairs because the API deliberately accepts exactly one resource.
  const resolvedPresetId = resolvedLutId ? null : (presetId || selection.preset_id || recommendedChoice.preset_id);
  const resolvedPresetHash = resolvedLutId ? null : (presetHash || selection.preset_hash || recommendedChoice.preset_hash);
  const resolvedId = resolvedLutId || resolvedPresetId;
  const resolvedHash = resolvedLutId ? resolvedLutHash : resolvedPresetHash;
  if (!resolvedId || !resolvedHash) {
    toast("此外观缺少可验证资源，请重新推荐。 ");
    return false;
  }
  const requestedAmount = Number(amount ?? selection.strength ?? selection.amount ?? 100);
  if (!Number.isFinite(requestedAmount)) {
    toast("风格强度无效，请重新选择。 ");
    return false;
  }
  const resolvedAmount = Math.max(0, Math.min(200, Math.round(requestedAmount)));
  const previewsCurrentSelection = String(resolvedId) === String(selection.lut_id || selection.preset_id || "");
  const previousAmount = selection.amount;
  const previousStrength = selection.strength;
  if (previewsCurrentSelection) {
    selection.amount = resolvedAmount;
    if (resolvedLutId) selection.strength = resolvedAmount;
  }
  state.developBusy = true;
  renderDevelop();
  try {
    const job = await api(`/api/runs/${state.currentRun.run_id}/style-preview`, {
      method: "POST",
      body: {
        base_revision: Number(state.developPlan.revision),
        scope: resolvedScope,
        group_id: resolvedScope === "global" ? null : Number(groupId),
        ...(resolvedLutId ? { lut_id: resolvedLutId, lut_hash: resolvedLutHash } : {}),
        ...(resolvedPresetId ? { preset_id: resolvedPresetId, preset_hash: resolvedPresetHash } : {}),
        amount: resolvedAmount,
        ...(resolvedLutId ? { strength: resolvedAmount } : {}),
      },
    });
    activateJob(job);
    return true;
  } catch (error) {
    if (previewsCurrentSelection) {
      selection.amount = previousAmount;
      selection.strength = previousStrength;
    }
    toast(error.message);
    return false;
  } finally {
    state.developBusy = false;
    renderDevelop();
  }
}

async function updateStyleGroup(groupId, changes) {
  if (!state.currentRun || !state.developPlan || state.developBusy) return;
  state.developBusy = true;
  renderDevelop();
  try {
    const plan = await api(`/api/runs/${state.currentRun.run_id}/develop/style/groups/${groupId}`, {
      method: "PUT",
      body: { base_revision: Number(state.developPlan.revision), ...changes },
    });
    state.developPlan = plan;
    syncDevelopSummary(plan);
  } catch (error) {
    toast(error.message);
  } finally {
    state.developBusy = false;
    renderDevelop();
  }
}

async function updateStyleGlobal(changes) {
  if (!state.currentRun || !state.developPlan || state.developBusy) return;
  state.developBusy = true;
  renderDevelop();
  try {
    const plan = await api(`/api/runs/${state.currentRun.run_id}/develop/style/global`, {
      method: "PUT",
      body: { base_revision: Number(state.developPlan.revision), ...changes },
    });
    state.developPlan = plan;
    state.styleScope = "global";
    syncDevelopSummary(plan);
  } catch (error) {
    toast(error.message);
  } finally {
    state.developBusy = false;
    renderDevelop();
  }
}

async function confirmRecommendedStyles() {
  if (!state.currentRun || !state.developPlan || state.developBusy) return;
  if (state.styleScope === "global") {
    const selection = state.developPlan.creative_style?.global_selection || {};
    if (selection.recommendation_status === "complete" && selection.recommended_kind === "preset") {
      const candidateId = selection.recommended_lut_id || selection.recommended_preset_id;
      const candidate = (selection.top3 || []).find((choice) =>
        String(choice.lut_id || choice.preset_id || "") === String(candidateId || "")
      );
      if (candidate) {
        await requestStylePreview(null, {
          scope: "global",
          lutId: candidate.lut_id || null,
          lutHash: candidate.lut_hash || null,
          presetId: candidate.preset_id || null,
          presetHash: candidate.preset_hash || null,
          amount: Number(candidate.strength ?? candidate.amount ?? selection.recommended_amount ?? 100),
        });
        return;
      }
    }
  }
  state.developBusy = true;
  renderDevelop();
  try {
    const plan = await api(`/api/runs/${state.currentRun.run_id}/develop/style/confirm-all`, {
      method: "POST",
      body: { base_revision: Number(state.developPlan.revision), scope: state.styleScope },
    });
    state.developPlan = plan;
    syncDevelopSummary(plan);
    toast(state.styleScope === "global" ? "已采用统一 AI 外观" : "已采用各组 AI 推荐");
  } catch (error) {
    toast(error.message);
  } finally {
    state.developBusy = false;
    renderDevelop();
  }
}

async function syncStyleLibrary(overrides = {}, successMessage = "风格库已更新") {
  if (state.styleLibraryBusy) return;
  const current = state.styleLibrary?.settings || {};
  const requested = {
    include_lightroom_presets: overrides.include_lightroom_presets ?? current.include_lightroom_presets ?? true,
    include_user_uploads: overrides.include_user_uploads ?? current.include_user_uploads ?? true,
  };
  if (state.styleLibrary) state.styleLibrary = { ...state.styleLibrary, settings: { ...(state.styleLibrary.settings || {}), ...requested } };
  state.styleLibraryBusy = true;
  renderStyleLibrary();
  try {
    const result = await api("/api/style-library/sync", {
      method: "POST",
      body: requested,
    });
    state.styleLibrary = result.library || result;
    renderStyleLibrary();
    toast(successMessage);
  } catch (error) {
    toast(error.message);
  } finally {
    state.styleLibraryBusy = false;
    renderStyleLibrary();
  }
}

async function setStyleSourceEnabled(source, enabled) {
  const overrides = source === "user"
    ? { include_user_uploads: enabled }
    : { include_lightroom_presets: enabled };
  await syncStyleLibrary(overrides, `${source === "user" ? "用户上传" : "Lightroom"}风格已${enabled ? "启用" : "停用"}`);
}

async function setStyleLibraryItemHidden(resourceId, hidden) {
  if (!resourceId || state.styleLibraryItemBusy || state.styleLibraryBusy) return;
  const item = state.styleLibrary?.presets?.find((candidate) => candidate.resource_id === resourceId);
  if (!item) return;
  state.styleLibraryItemBusy = resourceId;
  renderStyleLibraryManager();
  try {
    const result = await api(`/api/style-library/items/${encodeURIComponent(resourceId)}`, {
      method: "PATCH",
      body: { hidden },
    });
    state.styleLibrary = result.library;
    renderStyleLibrary();
    toast(`“${item.label || "未命名风格"}”已${hidden ? "停用" : "启用"}，AI 索引已更新`);
  } catch (error) {
    toast(error.message);
  } finally {
    state.styleLibraryItemBusy = null;
    renderStyleLibraryManager();
  }
}

async function deleteStyleLibraryItem(resourceId) {
  if (!resourceId || state.styleLibraryItemBusy) return;
  const item = state.styleLibrary?.presets?.find((candidate) => candidate.resource_id === resourceId);
  if (!item || !window.confirm(`从风格库删除“${item.label || "未命名风格"}”？\n原资源会保留在所选数据目录的归档中。`)) return;
  state.styleLibraryItemBusy = resourceId;
  renderStyleLibraryManager();
  try {
    const result = await api(`/api/style-library/items/${encodeURIComponent(resourceId)}`, { method: "DELETE" });
    state.styleLibrary = result.library;
    renderStyleLibrary();
    toast("已从风格库删除，AI 索引已更新");
  } catch (error) {
    toast(error.message);
  } finally {
    state.styleLibraryItemBusy = null;
    renderStyleLibraryManager();
  }
}

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "").split(",").at(-1) || ""));
    reader.addEventListener("error", () => reject(new Error(`无法读取 ${file.name}`)));
    reader.readAsDataURL(file);
  });
}

async function importStyleLibraryFiles() {
  const input = $("#style-library-files");
  const files = [...input.files];
  if (!files.length || state.styleImportBusy) return;
  if (files.length > 500) return toast("单次最多导入 500 个文件。 ");
  if (files.reduce((total, file) => total + file.size, 0) > 96 * 1024 * 1024) {
    return toast("单次批量导入不能超过 96 MB。 ");
  }
  state.styleImportBusy = true;
  renderStyleLibrary();
  const status = $("#style-library-import-status");
  status.textContent = "正在上传并增量重建索引…";
  status.classList.remove("hidden");
  try {
    const payload = await Promise.all(files.map(async (file) => ({
      name: file.name,
      content_base64: await readFileBase64(file),
    })));
    const result = await api("/api/style-library/import", {
      method: "POST",
      body: { files: payload },
    });
    state.styleLibrary = result.library;
    input.value = "";
    const failed = Number(result.failed_count || 0);
    const indexCopy = result.registration_pending ? "Lightroom 将自动完成注册" : "AI 索引已更新";
    status.textContent = `${result.imported_count} 个已上传 · ${result.reused_count} 个复用${failed ? ` · ${failed} 个失败` : ""} · ${indexCopy}`;
    toast(failed ? "上传完成，部分文件未通过校验" : `风格已上传，${indexCopy}`);
  } catch (error) {
    status.textContent = error.message;
    toast(error.message);
  } finally {
    state.styleImportBusy = false;
    renderStyleLibrary();
  }
}

async function executeExport() {
  if (!state.currentRun || state.activeJob) return;
  const xmp = Boolean(state.exportTargets.xmp);
  const jpeg = Boolean(state.exportTargets.jpeg);
  if (!xmp && !jpeg) return toast("请至少选择 XMP 或 JPEG。 ");
  try {
    const retry = ["failed", "partial", "partial_failure"].includes(state.exportSpec?.status)
      && state.exportSpec?.run_id === state.currentRun.run_id;
    const spec = retry ? state.exportSpec : await api(`/api/runs/${state.currentRun.run_id}/exports/prepare`, {
        method: "POST",
        body: {
          base_revision: Number(state.currentRun.review_revision),
          develop_revision: state.developPlan?.exists ? Number(state.developPlan.revision) : null,
          xmp,
          jpeg,
          jpeg_output_dir: jpeg ? $("#export-directory").value.trim() : null,
          jpeg_settings: {
            color_space: "sRGB",
            size: "original",
            quality: 90,
            sharpening: "screen_standard",
            collision: "suffix",
            ...(state.bootstrap?.preferences?.export_defaults?.jpeg_settings || {}),
          },
        },
      });
    state.exportSpec = spec;
    renderExport();
    const execution = await api(`/api/exports/${encodeURIComponent(spec.export_spec_id)}/execute`, {
      method: "POST",
      body: { retry_failed_only: retry },
    });
    if (execution?.id) {
      state.activeJob = execution;
      state.jobs.unshift(execution);
      state.exportSpec = { ...spec, status: execution.status || "queued", job_id: execution.id };
      renderJobBar();
      renderExport();
      pollJobs();
    } else {
      state.exportSpec = execution;
      renderExport();
    }
  } catch (error) {
    toast(error.message);
    if (state.exportSpec) state.exportSpec = { ...state.exportSpec, status: "failed", message: error.message };
    renderExport();
  }
}

function renderStyleSearch() {
  const query = $("#style-search-input").value.trim().toLowerCase();
  const presets = state.styleLibrary?.presets || [];
  const matches = presets.filter((preset) => {
    if (!preset.ai_enabled) return false;
    const haystack = [preset.label, preset.name, preset.source, preset.category, preset.tier].filter(Boolean).join(" ").toLowerCase();
    return !query || haystack.includes(query);
  }).slice(0, 120);
  const controlsBusy = state.developBusy || Boolean(state.activeJob);
  $("#style-search-results").innerHTML = matches.map((preset) => {
    const lutId = preset.lut_id || "";
    const presetId = preset.preset_id || "";
    return `<article class="style-search-result"><div><strong>${escapeHtml(preset.label || preset.name)}</strong><small>${escapeHtml([preset.source, preset.category].filter(Boolean).join(" · "))}</small></div><button type="button" class="button" data-style-search-select="${escapeHtml(lutId || presetId)}" data-lut-id="${escapeHtml(lutId)}" data-lut-hash="${escapeHtml(preset.lut_hash || "")}" data-preset-id="${escapeHtml(presetId)}" data-preset-hash="${escapeHtml(preset.preset_hash || preset.hash || "")}" ${controlsBusy ? "disabled" : ""}>预览</button></article>`;
  }).join("") || `<div class="empty">没有匹配的外观</div>`;
}

function openStyleSearch(groupId = null) {
  state.styleSearchGroupId = groupId == null ? null : Number(groupId);
  $("#style-search-input").value = "";
  renderStyleSearch();
  $("#style-search-dialog").showModal();
}

async function configureLightroom(autoDetect = false) {
  if (state.lightroomBusy) return;
  const path = $("#lightroom-path").value.trim();
  if (!autoDetect && !path) return toast("请填写 Lightroom.exe 或安装文件夹。 ");
  state.lightroomBusy = true;
  state.lightroomAction = autoDetect ? "auto" : "manual";
  state.lightroomMessage = { kind: "progress", text: autoDetect ? "正在识别 Lightroom 并检查插件连接…" : "正在保存路径并检查插件连接…" };
  renderLightroom();
  try {
    state.lightroomStatus = await api("/api/lightroom/configure", {
      method: "POST",
      body: { executable_path: autoDetect ? null : path },
    });
    const online = state.lightroomStatus?.heartbeat?.state === "online";
    state.lightroomMessage = {
      kind: "success",
      text: online
        ? "已确认：Lightroom 路径、插件配置和当前连接均正常。"
        : "配置已保存。打开 Lightroom 并启用插件后会自动连接。",
    };
    renderLightroom();
    toast(online ? "Lightroom 连接正常" : "Lightroom 配置已保存");
    if (state.lightroomStatus?.configuration?.restart_required) {
      window.alert("Lightroom 插件已安装或更新。请完全退出并重新启动 Lightroom Classic，然后返回这里点击“刷新连接”。");
    }
  } catch (error) {
    state.lightroomMessage = { kind: "error", text: error.message };
    toast(error.message);
  } finally {
    state.lightroomBusy = false;
    state.lightroomAction = null;
    renderLightroom();
  }
}

async function refreshLightroomConnection() {
  if (state.lightroomBusy) return;
  state.lightroomBusy = true;
  state.lightroomAction = "refresh";
  state.lightroomMessage = { kind: "progress", text: "正在重新读取 Lightroom 插件连接…" };
  renderLightroom();
  try {
    state.lightroomStatus = await api(`/api/lightroom/status?refresh=${Date.now()}`);
    const online = state.lightroomStatus?.heartbeat?.state === "online";
    state.lightroomMessage = {
      kind: online ? "success" : "error",
      text: online
        ? "连接已刷新：Lightroom 插件在线。"
        : "尚未连接。若刚安装或更新插件，请完全退出并重新启动 Lightroom 后再刷新。",
    };
    toast(online ? "Lightroom 连接已刷新" : "Lightroom 插件尚未连接");
  } catch (error) {
    state.lightroomMessage = { kind: "error", text: `刷新失败：${error.message}` };
    toast(error.message);
  } finally {
    state.lightroomBusy = false;
    state.lightroomAction = null;
    renderLightroom();
  }
}

async function loadExport(historyMode = "push") {
  if (!state.currentRun?.xmp_ready) throw new Error("请先完成 AI 评分。 ");
  let plan = state.developPlan;
  if (!plan || plan.stale || Number(plan.source_review_revision) !== Number(state.currentRun.review_revision)) {
    plan = await api(`/api/runs/${state.currentRun.run_id}/develop`);
  }
  if (!currentCropComplete(plan)) throw new Error("请先完成或跳过构图。 ");
  if (!currentBaseComplete(plan)) throw new Error("请先选择基础调色方式。 ");
  if (!currentStyleComplete(plan)) throw new Error("请先确认或跳过创意外观。 ");
  state.developPlan = plan;
  syncDevelopSummary(plan);
  setView("export", `export/${state.currentRun.run_id}`, historyMode);
  renderExport();
}

async function goWorkflowStage(stage) {
  if (!state.currentRun) return;
  if (stage === "color") stage = "style";
  if (stage === "review") {
    state.developStage = "crop";
    renderReview();
    setView("review", currentProjectRoute(), "push");
    return;
  }
  if (!state.currentRun.xmp_ready) {
    toast("请先确认分组并完成 AI 评分。 ");
    return;
  }
  if (stage === "export") {
    if ($("#view-export").classList.contains("active")) return;
    if ($("#view-develop").classList.contains("active") && state.developStage === "style") {
      await loadExport();
      return;
    }
    toast("请按顺序完成或跳过可选步骤。 ");
    return;
  }
  if (stage === "crop") {
    await loadDevelop(false, null, "crop");
    return;
  }
  let plan = state.developPlan;
  if (!plan || plan.stale || Number(plan.source_review_revision) !== Number(state.currentRun.review_revision)) {
    plan = await api(`/api/runs/${state.currentRun.run_id}/develop`);
  }
  if (!plan?.exists || plan.stale || Number(plan.confirmed_count || 0) !== Number(plan.eligible_count || 0)) {
    toast("请先在构图步骤确认全部照片。 ");
    await loadDevelop(false, null, "crop");
    return;
  }
  state.developPlan = plan;
  state.styleScope = currentStyleScope(plan);
  syncDevelopSummary(plan);
  if (stage === "style" && !currentBaseComplete(plan)) {
    toast("请先选择或跳过基础调色。 ");
    stage = "base";
  }
  state.developStage = stage === "style" ? "style" : "base";
  setView("develop", `develop/${state.currentRun.run_id}/${state.developStage}`, "push");
  renderDevelop();
}

async function setRating(index, rating) {
  if (!state.currentRun || state.groupingMode || !state.currentRun.xmp_ready) return;
  const parsed = rating === "reset" ? null : Number(rating);
  try {
    const update = await api(`/api/runs/${state.currentRun.run_id}/items/${index}`, {
      method: "PATCH",
      body: { rating: parsed, base_revision: state.currentRun.review_revision },
    });
    const item = state.currentRun.results.find((value) => value.index === Number(index));
    if (!item) return;
    if (parsed == null) {
      item.rating = item.ai_rating;
      item.effective_rating = item.ai_rating;
      item.manual_override = false;
      delete item.manual_rating;
    } else {
      item.rating = parsed;
      item.effective_rating = parsed;
      item.manual_rating = parsed;
      item.manual_override = true;
    }
    state.currentRun.review_revision = update.review_revision;
    if (state.currentRun.develop?.exists) {
      state.currentRun.develop.stale = true;
      state.currentRun.develop.confirmed_count = 0;
    }
    state.developPlan = null;
    state.currentRun.manual_adjusted_count = update.manual_adjusted_count;
    state.currentRun.candidate_count = state.currentRun.results.filter((value) => value.effective_rating >= 3).length;
    state.currentRun.strong_count = state.currentRun.results.filter((value) => value.effective_rating >= 4).length;
    syncProjectRunStats(state.currentRun);
    renderReview();
  } catch (error) {
    toast(error.message);
    if (error.message.includes("刷新")) await openRun(state.currentRun.run_id);
  }
}

function updatePhotoDialog(index) {
  const item = state.currentRun?.results.find((value) => value.index === Number(index));
  if (!item) return;
  state.dialogIndex = item.index;
  $("#dialog-image").src = item.preview_url;
  const grouping = state.groupingMode || state.currentRun.workflow_state !== "scored" || state.currentRun.needs_rescore;
  $("#dialog-name").textContent = grouping
    ? `${item.filename} · 组 ${item.group_id}`
    : item.manual_override ? `${item.filename} · 你 ${item.effective_rating} / AI ${item.ai_rating}` : item.filename;
  $("#dialog-ratings").innerHTML = grouping ? "" : ratingControls(item, true);
  if (grouping) {
    $("#dialog-analysis").innerHTML = `<p>${item.manual_group_override ? "已人工调整分组" : "AI 自动分组"}</p>`;
    return;
  }
  const components = item.components || {};
  const labels = {
    vlm: "视觉综合", composition: "构图", light: "光线", subject_layers: "层次", color: "色彩",
    edit_potential: "后期潜力", distraction: "干扰（低好）", aesthetic: "审美", quality: "画质",
  };
  const scores = Object.entries(labels)
    .filter(([key]) => Number.isFinite(Number(components[key])))
    .map(([key, label]) => `${label} ${Math.round(Number(components[key]) * 100)}`)
    .join(" · ");
  const reason = item.reason || {};
  const strengths = (reason.strengths || []).join("；");
  const issues = (reason.issues || []).join("；");
  $("#dialog-analysis").innerHTML = `${reason.summary ? `<p>${escapeHtml(reason.summary)}</p>` : ""}
    ${scores ? `<div class="analysis-scores">${escapeHtml(scores)}</div>` : ""}
    ${strengths ? `<div class="analysis-detail">优点：${escapeHtml(strengths)}</div>` : ""}
    ${issues ? `<div class="analysis-detail">留意：${escapeHtml(issues)}</div>` : ""}`;
}

function openPhoto(index) {
  updatePhotoDialog(index);
  $("#photo-dialog").showModal();
}

function openGroupDialog(value) {
  const run = state.currentRun;
  const requested = Array.isArray(value) ? value : [value];
  const indexes = [...new Set(requested.map(Number))].filter((index) =>
    run?.results.some((item) => item.index === index && !item.excluded)
  );
  if (!run || !indexes.length) return;
  const item = run.results.find((candidate) => candidate.index === indexes[0]);
  state.groupDialogIndices = indexes;
  const counts = new Map();
  run.results.filter((candidate) => !candidate.excluded).forEach((candidate) =>
    counts.set(candidate.group_id, (counts.get(candidate.group_id) || 0) + 1)
  );
  const options = [...counts.entries()]
    .sort((a, b) => Number(a[0]) - Number(b[0]))
    .map(([groupId, count]) => `<option value="${groupId}" ${indexes.length === 1 && Number(groupId) === Number(item.group_id) ? "selected" : ""}>组 ${groupId}（${count} 张）</option>`)
    .join("");
  const restore = indexes.length === 1 && item.manual_group_override
    ? `<option value="auto">恢复 AI 分组（组 ${item.ai_group_id}）</option>`
    : "";
  const choose = indexes.length > 1 ? `<option value="" selected disabled>选择目标组</option>` : "";
  $("#group-target").innerHTML = `${choose}${options}<option value="new">新建一组</option>${restore}`;
  $("#group-dialog-copy").textContent = indexes.length === 1 ? item.filename : `已选择 ${indexes.length} 张照片`;
  $("#group-dialog").showModal();
}

async function applyGroupChange(indexes, { selected = null, direction = null, closeDialog = false } = {}) {
  const run = state.currentRun;
  indexes = [...new Set(indexes.map(Number))];
  if (!run || !indexes.length) return;
  if (state.activeJob) return toast("请等待当前任务完成");
  const body = { indexes, base_revision: run.review_revision };
  if (direction) body.direction = direction;
  else if (selected === "new") body.new_group = true;
  else body.group_id = selected === "auto" ? null : Number(selected);
  const keepBatch = state.batchGrouping;
  if (closeDialog) $("#group-submit").disabled = true;
  state.batchBusy = true;
  renderReview();
  try {
    const update = await api(`/api/runs/${run.run_id}/groups`, {
      method: "PATCH",
      body,
    });
    if (closeDialog && $("#group-dialog").open) $("#group-dialog").close();
    const route = location.hash.replace(/^#/, "") || "review";
    await openRun(run.run_id, route);
    state.groupingMode = true;
    state.batchGrouping = keepBatch || indexes.length > 1;
    renderReview();
    if (direction) {
      const moved = Number(update.moved_count || 0);
      const unchanged = indexes.length - moved;
      const action = direction === "previous" ? "上移一组" : "下移一组";
      toast(moved ? `${moved} 张照片已${action}${unchanged ? `，${unchanged} 张已在边界` : ""}` : `所选照片已在${direction === "previous" ? "最前" : "最后"}一组`);
    } else {
      toast(selected === "auto"
        ? "已恢复 AI 分组"
        : `${indexes.length} 张照片已移到组 ${update.group_id}`);
    }
  } catch (error) {
    toast(error.message);
    if (error.message.includes("刷新")) await openRun(run.run_id);
  } finally {
    state.batchBusy = false;
    if (closeDialog) $("#group-submit").disabled = false;
    renderReview();
  }
}

async function moveGroup() {
  const run = state.currentRun;
  const indexes = [...state.groupDialogIndices];
  if (!run || !indexes.length) return;
  const selected = $("#group-target").value;
  if (!selected) return toast("请选择目标分组");
  const item = run.results.find((candidate) => candidate.index === indexes[0]);
  const groupId = selected === "auto" || selected === "new" ? null : Number(selected);
  if (indexes.length === 1 && groupId === Number(item.group_id) && selected !== "auto" && selected !== "new") {
    $("#group-dialog").close();
    return;
  }
  await applyGroupChange(indexes, { selected, closeDialog: true });
}

function toggleBatchGrouping() {
  if (!state.currentRun || state.activeJob || state.batchBusy) return;
  state.batchGrouping = !state.batchGrouping;
  clearGroupingSelection();
  renderReview();
}

function selectGroupingIndex(index, selected, range = false) {
  const active = state.currentRun?.results.filter((item) => !item.excluded).map((item) => item.index) || [];
  if (range && state.selectionAnchor != null) {
    const start = active.indexOf(state.selectionAnchor);
    const end = active.indexOf(index);
    if (start >= 0 && end >= 0) {
      active.slice(Math.min(start, end), Math.max(start, end) + 1).forEach((itemIndex) =>
        selected ? state.selectedIndices.add(itemIndex) : state.selectedIndices.delete(itemIndex)
      );
    }
  } else if (selected) {
    state.selectedIndices.add(index);
  } else {
    state.selectedIndices.delete(index);
  }
  state.selectionAnchor = index;
  renderReview();
}

async function updateExcluded(indexes, excluded) {
  const run = state.currentRun;
  indexes = [...new Set(indexes.map(Number))];
  if (!run || !indexes.length || state.activeJob || state.batchBusy) return;
  state.batchBusy = true;
  renderReview();
  try {
    await api(`/api/runs/${run.run_id}/excluded`, {
      method: "PATCH",
      body: { indexes, excluded, base_revision: run.review_revision },
    });
    const route = location.hash.replace(/^#/, "") || "review";
    const keepBatch = state.batchGrouping;
    await openRun(run.run_id, route);
    state.groupingMode = true;
    state.batchGrouping = keepBatch;
    renderReview();
    toast(excluded
      ? `${indexes.length} 张已移出工程，RAW 文件未删除`
      : `${indexes.length} 张已恢复到工程`);
  } catch (error) {
    toast(error.message);
    if (error.message.includes("刷新")) await openRun(run.run_id);
  } finally {
    state.batchBusy = false;
    renderReview();
  }
}

function toggleGrouping() {
  if (!state.currentRun || state.currentRun.needs_rescore) return;
  state.groupingMode = !state.groupingMode;
  state.batchGrouping = false;
  clearGroupingSelection();
  if ($("#photo-dialog").open) $("#photo-dialog").close();
  renderReview();
}

async function scoreCurrentGroups() {
  const run = state.currentRun;
  if (!run) return;
  if (!await requireModelSetupUi()) return;
  state.batchGrouping = false;
  clearGroupingSelection();
  await startJob(`/api/runs/${run.run_id}/score`, {
    base_revision: Number(run.review_revision),
    retain_ratio: Number($("#retain-ratio").value || 0.30),
    mode: $("#scoring-mode").value === "fast" ? "fast" : "deep",
  });
}

async function startJob(path, body) {
  try {
    state.jobNotice = null;
    const job = await api(path, { method: "POST", body });
    return activateJob(job);
  } catch (error) {
    toast(error.message);
    throw error;
  }
}

async function retryFailedJob(requestedJob = currentProgressJob()) {
  const job = typeof requestedJob === "string"
    ? state.jobs.find((item) => item.id === requestedJob) || state.jobNotice
    : requestedJob;
  if (!job || !job.retryable || state.activeJob || state.jobRetryBusy) return;
  state.jobRetryBusy = true;
  const previousNotice = state.jobNotice;
  renderJobBar();
  renderModelInstallProgress(job);
  try {
    if (job.retry_mode === "export_failed") {
      const exportSpecId = job.context?.export_spec_id;
      if (!exportSpecId) throw new Error("导出失败项清单不存在，请返回导出步骤重新准备。 ");
      const spec = await api(`/api/exports/${encodeURIComponent(exportSpecId)}`);
      const execution = await api(`/api/exports/${encodeURIComponent(exportSpecId)}/execute`, {
        method: "POST",
        body: { retry_failed_only: true },
      });
      state.exportSpec = execution?.id ? { ...spec, status: execution.status || "queued", job_id: execution.id } : execution;
      if (execution?.id) activateJob(execution);
      else {
        toast("失败项已处理完毕，无需再次运行");
        renderExport();
      }
      return;
    }
    if (job.retry_mode === "develop") {
      const runId = String(job.context?.run_id || "");
      if (!runId) throw new Error("构图任务缺少工程信息，请返回选片步骤重新进入。 ");
      if (String(state.currentRun?.run_id || "") !== runId) await openRun(runId);
      else state.currentRun = await api(`/api/runs/${runId}`);
      state.jobNotice = null;
      state.developProgress = null;
      await loadDevelop(true, null, "crop", true, "replace");
      return;
    }
    const retried = await api(`/api/jobs/${encodeURIComponent(job.id)}/retry`, { method: "POST" });
    activateJob(retried);
  } catch (error) {
    if (!state.activeJob) state.jobNotice = previousNotice || job;
    toast(`重试未启动：${error.message}`);
  } finally {
    state.jobRetryBusy = false;
    renderJobBar();
    renderModelInstallProgress();
  }
}

async function cancelActiveJob() {
  const current = state.activeJob;
  if (!current || current.status === "cancelling") return;
  current.status = "cancelling";
  current.stage = "正在取消";
  renderJobBar();
  renderModelInstallProgress(current);
  try {
    const cancelled = await api(`/api/jobs/${current.id}`, { method: "DELETE" });
    const index = state.jobs.findIndex((item) => item.id === cancelled.id);
    if (index >= 0) state.jobs[index] = cancelled;
    if (["cancelled", "failed", "interrupted", "completed"].includes(cancelled.status)) {
      state.activeJob = null;
    } else {
      state.activeJob = cancelled;
    }
    renderJobBar();
    renderModelInstallProgress(cancelled);
    pollJobs();
  } catch (error) {
    current.status = "running";
    state.activeJob = current;
    renderJobBar();
    renderModelInstallProgress(current);
    toast(error.message);
  }
}

async function pollJobs() {
  clearTimeout(pollJobs.timer);
  try {
    state.jobs = await api("/api/jobs");
    const active = state.jobs.find((job) => ["queued", "running", "cancelling"].includes(job.status));
    const activeChanged = (state.activeJob?.id || null) !== (active?.id || null);
    state.activeJob = active || null;
    renderJobBar();
    if (state.currentProject && !state.currentProject.latest_run_id) renderProjectStart();
    if (location.hash.replace(/^#/, "").startsWith("develop/")) renderDevelop();
    if (location.hash.replace(/^#/, "").startsWith("export/")) renderExport();
    if (activeChanged) renderReview();
    if (activeChanged) renderToolbox();
    const completed = state.jobs.filter((job) => ["completed", "failed", "cancelled", "interrupted"].includes(job.status));
    for (const job of completed) {
      if (state.handledJobs.has(job.id)) continue;
      const styleTerminal = isStyleJob(job);
      if (!styleTerminal) state.handledJobs.add(job.id);
      if (job.status === "completed") {
        const partialStyle = styleTerminal && job.result?.style_status === "partial";
        toast(partialStyle ? `${job.title}部分完成：${job.message}` : `${job.title}已完成`);
        if (partialStyle) {
          state.jobNotice = job;
          renderJobBar();
        }
        await refreshBootstrap();
        if (isStyleJob(job) && styleJobMatchesCurrentRun(job)) {
          await reloadDevelopAfterStyleJob(job);
          renderDevelop();
        }
        if (["group", "score"].includes(job.kind) && job.result?.run_id) await openRun(job.result.run_id);
        if (["xmp_commit", "rollback", "lightroom_apply"].includes(job.kind) && state.currentRun) {
          const route = location.hash.replace(/^#/, "");
          const returnToDevelop = route.startsWith("develop/");
          const returnToExport = route.startsWith("export/");
          const developStage = state.developStage;
          const selectedExportTargets = { ...state.exportTargets };
          await openRun(state.currentRun.run_id, returnToDevelop || returnToExport || route.startsWith("project/") ? route : "review");
          if (returnToDevelop) await loadDevelop(false, route, developStage, false, "replace");
          if (returnToExport) {
            state.exportTargets = selectedExportTargets;
            await loadExport("replace");
          }
        }
        if (job.context?.export_spec_id) {
          state.exportSpec = await api(`/api/exports/${encodeURIComponent(job.context.export_spec_id)}`);
          if (location.hash.replace(/^#/, "").startsWith("export/")) renderExport();
        }
        if (["raw_jpeg_execute", "raw_jpeg_rollback"].includes(job.kind)) {
          state.rawJpegPlan = null;
          renderToolbox();
        }
        if (["xmp_cleanup_execute", "xmp_cleanup_rollback"].includes(job.kind)) {
          state.xmpCleanupPlan = null;
          renderToolbox();
        }
        if (job.kind === "model_download") await refreshModelResources();
      } else if (job.status === "failed") {
        state.jobNotice = job;
        renderJobBar();
        toast(`${job.title}失败：${job.message}`);
        if (isStyleJob(job) && styleJobMatchesCurrentRun(job)) {
          await reloadDevelopAfterStyleJob(job);
          renderDevelop();
        }
        if (job.kind === "lightroom_apply") {
          try { state.lightroomStatus = await api("/api/lightroom/status"); renderLightroom(); } catch (_error) { /* keep failure message */ }
        }
        if (job.kind === "model_download") {
          try { await refreshModelResources(); } catch (_error) { /* keep failure message */ }
        }
        if (location.hash.replace(/^#/, "").startsWith("export/")) renderExport();
        if (job.context?.export_spec_id) {
          try {
            state.exportSpec = await api(`/api/exports/${encodeURIComponent(job.context.export_spec_id)}`);
            renderExport();
          } catch (_error) { /* keep the worker failure details */ }
        }
        if (job.kind === "raw_jpeg_execute") {
          state.rawJpegPlan = null;
          renderToolbox();
        }
        if (["xmp_cleanup_execute", "xmp_cleanup_rollback"].includes(job.kind)) {
          try { await refreshBootstrap(); } catch (_error) { /* keep the worker failure details */ }
          state.xmpCleanupPlan = null;
          renderToolbox();
        }
      } else if (["cancelled", "interrupted"].includes(job.status)) {
        if (job.status === "interrupted") {
          state.jobNotice = job;
          renderJobBar();
          toast(`${job.title}已中断；点击重试可从最近进度继续。`);
        }
        if (job.kind === "model_download") {
          try { await refreshModelResources(); } catch (_error) { /* keep partial download state */ }
          if (job.status !== "interrupted") {
            toast(`${job.title}已停止；可以稍后继续安装。`);
          }
        }
        if (["raw_jpeg_execute", "raw_jpeg_rollback"].includes(job.kind)) {
          await refreshBootstrap();
          state.rawJpegPlan = null;
          renderToolbox();
        }
        if (["xmp_cleanup_execute", "xmp_cleanup_rollback"].includes(job.kind)) {
          await refreshBootstrap();
          state.xmpCleanupPlan = null;
          renderToolbox();
        }
        if (isStyleJob(job) && styleJobMatchesCurrentRun(job)) {
          await reloadDevelopAfterStyleJob(job);
          renderDevelop();
        }
      }
      if (styleTerminal) state.handledJobs.add(job.id);
    }
    if (active) pollJobs.timer = setTimeout(pollJobs, 1200);
  } catch (_error) {
    pollJobs.timer = setTimeout(pollJobs, 2500);
  }
}

async function submitCull(event) {
  event.preventDefault();
  if (state.projectBusy) return;
  if (!await requireModelSetupUi()) return;
  state.projectBusy = true;
  const button = $("#cull-form button[type=submit]");
  button.disabled = true;
  button.textContent = "正在创建…";
  try {
    const project = await api("/api/projects", {
      method: "POST",
      body: { input_path: $("#cull-path").value.trim() },
    });
    await refreshBootstrap();
    await openProject(project.project_id);
  } catch (error) {
    toast(error.message);
  } finally {
    state.projectBusy = false;
    button.disabled = !modelSetupStatus().ready;
    button.textContent = "创建工程";
  }
}

async function submitProjectGroup(event) {
  event.preventDefault();
  const project = state.currentProject;
  if (!project || state.activeJob) return;
  if (!await requireModelSetupUi()) return;
  try {
    await startJob(`/api/projects/${encodeURIComponent(project.project_id)}/group`, {
      retain_ratio: 0.30,
      mode: "deep",
    });
    renderProjectStart();
  } catch (_error) {
    renderProjectStart();
  }
}

function extensionValues(selector) {
  return $(selector).value.split(/[\s,，;；]+/).map((value) => value.trim().replace(/^\./, "")).filter(Boolean);
}

async function previewRawJpeg(event) {
  event.preventDefault();
  state.toolboxBusy = true;
  renderToolbox();
  try {
    const layout = $("#raw-jpeg-layout").value;
    state.rawJpegPlan = await api("/api/tools/raw-jpeg/preview", {
      method: "POST",
      body: {
        layout,
        direction: $("#raw-jpeg-direction").value,
        mixed_path: layout === "mixed" ? $("#raw-jpeg-mixed-path").value.trim() : null,
        raw_path: layout === "separate" ? $("#raw-jpeg-raw-path").value.trim() : null,
        jpeg_path: layout === "separate" ? $("#raw-jpeg-jpeg-path").value.trim() : null,
        raw_extensions: extensionValues("#raw-jpeg-raw-extensions"),
        jpeg_extensions: extensionValues("#raw-jpeg-jpeg-extensions"),
        recursive: $("#raw-jpeg-recursive").checked,
      },
    });
  } catch (error) {
    toast(error.message);
  } finally {
    state.toolboxBusy = false;
    renderToolbox();
  }
}

function openRawJpegExecute() {
  const plan = state.rawJpegPlan;
  if (!plan?.candidate_count) return;
  const sidecars = plan.sidecar_count ? `，连同 ${plan.sidecar_count} 个对应 XMP` : "";
  $("#raw-jpeg-confirm-copy").textContent = `${rawJpegDirectionLabel(plan.direction)}：将 ${plan.candidate_count} 个孤片${sidecars}（${formatBytes(plan.candidate_bytes)}）移入同盘回收区。执行前会再次核对目录。`;
  $("#raw-jpeg-confirm-dialog").showModal();
}

async function executeRawJpeg() {
  const plan = state.rawJpegPlan;
  if (!plan) return;
  $("#raw-jpeg-confirm-dialog").close();
  await startJob("/api/tools/raw-jpeg/execute", { plan_id: plan.plan_id });
  renderToolbox();
}

async function rollbackRawJpeg(transactionId) {
  await startJob("/api/tools/raw-jpeg/rollback", { transaction_id: transactionId });
  renderToolbox();
}

async function previewXmpCleanup(event) {
  event.preventDefault();
  state.xmpCleanupPlan = null;
  state.xmpCleanupBusy = true;
  renderToolbox();
  try {
    state.xmpCleanupPlan = await api("/api/tools/xmp-cleanup/preview", {
      method: "POST",
      body: {
        root_path: $("#xmp-cleanup-path").value.trim(),
        recursive: $("#xmp-cleanup-recursive").checked,
      },
    });
  } catch (error) {
    toast(error.message);
  } finally {
    state.xmpCleanupBusy = false;
    renderToolbox();
  }
}

function openXmpCleanupExecute() {
  const plan = state.xmpCleanupPlan;
  if (!plan?.xmp_count || !plan.complete) return;
  $("#xmp-cleanup-confirm-copy").textContent = `将永久删除 ${plan.xmp_count} 个 XMP（${formatBytes(plan.xmp_bytes)}），不可恢复。执行前会再次核对目录与文件指纹。Lightroom 开启时仍可能重新生成旁车文件。`;
  $("#xmp-cleanup-confirm-dialog").showModal();
}

async function executeXmpCleanup() {
  const plan = state.xmpCleanupPlan;
  if (!plan) return;
  $("#xmp-cleanup-confirm-dialog").close();
  await startJob("/api/tools/xmp-cleanup/execute", { plan_id: plan.plan_id });
  renderToolbox();
}

async function rollbackXmpCleanup(transactionId) {
  await startJob("/api/tools/xmp-cleanup/rollback", { transaction_id: transactionId });
  renderToolbox();
}

async function rollbackXmp() {
  try {
    await startJob("/api/transactions/rollback", { transaction_id: state.rollbackId, confirmation: $("#rollback-confirm").value.trim() });
    $("#rollback-dialog").close();
    $("#rollback-confirm").value = "";
  } catch (_error) {}
}

function openDeleteProjectDialog(projectId) {
  const project = state.projects.find((item) => item.project_id === projectId);
  if (!project) return;
  state.deleteProjectTarget = project;
  const xmpNotice = project.xmp_count
    ? ` 当前有 ${project.xmp_count} 个 XMP 会继续保留在照片目录中。`
    : "";
  $("#delete-project-copy").textContent = `工程记录将移入所选数据目录的回收区。RAW 和现有 XMP 不会删除。${xmpNotice}`;
  $("#delete-project-confirm").value = "";
  $("#delete-project-dialog").showModal();
}

async function deleteProject() {
  const project = state.deleteProjectTarget;
  if (!project) return;
  const button = $("#delete-project-submit");
  button.disabled = true;
  button.textContent = "正在移动…";
  try {
    await api(`/api/projects/${encodeURIComponent(project.project_id)}`, {
      method: "DELETE",
      body: { confirmation: $("#delete-project-confirm").value.trim() },
    });
    if (state.currentRun?.run_id === project.latest_run_id) state.currentRun = null;
    if (state.currentProject?.project_id === project.project_id) state.currentProject = null;
    state.deleteProjectTarget = null;
    $("#delete-project-dialog").close();
    await refreshBootstrap();
    setView("cull");
    toast(`${project.name}已移入回收目录`);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "移入回收目录";
  }
}

async function routeFromHash() {
  const requested = location.hash.replace(/^#/, "");
  if (requested === "toolbox" || requested.startsWith("toolbox/")) {
    const section = requested.startsWith("toolbox/") ? requested.slice("toolbox/".length).split("/")[0] : "home";
    openToolboxSection(section, "replace");
    return;
  }
  if (requested.startsWith("develop/")) {
    const parts = requested.slice("develop/".length).split("/");
    const runId = parts[0];
    const stage = ["style", "color"].includes(parts[1]) ? "style" : parts[1] === "base" ? "base" : "crop";
    await openRun(runId, requested, "replace");
    try {
      await loadDevelop(false, requested, stage, false, "replace");
    } catch (error) {
      setView("review", currentProjectRoute(), "replace");
      toast(error.message);
    }
    return;
  }
  if (requested.startsWith("export/")) {
    const runId = requested.slice("export/".length).split("/")[0];
    await openRun(runId, requested, "replace");
    try {
      await loadExport("replace");
    } catch (error) {
      setView("review", currentProjectRoute(), "replace");
      toast(error.message);
    }
    return;
  }
  if (requested.startsWith("review/")) {
    const runId = requested.slice("review/".length).split("/")[0];
    await openRun(runId, requested, "replace");
    return;
  }
  if (requested.startsWith("project/")) {
    const projectId = decodeURIComponent(requested.slice("project/".length));
    await openProject(projectId, false);
    return;
  }
  if (requested === "train") return setView("settings");
  if (requested === "settings/resources") return openModelResources("replace");
  setView(["cull", "toolbox", "settings"].includes(requested) ? requested : "cull");
}

function bindEvents() {
  bindDesktopFolderPickers();
  $$(".tab").forEach((node) => node.addEventListener("click", () => {
    if (node.dataset.view === "toolbox") openToolboxSection("home", "replace");
    else setView(node.dataset.view);
  }));
  $("#cull-form").addEventListener("submit", submitCull);
  $("#project-group-form").addEventListener("submit", submitProjectGroup);
  $("#project-start-back").addEventListener("click", () => setView("cull", "cull", "push"));
  $("#raw-jpeg-form").addEventListener("submit", previewRawJpeg);
  $("#raw-jpeg-layout").addEventListener("change", () => { state.rawJpegPlan = null; renderToolbox(); });
  $("#raw-jpeg-form").addEventListener("input", (event) => {
    if (event.target.id === "raw-jpeg-layout") return;
    if (state.rawJpegPlan) { state.rawJpegPlan = null; renderToolbox(); }
  });
  $("#raw-jpeg-execute").addEventListener("click", executeRawJpeg);
  $("#xmp-cleanup-form").addEventListener("submit", previewXmpCleanup);
  $("#xmp-cleanup-form").addEventListener("input", () => {
    if (state.xmpCleanupPlan) { state.xmpCleanupPlan = null; renderToolbox(); }
  });
  $("#xmp-cleanup-execute").addEventListener("click", executeXmpCleanup);
  $("#job-cancel").addEventListener("click", async () => {
    if (state.activeJob) return cancelActiveJob();
    state.developProgress = null;
    state.developFlowProgress = null;
    state.jobNotice = null;
    renderJobBar();
  });
  $("#job-retry").addEventListener("click", () => retryFailedJob());
  $("#review-back").addEventListener("click", () => setView("cull", "cull", "push"));
  $("#develop-open").addEventListener("click", () => goWorkflowStage("crop").catch((error) => toast(error.message)));
  $("#develop-back").addEventListener("click", () => {
    const previous = state.developStage === "style" ? "base" : state.developStage === "base" ? "crop" : "review";
    goWorkflowStage(previous).catch((error) => toast(error.message));
  });
  $("#develop-apply-style").addEventListener("click", confirmRecommendedStyles);
  $("#style-recommend").addEventListener("click", () => recommendStyles(null, "global"));
  $("#style-recommend-all").addEventListener("click", recommendStylesForAllGroups);
  $("#style-library-manage").addEventListener("click", () => {
    state.styleLibrarySection = "home";
    $("#style-library-manager-search").value = "";
    renderStyleLibraryManager();
    $("#style-library-manager-dialog").showModal();
  });
  $("#model-resource-open").addEventListener("click", () => openModelResources("push"));
  $("#model-setup-open").addEventListener("click", () => openModelResources("push"));
  $("#project-model-setup-open").addEventListener("click", () => openModelResources("push"));
  $("#model-resource-back").addEventListener("click", () => setView("settings", "settings", "push"));
  $("#settings-content-root-change").addEventListener("click", async (event) => {
    const invoke = desktopInvoke();
    if (!invoke || state.activeJob) {
      if (state.activeJob) toast("请先停止当前任务，再修改数据目录。");
      return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在选择…";
    try {
      await invoke("reconnect_content_root");
    } catch (error) {
      toast(error?.message || String(error));
      button.disabled = false;
      button.textContent = "修改位置";
    }
  });
  $("#settings-export").addEventListener("click", exportSettingsFile);
  $("#settings-import-open").addEventListener("click", () => $("#settings-import-file").click());
  $("#settings-import-file").addEventListener("change", (event) => importSettingsFile(event.target.files?.[0]));
  $("#model-profile-list").addEventListener("click", (event) => {
    const offlineImport = event.target.closest("[data-model-offline-import]");
    if (offlineImport) return importOfflineModelBundle(offlineImport.dataset.modelOfflineImport);
    const configure = event.target.closest("[data-model-configure]");
    if (configure) return configureModelProfile(configure.dataset.modelConfigure);
    const profile = event.target.closest("[data-model-profile]");
    if (profile) {
      state.modelProfile = profile.dataset.modelProfile;
      renderModelResources();
    }
  });
  $("#model-resource-list").addEventListener("click", (event) => {
    if (event.target.closest("[data-model-install-cancel]")) return cancelActiveJob();
    const retry = event.target.closest("[data-model-install-retry]");
    if (retry) return retryFailedJob(retry.dataset.modelInstallRetry);
    if (event.target.closest("[data-model-install-dismiss]")) {
      state.jobNotice = null;
      renderModelInstallProgress(null);
      return;
    }
    const remove = event.target.closest("[data-model-delete]");
    if (remove) deleteModelResource(remove.dataset.modelDelete);
  });
  $("#model-profile-delete").addEventListener("click", deleteSelectedModelProfile);
  $$('[data-style-source-open]').forEach((node) => {
    node.addEventListener("click", () => {
      state.styleLibrarySection = node.dataset.styleSourceOpen;
      $("#style-library-manager-search").value = "";
      $("#style-library-import-status").classList.add("hidden");
      renderStyleLibraryManager();
    });
  });
  $("#style-library-source-back").addEventListener("click", () => { state.styleLibrarySection = "home"; renderStyleLibraryManager(); });
  $("#style-library-source-toggle").addEventListener("click", (event) => setStyleSourceEnabled(event.currentTarget.dataset.source, event.currentTarget.dataset.enabled === "true"));
  $("#style-library-source-rescan").addEventListener("click", () => syncStyleLibrary({}, "Lightroom 预设已重新扫描，AI 索引已更新"));
  $("#style-library-files").addEventListener("change", renderStyleLibrary);
  $("#style-library-import").addEventListener("click", importStyleLibraryFiles);
  $("#style-library-manager-search").addEventListener("input", renderStyleLibraryManager);
  $("#style-library-manager-list").addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-style-library-toggle]");
    if (toggle) {
      setStyleLibraryItemHidden(toggle.dataset.styleLibraryToggle, toggle.dataset.hidden === "true");
      return;
    }
    const remove = event.target.closest("[data-style-library-delete]");
    if (remove) {
      deleteStyleLibraryItem(remove.dataset.styleLibraryDelete);
    }
  });
  $("#lightroom-config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    configureLightroom(false);
  });
  $("#lightroom-auto-configure").addEventListener("click", () => configureLightroom(true));
  $("#lightroom-refresh").addEventListener("click", refreshLightroomConnection);
  $("#lightroom-path").addEventListener("input", renderLightroom);
  $("#style-search-input").addEventListener("input", renderStyleSearch);
  $("#develop-next").addEventListener("click", () => {
    const action = state.developStage === "crop"
      ? advanceFromCrop()
      : state.developStage === "base"
        ? goWorkflowStage("style")
        : loadExport();
    action.catch((error) => toast(error.message));
  });
  $("#develop-skip").addEventListener("click", () => {
    const action = state.developStage === "crop"
      ? skipCropDevelop().then(() => goWorkflowStage("base"))
      : state.developStage === "style"
        ? setColorMode(state.developPlan?.basic_color?.status === "enabled" ? "auto" : "skip")
          .then((saved) => saved && loadExport())
        : Promise.resolve();
    action.catch((error) => toast(error.message));
  });
  $("#export-back").addEventListener("click", () => goWorkflowStage("style").catch((error) => toast(error.message)));
  $("#export-action").addEventListener("click", executeExport);
  $("#export-xmp").addEventListener("change", (event) => { state.exportTargets.xmp = event.target.checked; state.exportSpec = null; renderExport(); });
  $("#export-jpeg").addEventListener("change", (event) => { state.exportTargets.jpeg = event.target.checked; state.exportSpec = null; renderExport(); });
  $("#grouping-toggle").addEventListener("click", toggleGrouping);
  $("#batch-select-toggle").addEventListener("click", toggleBatchGrouping);
  $("#selection-all").addEventListener("click", () => {
    state.currentRun?.results.filter((item) => !item.excluded).forEach((item) => state.selectedIndices.add(item.index));
    renderReview();
  });
  $("#selection-clear").addEventListener("click", () => { clearGroupingSelection(); renderReview(); });
  $("#selection-move").addEventListener("click", () => openGroupDialog([...state.selectedIndices]));
  $("#selection-up").addEventListener("click", () => applyGroupChange([...state.selectedIndices], { direction: "previous" }));
  $("#selection-down").addEventListener("click", () => applyGroupChange([...state.selectedIndices], { direction: "next" }));
  $("#selection-new-group").addEventListener("click", () => applyGroupChange([...state.selectedIndices], { selected: "new" }));
  $("#selection-remove").addEventListener("click", () => updateExcluded([...state.selectedIndices], true));
  $("#score-run").addEventListener("click", () => scoreCurrentGroups().catch(() => {}));
  $("#group-submit").addEventListener("click", moveGroup);
  $("#rollback-submit").addEventListener("click", rollbackXmp);
  $("#delete-project-submit").addEventListener("click", deleteProject);
  document.addEventListener("click", (event) => {
    const toolboxModule = event.target.closest("[data-toolbox-open]");
    if (toolboxModule) {
      openToolboxSection(toolboxModule.dataset.toolboxOpen, "push");
      return;
    }
    const toolboxBack = event.target.closest("[data-toolbox-back]");
    if (toolboxBack) {
      openToolboxSection("home", "push");
      return;
    }
    const workflowStep = event.target.closest("[data-workflow-stage]");
    if (workflowStep && !workflowStep.disabled) {
      const activeStage = $("#view-review").classList.contains("active") ? "review"
        : $("#view-export").classList.contains("active") ? "export" : state.developStage;
      if (workflowStep.dataset.workflowStage === activeStage) return;
      goWorkflowStage(workflowStep.dataset.workflowStage).catch((error) => toast(error.message));
      return;
    }
    const colorOption = event.target.closest("[data-color-mode]");
    if (colorOption) {
      setColorMode(colorOption.dataset.colorMode);
      return;
    }
    const styleScopeOption = event.target.closest(".style-scope-option");
    if (styleScopeOption) {
      setStyleScope(styleScopeOption.dataset.styleScope);
      return;
    }
    const styleTier = event.target.closest("[data-style-tier]");
    if (styleTier) {
      const global = styleTier.dataset.styleScope === "global";
      requestStylePreview(global ? null : Number(styleTier.dataset.styleAmount), {
        scope: global ? "global" : "group",
        lutId: styleTier.dataset.lutId || null,
        lutHash: styleTier.dataset.lutHash || null,
        presetId: styleTier.dataset.presetId || null,
        presetHash: styleTier.dataset.presetHash || null,
        amount: Number(styleTier.dataset.styleTier),
      });
      return;
    }
    const styleChoice = event.target.closest("[data-style-choice]");
    if (styleChoice) {
      const natural = styleChoice.dataset.choiceId === "natural";
      if (styleChoice.disabled || (!natural && styleChoice.dataset.previewReady !== "true")) return;
      if (natural && currentColorMode(state.developPlan) !== "style") return;
      const changes = {
        amount: natural ? 0 : Number(styleChoice.dataset.styleChoiceAmount || 100),
        status: natural ? "skipped" : "confirmed",
      };
      if (natural) {
        changes.lut_id = null;
        changes.lut_hash = null;
        changes.preset_id = null;
        changes.preset_hash = null;
      } else if (styleChoice.dataset.lutId) {
        changes.lut_id = styleChoice.dataset.lutId;
        changes.lut_hash = styleChoice.dataset.lutHash || null;
        changes.preset_id = null;
        changes.preset_hash = null;
        changes.strength = changes.amount;
      } else {
        changes.lut_id = null;
        changes.lut_hash = null;
        changes.preset_id = styleChoice.dataset.presetId;
        changes.preset_hash = styleChoice.dataset.presetHash || null;
      }
      if (styleChoice.dataset.styleScope === "global" && !natural) {
        requestStylePreview(null, {
          scope: "global",
          lutId: changes.lut_id || null,
          lutHash: changes.lut_hash || null,
          presetId: changes.preset_id || null,
          presetHash: changes.preset_hash || null,
          amount: changes.amount,
        });
      } else if (styleChoice.dataset.styleScope === "global") updateStyleGlobal(changes);
      else updateStyleGroup(Number(styleChoice.dataset.groupId), changes);
      return;
    }
    const styleRecommendGroup = event.target.closest("[data-style-recommend-group]");
    if (styleRecommendGroup) { recommendStyles(Number(styleRecommendGroup.dataset.styleRecommendGroup), "group"); return; }
    const styleSearchGroup = event.target.closest("[data-style-search-group]");
    if (styleSearchGroup) { openStyleSearch(Number(styleSearchGroup.dataset.styleSearchGroup)); return; }
    const styleSearchSelect = event.target.closest("[data-style-search-select]");
    if (styleSearchSelect) {
      if (state.styleSearchGroupId == null) return toast("请先从具体照片组打开预设搜索。 ");
      requestStylePreview(state.styleSearchGroupId, {
        lutId: styleSearchSelect.dataset.lutId || null,
        lutHash: styleSearchSelect.dataset.lutHash || null,
        presetId: styleSearchSelect.dataset.presetId || null,
        presetHash: styleSearchSelect.dataset.presetHash || null,
        amount: 100,
        scope: "group",
      }).then((started) => { if (started) $("#style-search-dialog").close(); });
      return;
    }
    const selectInput = event.target.closest("[data-select-index]");
    if (selectInput) {
      event.stopPropagation();
      selectGroupingIndex(Number(selectInput.dataset.selectIndex), selectInput.checked, event.shiftKey);
      return;
    }
    const projectButton = event.target.closest(".open-project");
    if (projectButton) openProject(projectButton.dataset.projectId).catch((error) => toast(error.message));
    const deleteProjectButton = event.target.closest(".delete-project-open");
    if (deleteProjectButton) openDeleteProjectDialog(deleteProjectButton.dataset.projectId);
    const rateButton = event.target.closest("[data-rate-index]");
    if (rateButton) setRating(Number(rateButton.dataset.rateIndex), rateButton.dataset.rating);
    const photoButton = event.target.closest("[data-open-photo]");
    if (photoButton) openPhoto(Number(photoButton.dataset.openPhoto));
    const moveGroupButton = event.target.closest("[data-move-group]");
    if (moveGroupButton) openGroupDialog(Number(moveGroupButton.dataset.moveGroup));
    const shiftGroupButton = event.target.closest("[data-shift-index]");
    if (shiftGroupButton) applyGroupChange([Number(shiftGroupButton.dataset.shiftIndex)], { direction: shiftGroupButton.dataset.shiftDirection });
    const newGroupButton = event.target.closest("[data-new-group-index]");
    if (newGroupButton) applyGroupChange([Number(newGroupButton.dataset.newGroupIndex)], { selected: "new" });
    const rawJpegExecuteButton = event.target.closest(".raw-jpeg-execute-open");
    if (rawJpegExecuteButton) openRawJpegExecute();
    const rawJpegRollbackButton = event.target.closest(".raw-jpeg-rollback");
    if (rawJpegRollbackButton) rollbackRawJpeg(rawJpegRollbackButton.dataset.transactionId).catch(() => {});
    const xmpCleanupExecuteButton = event.target.closest(".xmp-cleanup-execute-open");
    if (xmpCleanupExecuteButton) openXmpCleanupExecute();
    const xmpCleanupRollbackButton = event.target.closest(".xmp-cleanup-rollback");
    if (xmpCleanupRollbackButton) rollbackXmpCleanup(xmpCleanupRollbackButton.dataset.transactionId).catch(() => {});
    const selectGroupButton = event.target.closest("[data-select-group]");
    if (selectGroupButton) {
      const groupId = Number(selectGroupButton.dataset.selectGroup);
      state.currentRun?.results.filter((item) => !item.excluded && Number(item.group_id) === groupId).forEach((item) => state.selectedIndices.add(item.index));
      renderReview();
    }
    const excludeButton = event.target.closest("[data-exclude-index]");
    if (excludeButton) updateExcluded([Number(excludeButton.dataset.excludeIndex)], true);
    const restoreButton = event.target.closest("[data-restore-index]");
    if (restoreButton) updateExcluded([Number(restoreButton.dataset.restoreIndex)], false);
    const restoreAllButton = event.target.closest("[data-restore-all]");
    if (restoreAllButton) updateExcluded(state.currentRun?.results.filter((item) => item.excluded).map((item) => item.index) || [], false);
    const rollbackButton = event.target.closest(".rollback-open");
    if (rollbackButton) { state.rollbackId = rollbackButton.dataset.transactionId; $("#rollback-dialog").showModal(); }
    const closeButton = event.target.closest("[data-close-dialog]");
    if (closeButton) { event.preventDefault(); $(`#${closeButton.dataset.closeDialog}`).close(); }
    const cropChoice = event.target.closest("[data-develop-crop-choice]");
    if (cropChoice && cropChoice.dataset.cropId) {
      updateDevelop(Number(cropChoice.dataset.developCropChoice), { crop_id: cropChoice.dataset.cropId, confirmed: true });
    }
  });
  $$(".filter").forEach((node) => node.addEventListener("click", () => {
    state.filter = node.dataset.filter;
    $$(".filter").forEach((item) => item.classList.toggle("active", item === node));
    renderReview();
  }));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.batchGrouping && !$("#group-dialog").open && !$("#photo-dialog").open) {
      clearGroupingSelection();
      renderReview();
      return;
    }
    if (!$("#photo-dialog").open || state.dialogIndex == null || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    const items = state.currentRun.results;
    const position = items.findIndex((item) => item.index === state.dialogIndex);
    if (event.key === "ArrowRight" && position < items.length - 1) updatePhotoDialog(items[position + 1].index);
    if (event.key === "ArrowLeft" && position > 0) updatePhotoDialog(items[position - 1].index);
    if (["0", "3", "4", "5"].includes(event.key)) setRating(state.dialogIndex, event.key);
  });
  window.addEventListener("hashchange", () => routeFromHash().catch((error) => toast(error.message)));
}

document.addEventListener("DOMContentLoaded", async () => {
  bindEvents();
  try {
    await refreshBootstrap();
    if (!await resumePendingModelInstall()) await routeFromHash();
    state.jobs.filter((job) => !["queued", "running", "cancelling"].includes(job.status)).forEach((job) => state.handledJobs.add(job.id));
    pollJobs();
  } catch (error) {
    $("#alert-bar").textContent = error.message;
    $("#alert-bar").classList.remove("hidden");
  }
});
