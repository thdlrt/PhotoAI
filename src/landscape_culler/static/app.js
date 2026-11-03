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
  deletePhotoTarget: null,
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
  dialogSequence: null,
  thumbnailSize: 210,
  thumbnailDirty: false,
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
let visionThinkingRequest = 0;
let ratingSave = Promise.resolve();
let reviewViewer = null;
let reviewGrid = null;
let reviewOpenRequest = 0;
let developLoadRequest = 0;
let exportStarting = false;
let jobsPolling = false;
let jobsPollAgain = false;

function renderVisionThinkingOptions(capabilities, value = $("#vision-thinking").value) {
  const select = $("#vision-thinking");
  const options = capabilities.options || [];
  select.innerHTML = options.map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`).join("");
  select.value = options.some((option) => option.value === value) ? value : capabilities.default;
  select.disabled = options.length < 2;
  $("#vision-thinking-hint").textContent = capabilities.hint || "";
}

async function refreshVisionThinkingOptions() {
  const requestId = ++visionThinkingRequest;
  try {
    const query = new URLSearchParams({base_url: $("#vision-url").value, model: $("#vision-model").value});
    const capabilities = await api(`/api/vision-provider/thinking-options?${query}`);
    if (requestId === visionThinkingRequest) renderVisionThinkingOptions(capabilities);
  } catch (error) {
    if (requestId === visionThinkingRequest) $("#vision-status").textContent = error.message;
  }
}

async function saveVisionProviderSettings(event) {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  visionThinkingRequest += 1;
  try {
    const config = await api("/api/vision-provider", {method: "PUT", body: {
      mode: $("#vision-mode").value, base_url: $("#vision-url").value,
      model: $("#vision-model").value, api_key: $("#vision-key").value,
      thinking_effort: $("#vision-thinking").value,
      timeout: Number($("#vision-timeout").value), upload_consent: $("#vision-consent").checked,
    }});
    $("#vision-key").value = "";
    renderVisionThinkingOptions(config.thinking_capabilities, config.thinking_effort);
    $("#vision-status").textContent = "已保存，下次任务生效。";
    await refreshModelResources();
  } catch (error) { $("#vision-status").textContent = error.message; }
  finally { button.disabled = false; }
}

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

async function requireStyleRuntimeUi() {
  const run = state.currentRun;
  const navigation = reviewOpenRequest;
  try {
    const status = await api("/api/style-runtime");
    if (state.currentRun !== run || reviewOpenRequest !== navigation) return false;
    if (status.ready) return true;
    toast(status.message || "外观处理运行组件尚未就绪。");
    await openModelResources("push");
  } catch (error) {
    if (state.currentRun === run && reviewOpenRequest === navigation) toast(error.message);
  }
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

async function copyDiagnostics() {
  const button = $("#app-diagnostics-copy");
  if (button.disabled) return;
  button.disabled = true;
  const fallback = $("#app-diagnostics-fallback");
  fallback.classList.add("hidden");
  try {
    const report = await api("/api/diagnostics");
    try {
      await navigator.clipboard.writeText(report.copy_text);
      toast("诊断信息已复制");
    } catch {
      $("#app-diagnostics-text").value = report.copy_text;
      fallback.classList.remove("hidden");
      $("#app-diagnostics-text").focus();
      $("#app-diagnostics-text").select();
    }
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
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

async function setView(name, route = name, historyMode = "replace") {
  if (PhotoReviewWorkspace.hasPending() && !await PhotoReviewWorkspace.savePending()) return;
  if (!["project", "review", "develop", "export"].includes(name)) reviewOpenRequest += 1;
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
  const photoCount = hasRun ? (excluded ? `${active} 张 · 移出 ${excluded} 张` : `${active} 张`) : "尚未分析";
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

async function openToolboxSection(section = "home", historyMode = "push") {
  const requestedHash = location.hash;
  if (PhotoReviewWorkspace.hasPending() && !await PhotoReviewWorkspace.savePending()) return;
  const validSections = new Set(["home", "review", "timelapse", "agent", "raw-jpeg", "xmp-cleanup", "xmp-records"]);
  state.toolboxSection = validSections.has(section) ? section : "home";
  $$('[data-toolbox-panel]').forEach((node) => node.classList.toggle("hidden", node.dataset.toolboxPanel !== state.toolboxSection));
  let route = state.toolboxSection === "home" ? "toolbox" : `toolbox/${state.toolboxSection}`;
  if (state.toolboxSection !== "home" && historyMode === "replace" && requestedHash.startsWith(`#${route}/`)) route = requestedHash.slice(1);
  setView("toolbox", route, historyMode);
  renderToolbox();
  if (state.toolboxSection === "review") PhotoReviewWorkspace.open().catch(error => toast(error.message));
  if (state.toolboxSection === "timelapse") PhotoTimelapse.open().catch(error => toast(error.message));
  if (state.toolboxSection === "agent") PhotoAgentPanel.open().catch(error => toast(error.message));
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
  const request = ++reviewOpenRequest;
  await ratingSave;
  if (request !== reviewOpenRequest) return;
  const project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
  if (request !== reviewOpenRequest) return;
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
  } else if (data.engine?.worker_update_required) {
    taskNote.textContent = data.engine.message;
    taskNote.classList.remove("hidden", "error", "success");
    taskNote.classList.add("warning");
  } else {
    taskNote.textContent = "";
    taskNote.classList.add("hidden");
    taskNote.classList.remove("warning", "error", "success");
  }

  const busy = Boolean(state.activeJob) || Boolean(state.modelResourceBusy);
  $("#model-profile-list").innerHTML = (data.profiles || []).map((profile) => {
    const isSelected = profile.id === selected.id;
    const action = profile.configured
      ? (data.engine?.worker_update_required && profile.verified_count === profile.model_count ? "更新处理代码" : profile.ready ? "检查配置" : "补齐并修复")
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
  const componentRows = (data.cloud_vision ? [] : data.components || []).map((item) => `<article class="model-resource-row" data-model-resource-row="runtime">
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

function jobBlocksProject(project = state.currentRun, job = state.activeJob) {
  if (!job || !["queued", "running", "cancelling"].includes(job.status) || job.kind === "model_download") return false;
  if (!project) return true;
  const scope = job.context || {};
  if (project.run_id && [scope.run_id, scope.source_run_id].includes(project.run_id)) return true;
  if (project.project_id && project.project_id === scope.project_id) return true;
  const normalize = path => String(path || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const source = normalize(scope.input_root), target = normalize(project.input_root);
  if (source && target) return source === target || source.startsWith(`${target}/`) || target.startsWith(`${source}/`);
  // Unknown legacy file tasks remain protected until their scope is resolved.
  return true;
}

function currentProgressJob() {
  const localProgress = state.developProgress || state.developFlowProgress;
  const foreground = localProgress ? {
    id: localProgress.job_id,
    kind: state.developProgress ? "develop" : null,
    title: localProgress.title || (state.developProgress ? "分析智能构图" : "保存处理设置"),
    status: localProgress.status || "running",
    stage: localProgress.stage_label,
    message: localProgress.message,
    progress: localProgress,
    foreground: true,
    retryable: ["failed", "cancelled", "interrupted"].includes(localProgress.status) && Boolean(state.currentRun),
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
  const failed = ["failed", "interrupted"].includes(job?.status) || (job?.kind === "develop" && job.status === "cancelled") || partialFailure;
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
  percent.textContent = job.status === "cancelled" ? "已取消" : partialFailure ? "部分失败" : failed ? "失败" : (overall === null ? "进行中" : `${Math.round(overall)}%`);
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
    : reclassificationRunId(job) ? "重新分类" : partialFailure ? "重试失败组" : job.retry_mode === "develop" ? "继续构图" : job.retry_mode === "export_failed" ? "重试失败项" : "重试";
  retry.disabled = state.jobRetryBusy;
  retry.classList.toggle("hidden", !failed || !job.retryable);
  $("#job-cancel").textContent = failed ? "关闭" : job.status === "cancelling" ? "正在取消…" : job.foreground && !job.id ? "正在启动…" : "取消";
  $("#job-cancel").disabled = job.status === "cancelling" || Boolean(job.foreground && !job.id && !failed);
  bar.classList.toggle("failed", failed && job.status !== "cancelled");
  bar.classList.remove("hidden");
}

let thumbnailSave = Promise.resolve();

function setThumbnailSize(value) {
  const number = Number(value);
  state.thumbnailSize = Number.isFinite(number) ? Math.min(420, Math.max(160, Math.round(number / 10) * 10)) : 210;
  const workspace = $("#review-workspace");
  workspace.style.setProperty("--review-thumbnail-size", `${state.thumbnailSize}px`);
  workspace.style.setProperty("--review-preview-height", `${Math.round(state.thumbnailSize * 6 / 7)}px`);
  $("#thumbnail-size").value = state.thumbnailSize;
}

async function saveThumbnailSize() {
  const size = state.thumbnailSize;
  const save = thumbnailSave.catch(() => {}).then(() => api("/api/preferences/ui", {
    method: "PATCH", body: { thumbnail_size: size },
  }));
  thumbnailSave = save;
  try {
    const ui = await save;
    if (state.bootstrap && state.thumbnailSize === size) {
      state.bootstrap.preferences = { ...state.bootstrap.preferences, ui };
    }
  } catch (error) {
    toast(`缩略图大小未能保存：${error.message}`);
  } finally {
    if (state.thumbnailSize === size) state.thumbnailDirty = false;
  }
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
  if (!state.thumbnailDirty) setThumbnailSize(preferences.ui?.thumbnail_size ?? 210);
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
  const labels = {0: "不入选", 3: "候选", 4: "推荐", 5: "精选"};
  const buttons = [0, 3, 4, 5].map((rating) => `<button class="rating-button ${item.effective_rating === rating ? "active" : ""}" data-rate-index="${item.index}" data-rating="${rating}" aria-pressed="${item.effective_rating === rating}" title="${labels[rating]}（${rating} 星，快捷键 ${rating}）">${rating}</button>`).join("");
  const reset = item.manual_override ? `<button class="rating-button rating-reset" data-rate-index="${item.index}" data-rating="reset">恢复 AI</button>` : "";
  return `<div class="rating-buttons ${compact ? "compact" : ""}">${buttons}${reset}</div>`;
}

function matchesFilter(item) {
  if (state.filter === "all") return true;
  if (state.filter === "warning") return Boolean(warningText(item));
  if (state.filter === "candidates") return item.effective_rating >= 3;
  const rating = Number(state.filter);
  if (rating === 4) return item.effective_rating >= 4;
  return item.effective_rating === rating;
}

function clearGroupingSelection() {
  state.selectedIndices.clear();
  state.selectionAnchor = null;
}

function reviewGroupOrder(run = state.currentRun) {
  const active = new Set((run?.results || []).filter((item) => !item.excluded).map((item) => Number(item.group_id)));
  return [...new Set([...(run?.group_order || []).filter((id) => active.has(id)), ...[...active].sort((a, b) => a - b)])];
}

let groupingDrag = null;
let groupingDragFrame = 0;
let suppressDragClickUntil = 0;

function endGroupingDrag() {
  if (groupingDragFrame) cancelAnimationFrame(groupingDragFrame);
  groupingDragFrame = 0;
  groupingDrag = null;
  document.body.classList.remove("grouping-dragging");
  $$(".drag-source, .drop-photos, .drop-before, .drop-after").forEach((node) =>
    node.classList.remove("drag-source", "drop-photos", "drop-before", "drop-after"));
}

function canDragGroups() {
  const run = state.currentRun;
  return run && !jobBlocksProject() && !state.batchBusy &&
    (state.groupingMode || run.workflow_state !== "scored" || run.needs_rescore);
}

function markGroupingDrop(x, y) {
  $$(".drop-photos, .drop-before, .drop-after").forEach((node) => node.classList.remove("drop-photos", "drop-before", "drop-after"));
  const target = document.elementFromPoint(x, y)?.closest("#review-groups .photo-group[data-group-id]");
  if (!groupingDrag || !target) return null;
  const id = Number(target.dataset.groupId);
  if (groupingDrag.kind === "group" && id === groupingDrag.groupId) return null;
  const after = y > target.getBoundingClientRect().top + target.getBoundingClientRect().height / 2;
  target.classList.add(groupingDrag.kind === "photos" ? "drop-photos" : after ? "drop-after" : "drop-before");
  return { id, after };
}

function scrollGroupingDrag(time) {
  const drag = groupingDrag;
  if (!drag) return;
  if (!canDragGroups() || state.currentRun.run_id !== drag.runId) { endGroupingDrag(); return; }
  const dt = Math.min(40, time - (drag.time || time));
  drag.time = time;
  const top = Math.max(24, document.querySelector(".app-header")?.getBoundingClientRect().bottom || 0);
  const toolbar = document.querySelector("#selection-toolbar:not(.hidden)");
  const bottom = Math.min(window.innerHeight, toolbar?.getBoundingClientRect().top || window.innerHeight);
  const edge = 90;
  const speed = drag.y < top + edge ? -Math.min(1, Math.max(0, (top + edge - drag.y) / edge))
    : drag.y > bottom - edge ? Math.min(1, Math.max(0, (drag.y - bottom + edge) / edge)) : 0;
  if (speed && drag.x >= 0 && drag.x <= window.innerWidth && drag.y >= 0 && drag.y <= window.innerHeight) {
    window.scrollBy(0, speed * dt * 1.1);
    markGroupingDrop(drag.x, drag.y);
  }
  groupingDragFrame = requestAnimationFrame(scrollGroupingDrag);
}

async function saveGroupOrder(order, drag) {
  if (!canDragGroups() || state.currentRun.run_id !== drag.runId) return;
  const run = state.currentRun;
  const scrollTop = window.scrollY;
  state.batchBusy = true;
  renderReview();
  try {
    const updated = await api(`/api/runs/${run.run_id}/group-order`, {
      method: "PATCH", body: { group_ids: order, base_revision: drag.revision, base_order_revision: drag.orderRevision },
    });
    if (state.currentRun?.run_id === run.run_id) Object.assign(state.currentRun, updated);
    toast("分组顺序已保存");
  } catch (error) {
    toast(error.message);
    if (error.message.includes("刷新") && state.currentRun?.run_id === run.run_id) await openRun(run.run_id);
  } finally {
    state.batchBusy = false;
    renderReview();
    window.scrollTo(0, scrollTop);
  }
}

function bindGroupingDrag() {
  const container = $("#review-groups");
  container.addEventListener("dragstart", (event) => {
    const handle = event.target.closest("[data-drag-group]");
    const photo = event.target.closest("[data-drag-photo]");
    if (!canDragGroups() || (!handle && !photo)) { event.preventDefault(); return; }
    endGroupingDrag();
    const run = state.currentRun;
    const index = Number(photo?.dataset.dragPhoto);
    groupingDrag = { kind: handle ? "group" : "photos", groupId: Number(handle?.dataset.dragGroup),
      indexes: state.batchGrouping && state.selectedIndices.has(index) ? [...state.selectedIndices] : [index],
      runId: run.run_id, revision: run.review_revision, orderRevision: run.group_order_revision || 0,
      x: event.clientX, y: event.clientY };
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", handle ? `分组 ${handle.dataset.dragGroup}` : `移动 ${groupingDrag.indexes.length} 张照片`);
    (handle?.closest(".photo-group") || photo.closest(".photo-card")).classList.add("drag-source");
    document.body.classList.add("grouping-dragging");
    groupingDragFrame = requestAnimationFrame(scrollGroupingDrag);
  });
  document.addEventListener("dragover", (event) => {
    if (!groupingDrag) return;
    event.preventDefault();
    groupingDrag.x = event.clientX;
    groupingDrag.y = event.clientY;
    event.dataTransfer.dropEffect = markGroupingDrop(event.clientX, event.clientY) ? "move" : "none";
  });
  document.addEventListener("drop", (event) => {
    if (!groupingDrag) return;
    event.preventDefault();
    const drag = groupingDrag;
    const target = markGroupingDrop(event.clientX, event.clientY);
    endGroupingDrag();
    suppressDragClickUntil = Date.now() + 400;
    if (!target || !canDragGroups() || state.currentRun.run_id !== drag.runId) return;
    if (drag.kind === "photos") {
      const indexes = drag.indexes.filter((index) => state.currentRun.results.some((item) => item.index === index && !item.excluded && Number(item.group_id) !== target.id));
      if (indexes.length) applyGroupChange(indexes, { selected: String(target.id), preserveScroll: true, baseRevision: drag.revision });
    } else {
      const order = reviewGroupOrder();
      const moved = order.filter((id) => id !== drag.groupId);
      moved.splice(moved.indexOf(target.id) + (target.after ? 1 : 0), 0, drag.groupId);
      if (order.join() !== moved.join()) saveGroupOrder(moved, drag);
    }
  });
  document.addEventListener("dragend", () => { if (groupingDrag) suppressDragClickUntil = Date.now() + 400; endGroupingDrag(); });
  document.addEventListener("click", (event) => {
    const framepick = event.target.closest("[data-framepick-launch]");
    if (framepick && !framepick.disabled) {
      framepick.disabled = true;
      api("/api/toolbox/framepick/launch", { method: "POST", body: {} })
        .then(() => toast("帧选已打开"))
        .catch(error => toast(error.message))
        .finally(() => { framepick.disabled = false; });
      return;
    }
    if (Date.now() < suppressDragClickUntil && event.target.closest("#review-groups")) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && groupingDrag) { event.stopImmediatePropagation(); endGroupingDrag(); } }, true);
  document.addEventListener("dragleave", (event) => { if (groupingDrag && !event.relatedTarget) groupingDrag.y = -1; });
  window.addEventListener("blur", endGroupingDrag);
  window.addEventListener("hashchange", endGroupingDrag);
  document.addEventListener("visibilitychange", () => { if (document.hidden) endGroupingDrag(); });
}

function photoScoreMarkup(item, grouping) {
  if (grouping || !Number.isFinite(item.score) || item.score < 0 || item.score > 1) return "";
  return `<span class="photo-score" title="综合分（满分 100）">综合分 ${(item.score * 100).toFixed(1)}</span>`;
}

function renderReview() {
  const run = state.currentRun;
  $("#review-empty").classList.toggle("hidden", Boolean(run));
  $("#review-workspace").classList.toggle("hidden", !run);
  if (!run) return;
  const scored = run.workflow_state === "scored";
  const grouping = state.groupingMode || !scored || run.needs_rescore;
  const activeItems = run.results.filter((item) => !item.excluded);
  const excludedItems = run.results.filter((item) => item.excluded && !item.deleted);
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
  $("#selection-export-open").classList.toggle("hidden", grouping || !run.xmp_ready);
  $("#score-run").classList.toggle("hidden", !grouping);
  $("#score-run").textContent = scored ? "重新 AI 选片" : "开始 AI 选片";
  $("#score-run").disabled = Boolean(state.activeJob) || state.batchBusy || activeItems.length === 0 || !modelSetupStatus().ready;
  $("#score-settings").classList.toggle("hidden", !grouping);
  $("#review-flow-copy").textContent = grouping
    ? "核对相似分组，确认后开始 AI 评分"
    : `${run.candidate_count} 张候选已就绪；确认后导出选择`;
  $("#grouping-toggle").classList.toggle("hidden", !scored || Boolean(run.needs_rescore));
  $("#grouping-toggle").textContent = grouping ? "返回审片" : "调整分组";
  $("#reclassify-run").disabled = Boolean(state.activeJob) || state.batchBusy;
  $("#batch-select-toggle").classList.toggle("hidden", !grouping);
  $("#batch-select-toggle").textContent = batch ? "结束批量" : "批量选择";
  $("#batch-select-toggle").disabled = Boolean(jobBlocksProject()) || state.batchBusy;
  const selectionToolbar = $("#selection-toolbar");
  selectionToolbar.classList.toggle("hidden", !batch);
  $("#selection-count").textContent = `已选 ${state.selectedIndices.size} 张`;
  const orderedGroups = reviewGroupOrder(run);
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
  $("#selection-delete").disabled = state.batchBusy || state.selectedIndices.size === 0;
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
  const groupSections = [...groups.entries()].sort((a, b) => orderedGroups.indexOf(Number(a[0])) - orderedGroups.indexOf(Number(b[0]))).map(([groupId, items]) => {
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
      const selector = batch ? `<label class="card-select" title="选择 ${escapeHtml(item.filename)}"><input type="checkbox" data-select-index="${item.index}" ${selected ? "checked" : ""} ${state.batchBusy || jobBlocksProject() ? "disabled" : ""}><span></span></label>` : "";
      const actionDisabled = jobBlocksProject() || state.batchBusy;
      const actions = grouping && !batch ? `<div class="card-actions">
        <button class="move-group-button" data-move-group="${item.index}" ${actionDisabled ? "disabled" : ""}>移动到…</button>
        <button class="move-group-button" data-shift-index="${item.index}" data-shift-direction="previous" ${actionDisabled || Number(item.group_id) === firstGroup ? "disabled" : ""}>上移</button>
        <button class="move-group-button" data-shift-index="${item.index}" data-shift-direction="next" ${actionDisabled || Number(item.group_id) === lastGroup ? "disabled" : ""}>下移</button>
        <button class="move-group-button" data-new-group-index="${item.index}" ${actionDisabled ? "disabled" : ""}>新建组</button>
        <button class="move-group-button remove-item-button" data-exclude-index="${item.index}" ${actionDisabled ? "disabled" : ""}>移出</button>
        <button class="move-group-button delete-item-button" data-delete-photo-index="${item.index}" ${actionDisabled ? "disabled" : ""}>删除</button>
      </div>` : "";
      return `<article class="photo-card ${grouping ? "grouping-card" : ""} ${selected ? "selected" : ""} ${show ? "" : "hidden-by-filter"}" data-rating="${item.effective_rating}" data-index="${item.index}" aria-selected="${selected}">
        ${selector}
        <button class="preview-button" data-open-photo="${item.index}" ${grouping ? `data-drag-photo="${item.index}" draggable="${!actionDisabled}" title="拖动照片到其他分组"` : ""}><img draggable="false" src="${item.preview_url}" loading="lazy" alt="${escapeHtml(item.filename)}"></button>
        <div class="photo-meta">
          <div class="photo-line"><strong title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</strong>${photoScoreMarkup(item, grouping)}</div>
          ${actions}
          ${statusLine}
          ${grouping ? "" : ratingControls(item)}
          ${warningLine}
        </div>
      </article>`;
    }).join("");
    const groupVisible = grouping || items.some(matchesFilter);
    const selectGroup = batch ? `<button class="text-button select-group" data-select-group="${groupId}">选择本组</button>` : "";
    const dragHandle = grouping ? `<button class="group-drag-handle" data-drag-group="${groupId}" draggable="${!jobBlocksProject() && !state.batchBusy}" ${jobBlocksProject() || state.batchBusy ? "disabled" : ""} title="拖动调整分组顺序" aria-label="拖动组 ${groupId} 调整顺序">⠿</button>` : "";
    const kept = items.filter((item) => item.effective_rating >= 3).length;
    const selectionSummary = grouping ? "" : `<span class="group-selection-count">${kept ? `候选 ${kept} 张` : "本组未入选"}</span>`;
    return {key: `group:${groupId}`, html: `<section class="photo-group ${groupVisible ? "" : "hidden-by-filter"}" data-group-id="${groupId}"><div class="group-head">${dragHandle}<strong>组 ${groupId}</strong><span>${items.length === 1 ? "单张" : `${items.length} 张相似照片`}</span>${selectionSummary}${selectGroup}</div><div class="filmstrip">${cards}</div></section>`};
  });
  const unreadableCount = excludedItems.filter((item) => item.read_error).length;
  const excludedMarkup = (grouping || unreadableCount) && excludedItems.length ? `<details class="excluded-group" ${unreadableCount ? "open" : ""}>
    <summary>未参与评分 ${excludedItems.length} 张${unreadableCount ? ` · 读取失败 ${unreadableCount} 张` : ""}</summary>
    <div class="excluded-head"><span>${unreadableCount ? "读取失败的照片请修复源文件后重新分类。" : "这些照片不参与评分或 XMP，RAW 文件仍在原处。"}</span>${excludedItems.some((item) => !item.read_error) ? '<button class="text-button" data-restore-all>恢复已移出照片</button>' : ""}</div>
    <div class="filmstrip">${excludedItems.map((item) => `<article class="photo-card removed-card" data-index="${item.index}">
      ${item.read_error ? `<div class="unreadable-preview" title="${escapeHtml(item.read_error)}">读取失败</div>` : `<button class="preview-button" data-open-photo="${item.index}"><img src="${item.preview_url}" loading="lazy" alt="${escapeHtml(item.filename)}"></button>`}
      <div class="photo-meta"><div class="photo-line"><strong title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</strong>${item.read_error ? "" : `<button class="move-group-button" data-restore-index="${item.index}">恢复</button>`}<button class="move-group-button delete-item-button" data-delete-photo-index="${item.index}" ${jobBlocksProject() || state.batchBusy ? "disabled" : ""}>删除</button></div></div>
    </article>`).join("")}</div>
  </details>` : "";
  const sections = !grouping && visible === 0
    ? [{key: "empty", html: `<div class="empty">当前筛选下没有照片</div>`}]
    : groupSections.length ? groupSections : [{key: "empty", html: `<div class="empty">当前没有参与评分的照片</div>`}];
  if (excludedMarkup) sections.push({key: "excluded", html: excludedMarkup});
  if (reviewGrid) reviewGrid.render(run.run_id, sections);
  else $("#review-groups").innerHTML = sections.map(section => section.html).join("");
  $("#visible-count").textContent = grouping || state.filter === "all" ? "" : `显示 ${visible} / ${activeItems.length}`;
  $("#selection-export-open").disabled = Number(run.candidate_count || 0) < 1;
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
  const request = ++reviewOpenRequest;
  await ratingSave;
  if (request !== reviewOpenRequest) return;
  const opened = await api(`/api/runs/${runId}`);
  if (request !== reviewOpenRequest) return;
  state.dialogIndex = null;
  state.dialogSequence = null;
  if ($("#photo-dialog").open) $("#photo-dialog").close();
  state.currentRun = opened;
  const project = state.projects.find((item) => item.latest_run_id === runId);
  if (project) state.currentProject = project;
  else if (state.currentProject?.latest_run_id !== runId) state.currentProject = null;
  state.developPlan = null;
  state.developBusy = false;
  state.developProgress = null;
  state.developFlowProgress = null;
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
  return opened;
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
  return runId != null && String(runId) === String(state.currentRun.run_id);
}

function jobMatchesProject(job, run = state.currentRun, project = state.currentProject) {
  const scope = job.context || {};
  if (run?.run_id && [scope.run_id, scope.source_run_id, job.result?.run_id].includes(run.run_id)) return true;
  return Boolean(scope.project_id && scope.project_id === (run?.project_id || project?.project_id));
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
  if ($("#view-export").classList.contains("active")) renderExport();
  pollJobs();
  return job;
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
  const run = state.currentRun;
  const navigationRequest = reviewOpenRequest;
  await ratingSave;
  if (state.currentRun !== run || !run) return;
  const summary = await api(`/api/runs/${run.run_id}/selection-export`);
  if (state.currentRun !== run || navigationRequest !== reviewOpenRequest) return;
  state.selectionExport = summary;
  setView("export", `export/${run.run_id}`, historyMode);
  renderExport();
}

async function goWorkflowStage(stage) {
  const run = state.currentRun;
  await ratingSave;
  if (!run || state.currentRun !== run) return;
  if (stage === "export") return loadExport();
  renderReview();
  setView("review", currentProjectRoute(), "push");
}

function setRating(index, rating) {
  if (!state.currentRun || state.groupingMode || !state.currentRun.xmp_ready) return;
  const parsed = rating === "reset" ? null : Number(rating);
  if (parsed != null && ![0, 3, 4, 5].includes(parsed)) return;
  const run = state.currentRun;
  const save = () => saveRating(run, Number(index), parsed);
  ratingSave = ratingSave.then(save, save);
  return ratingSave;
}

async function saveRating(run, index, parsed) {
  const item = run.results.find((value) => value.index === index && !value.excluded && !value.deleted);
  if (!item) return;
  try {
    const update = await api(`/api/runs/${run.run_id}/items/${index}`, {
      method: "PATCH",
      body: { rating: parsed, base_revision: run.review_revision },
    });
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
    run.review_revision = update.review_revision;
    if (run.develop?.exists) {
      run.develop.stale = true;
      run.develop.confirmed_count = 0;
    }
    run.manual_adjusted_count = update.manual_adjusted_count;
    const active = run.results.filter((value) => !value.excluded && !value.deleted);
    run.candidate_count = active.filter((value) => value.effective_rating >= 3).length;
    run.strong_count = active.filter((value) => value.effective_rating >= 4).length;
    syncProjectRunStats(run);
    if (state.currentRun === run) {
      state.developPlan = null;
      renderReview();
    }
  } catch (error) {
    toast(error.message);
    if (error.message.includes("刷新")) {
      // Do not call openRun here: it waits for this queue. Refresh the captured
      // run, never whichever project the user navigated to during the request.
      try {
        Object.assign(run, await api(`/api/runs/${run.run_id}`));
        if (state.currentRun === run) renderReview();
      } catch (refreshError) { toast(refreshError.message); }
    }
  }
}

function photoGroupItems(index = state.dialogIndex) {
  const run = state.currentRun;
  const current = run?.results.find((item) => item.index === Number(index));
  if (!current || current.deleted) return [];
  const grouping = state.groupingMode || run.workflow_state !== "scored" || run.needs_rescore;
  return run.results.filter((item) => !item.deleted && Boolean(item.excluded) === Boolean(current.excluded)
    && Number(item.group_id) === Number(current.group_id))
    .sort(grouping ? (a, b) => a.index - b.index
      : (a, b) => b.effective_rating - a.effective_rating || b.score - a.score || a.index - b.index);
}

function navigatePhotoGroup(direction) {
  const items = photoNavigationItems().filter(item => item.index !== reviewViewer?.referenceIndex);
  const position = items.findIndex((item) => item.index === state.dialogIndex);
  const next = items[position + direction];
  if (position >= 0 && next) updatePhotoDialog(next.index);
}

function photoNavigationItems(index = state.dialogIndex) {
  const run = state.currentRun;
  const current = run?.results.find((item) => item.index === Number(index));
  if (!current) return [];
  const grouping = state.groupingMode || run.workflow_state !== "scored" || run.needs_rescore;
  const items = photoGroupItems(index);
  const snapshot = state.dialogSequence;
  if (!snapshot || snapshot.run !== run || snapshot.group !== current.group_id
      || snapshot.excluded !== Boolean(current.excluded) || snapshot.grouping !== grouping) {
    state.dialogSequence = {run, group: current.group_id, excluded: Boolean(current.excluded),
      grouping, indexes: items.map((item) => item.index)};
  }
  const byIndex = new Map(items.map((item) => [item.index, item]));
  const order = state.dialogSequence.indexes.filter((value) => byIndex.has(value));
  items.forEach((item) => { if (!order.includes(item.index)) order.push(item.index); });
  state.dialogSequence.indexes = order;
  return order.map((value) => byIndex.get(value));
}

function updatePhotoDialog(index) {
  const item = state.currentRun?.results.find((value) => value.index === Number(index));
  if (!item || item.deleted) return;
  state.dialogIndex = item.index;
  if ($("#dialog-image").getAttribute?.("src") !== item.preview_url) $("#dialog-image").src = item.preview_url;
  $("#dialog-image").alt = item.filename;
  const items = photoNavigationItems(item.index);
  const position = items.findIndex((value) => value.index === item.index);
  $("#dialog-previous").disabled = position <= 0;
  $("#dialog-next").disabled = position < 0 || position >= items.length - 1;
  $("#dialog-position").textContent = `组 ${item.group_id} · ${position + 1} / ${items.length}`;
  const grouping = state.groupingMode || state.currentRun.workflow_state !== "scored" || state.currentRun.needs_rescore;
  reviewViewer?.setPhoto(item, items, grouping);
  if (reviewViewer?.referenceIndex != null) {
    const candidates = items.filter(value => value.index !== reviewViewer.referenceIndex);
    const current = candidates.findIndex(value => value.index === item.index);
    $("#dialog-previous").disabled = current <= 0;
    $("#dialog-next").disabled = current < 0 || current >= candidates.length - 1;
  }
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
  state.dialogSequence = null;
  reviewViewer?.reset();
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
    .sort((a, b) => reviewGroupOrder(run).indexOf(Number(a[0])) - reviewGroupOrder(run).indexOf(Number(b[0])))
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

async function applyGroupChange(indexes, { selected = null, direction = null, closeDialog = false, preserveScroll = false, baseRevision = null } = {}) {
  const run = state.currentRun;
  indexes = [...new Set(indexes.map(Number))];
  if (!run || !indexes.length) return;
  if (jobBlocksProject() || state.batchBusy) return toast("请等待当前任务完成");
  const scrollTop = window.scrollY;
  const body = { indexes, base_revision: baseRevision ?? run.review_revision };
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
    if (preserveScroll) window.scrollTo(0, scrollTop);
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
  if (!state.currentRun || jobBlocksProject() || state.batchBusy) return;
  state.batchGrouping = !state.batchGrouping;
  clearGroupingSelection();
  renderReview();
}

function selectGroupingIndex(index, selected, range = false) {
  const active = reviewGroupOrder().flatMap((id) => state.currentRun.results
    .filter((item) => !item.excluded && Number(item.group_id) === id).map((item) => item.index));
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
  if (!run || !indexes.length || jobBlocksProject() || state.batchBusy) return;
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

function openDeletePhotos(indexes) {
  const run = state.currentRun;
  if (!run || jobBlocksProject() || state.batchBusy || !indexes.length) return;
  const items = run.results.filter((item) => indexes.includes(item.index) && !item.deleted);
  if (!items.length) return;
  state.deletePhotoTarget = { runId: run.run_id, revision: Number(run.review_revision), indexes: items.map((item) => item.index) };
  $("#delete-photos-copy").textContent = items.length === 1
    ? `删除 ${items[0].filename}？源文件将移入照片目录内的 .photo-ai-trash/selection 回收区，并从工程中移除。`
    : `删除所选 ${items.length} 张照片？源文件将移入照片目录内的 .photo-ai-trash/selection 回收区，并从工程中移除。`;
  $("#delete-photos-dialog").showModal();
}

async function deletePhotos() {
  const target = state.deletePhotoTarget;
  if (!target || state.batchBusy || jobBlocksProject() || target.runId !== state.currentRun?.run_id) return;
  const button = $("#delete-photos-submit");
  state.batchBusy = true;
  button.disabled = true;
  button.textContent = "正在删除…";
  renderReview();
  try {
    const result = await api(`/api/runs/${target.runId}/photos/delete`, {
      method: "POST", body: { indexes: target.indexes, base_revision: target.revision, confirmation: "delete-source-files" },
    });
    $("#delete-photos-dialog").close();
    if ($("#photo-dialog").open) $("#photo-dialog").close();
    state.dialogIndex = null;
    state.deletePhotoTarget = null;
    clearGroupingSelection();
    const route = location.hash.replace(/^#/, "") || `review/${target.runId}`;
    const batch = state.batchGrouping;
    await openRun(target.runId, route);
    state.groupingMode = true;
    state.batchGrouping = batch;
    toast(`已删除 ${result.deleted_count} 张，源文件已移入回收区`);
  } catch (error) {
    toast(error.message);
    if (error.message.includes("刷新")) {
      $("#delete-photos-dialog").close();
      state.deletePhotoTarget = null;
      await openRun(target.runId);
    }
  } finally {
    state.batchBusy = false;
    button.disabled = false;
    button.textContent = "删除并移入回收区";
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

function reclassificationRunId(job) {
  return job?.kind === "score" && ["failed", "interrupted"].includes(job.status)
    && job.requires_reclassification !== false
    && /照片目录内容已变化|照片在分类后发生变化/.test(job.message || "")
    ? String(job.context?.source_run_id || "") : "";
}

async function reclassifyRun(runId = state.currentRun?.run_id) {
  if (!runId || state.activeJob || state.batchBusy) return;
  if (!await requireModelSetupUi()) return;
  state.batchBusy = true;
  renderReview();
  try {
    const run = state.currentRun?.run_id === runId ? state.currentRun : await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (!run.project_id) throw new Error("工程信息不完整，请刷新后重试。 ");
    return await startJob(`/api/projects/${encodeURIComponent(run.project_id)}/group`, {
      retain_ratio: Number(run.retain_ratio || 0.30),
      mode: run.scoring_mode === "fast" ? "fast" : "deep",
    });
  } finally {
    state.batchBusy = false;
    renderReview();
  }
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
  const job = typeof requestedJob === "string" ? state.jobs.find(item => item.id === requestedJob) : requestedJob;
  if (!job?.id || state.jobRetryBusy) return;
  state.jobRetryBusy = true;
  try { activateJob(await api(`/api/jobs/${job.id}/retry`, {method: "POST"})); pollJobs(); }
  catch (error) { toast(error.message); }
  finally { state.jobRetryBusy = false; renderJobBar(); }
}

async function cancelActiveJob() {
  const current = currentProgressJob();
  if (!current?.id || current.status === "cancelling") return;
  const previousStatus = current.status;
  if (current.foreground) state.developProgress = { ...state.developProgress, status: "cancelling" };
  current.status = "cancelling";
  current.stage = "正在取消";
  renderJobBar();
  renderModelInstallProgress(current);
  try {
    const cancelled = await api(`/api/jobs/${current.id}`, { method: "DELETE" });
    if (cancelled.kind === "develop" && cancelled.context?.run_id === state.currentRun?.run_id) {
      state.developProgress = { ...(state.developProgress || {}), status: cancelled.status, job_id: cancelled.id, run_id: cancelled.context.run_id, stage_label: cancelled.stage, message: cancelled.message };
    }
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
    current.status = previousStatus;
    if (current.foreground) state.developProgress.status = previousStatus;
    else state.activeJob = current;
    renderJobBar();
    renderModelInstallProgress(current);
    toast(error.message);
  }
}

async function pollJobs() {
  if (jobsPolling) { jobsPollAgain = true; return; }
  jobsPolling = true;
  clearTimeout(pollJobs.timer);
  try {
    state.jobs = await api("/api/jobs");
    const active = state.jobs.find(job => ["queued", "running", "cancelling"].includes(job.status));
    const changed = (state.activeJob?.id || null) !== (active?.id || null);
    state.activeJob = active || null;
    renderJobBar();
    if (changed) { renderReview(); renderToolbox(); }
    for (const job of state.jobs.filter(job => ["completed", "failed", "cancelled", "interrupted"].includes(job.status))) {
      if (state.handledJobs.has(job.id)) continue;
      state.handledJobs.add(job.id);
      if (job.status === "completed") {
        toast(`${job.title}已完成`);
        await refreshBootstrap();
        if (["group", "score"].includes(job.kind) && job.result?.run_id && jobMatchesProject(job)
          && /^(review|export|project)(\/|$)/.test(location.hash.slice(1))) await openRun(job.result.run_id);
        if (["xmp_commit", "rollback", "selection_copy"].includes(job.kind) && state.currentRun && jobMatchesProject(job)
          && /^(review|export|project)(\/|$)/.test(location.hash.slice(1))) {
          const run = state.currentRun;
          const onExport = location.hash.startsWith("#export/");
          await openRun(run.run_id, onExport ? `export/${run.run_id}` : currentProjectRoute());
          if (onExport) await loadExport("replace");
        }
        if (["raw_jpeg_execute", "raw_jpeg_rollback"].includes(job.kind)) state.rawJpegPlan = null;
        if (job.kind.startsWith("xmp_cleanup")) state.xmpCleanupPlan = null;
      } else if (["failed", "interrupted"].includes(job.status)) {
        state.jobNotice = job;
        toast(`${job.title}：${job.message || (job.status === "failed" ? "失败" : "已中断")}`);
      }
      if (job.kind === "model_download") await refreshModelResources();
      renderJobBar(); renderToolbox();
    }
    if (active) pollJobs.timer = setTimeout(pollJobs, 1200);
  } catch (_error) { pollJobs.timer = setTimeout(pollJobs, 2500); }
  finally {
    jobsPolling = false;
    if (jobsPollAgain) { jobsPollAgain = false; clearTimeout(pollJobs.timer); pollJobs.timer = setTimeout(pollJobs, 0); }
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
    await openRun(requested.split("/")[1]);
    return;
  }
  if (requested.startsWith("export/")) {
    const runId = requested.slice("export/".length).split("/")[0];
    const run = await openRun(runId, requested, "replace");
    if (!run || state.currentRun !== run) return;
    try {
      await loadExport("replace");
    } catch (error) {
      if (state.currentRun !== run || location.hash.replace(/^#/, "") !== requested) return;
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
  if (!["cull", "settings"].includes(requested)) return openToolboxSection("home", "replace");
  setView(requested);
}

function bindEvents() {
  $("#thumbnail-size").addEventListener("input", (event) => {
    state.thumbnailDirty = true;
    setThumbnailSize(event.target.value);
  });
  $("#thumbnail-size").addEventListener("change", saveThumbnailSize);
  $("#dialog-previous").addEventListener("click", () => navigatePhotoGroup(-1));
  $("#dialog-next").addEventListener("click", () => navigatePhotoGroup(1));
  let updateTimer = null;
  async function renderAppUpdate() {
    clearTimeout(updateTimer);
    try {
      const info = await api("/api/app-updates");
      const busy = ["checking", "downloading", "installing"].includes(info.phase);
      const buildCopy = info.build?.status === "identified" ? ` · 构建 ${info.build.id}`
        : ["invalid", "mismatch"].includes(info.build?.status) ? " · 构建资料不匹配" : "";
      $("#app-update-version").textContent = `当前 ${info.current_version}${buildCopy}${info.release ? ` · 可用 ${info.release.version}` : ""}`;
      $("#app-update-check").disabled = busy;
      $("#app-update-download").classList.toggle("hidden", !info.release || busy || info.phase === "ready");
      $("#app-update-cancel").classList.toggle("hidden", info.phase !== "downloading");
      $("#app-update-install").classList.toggle("hidden", info.phase !== "ready");
      $("#app-update-install").disabled = Boolean(state.activeJob) || !window.__TAURI__?.core?.invoke;
      $("#app-update-progress").classList.toggle("hidden", info.phase !== "downloading");
      $("#app-update-progress").value = info.release?.size ? 100 * info.downloaded / info.release.size : 0;
      const labels = {idle:"", checking:"正在检查 GitHub Release…", current:"已是当前通道最新版本。", available:"发现新版本。", ready:"下载完成。安装仅更新程序，保留工程、模型和设置。"};
      $("#app-update-message").textContent = info.phase === "downloading"
        ? `已下载 ${(info.downloaded/1048576).toFixed(1)} / ${(info.release.size/1048576).toFixed(1)} MB · ${(info.speed/1048576).toFixed(1)} MB/s`
        : info.message || labels[info.phase] || "";
      if (info.phase === "idle" && info.last_install) $("#app-update-message").textContent = info.last_install;
      $("#app-update-notes-wrap").classList.toggle("hidden", !info.release?.notes);
      $("#app-update-notes").textContent = info.release?.notes || "";
      if (busy) updateTimer = setTimeout(renderAppUpdate, 700);
    } catch (error) { $("#app-update-message").textContent = error.message; }
  }
  for (const action of ["check", "download", "cancel", "install"]) {
    $(`#app-update-${action}`).addEventListener("click", async () => {
      if (action === "install" && !confirm("现在退出并安装更新？工程、模型和设置会保留，完成后自动打开。")) return;
      try {
        await api(`/api/app-updates/${action}`, {method:"POST"});
        if (action === "install") await window.__TAURI__.core.invoke("exit_for_update");
        else await renderAppUpdate();
      } catch (error) { $("#app-update-message").textContent = error.message; }
    });
  }
  renderAppUpdate();
  $("#app-diagnostics-copy").addEventListener("click", copyDiagnostics);
  $("#vision-mode").addEventListener("change", () => $("#vision-cloud-fields").classList.toggle("hidden", $("#vision-mode").value !== "cloud"));
  $("#vision-bailian").addEventListener("click", () => {
    $("#vision-url").value = "https://dashscope.aliyuncs.com/compatible-mode/v1";
    $("#vision-model").value = "qwen3-vl-plus";
    refreshVisionThinkingOptions();
  });
  $("#vision-url").addEventListener("change", refreshVisionThinkingOptions);
  $("#vision-model").addEventListener("change", refreshVisionThinkingOptions);
  $("#vision-provider-form").addEventListener("submit", saveVisionProviderSettings);
  $("#vision-test").addEventListener("click", async () => {
    $("#vision-test").disabled = true;
    $("#vision-status").textContent = "正在测试已保存配置（仅发送测试色块）…";
    try { $("#vision-status").textContent = (await api("/api/vision-provider/test", {method: "POST"})).message; }
    catch (error) { $("#vision-status").textContent = error.message; }
    finally { $("#vision-test").disabled = false; }
  });
  api("/api/vision-provider").then((config) => {
    $("#vision-mode").value = config.mode;
    $("#vision-url").value = config.base_url;
    $("#vision-model").value = config.model;
    $("#vision-timeout").value = config.timeout;
    $("#vision-consent").checked = config.upload_consent;
    renderVisionThinkingOptions(config.thinking_capabilities, config.thinking_effort);
    $("#vision-cloud-fields").classList.toggle("hidden", config.mode !== "cloud");
    if (config.has_key) $("#vision-key").placeholder = "已保存密钥；留空不变";
  }).catch((error) => { $("#vision-status").textContent = error.message; });
  bindGroupingDrag();
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
    if (currentProgressJob()?.id && ["queued", "running", "cancelling"].includes(currentProgressJob().status)) return cancelActiveJob();
    state.developProgress = null;
    state.developFlowProgress = null;
    state.jobNotice = null;
    renderJobBar();
  });
  $("#job-retry").addEventListener("click", () => retryFailedJob());
  $("#review-back").addEventListener("click", () => setView("cull", "cull", "push"));
  $("#selection-export-open").addEventListener("click", () => goWorkflowStage("export").catch((error) => toast(error.message)));
  $("#photo-dialog").addEventListener("close", () => {
    state.dialogIndex = null;
    state.dialogSequence = null;
    reviewViewer?.reset();
  });
  if (globalThis.PhotoReviewViewer) reviewViewer = new PhotoReviewViewer($("#photo-dialog"), {
    selectPhoto: updatePhotoDialog, ratings: ratingControls, score: photoScoreMarkup,
  });
  if (globalThis.KeyedReviewGrid) reviewGrid = new KeyedReviewGrid($("#review-groups"));
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
  $("#lightroom-config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    configureLightroom(false);
  });
  $("#lightroom-auto-configure").addEventListener("click", () => configureLightroom(true));
  $("#lightroom-refresh").addEventListener("click", refreshLightroomConnection);
  $("#lightroom-path").addEventListener("input", renderLightroom);
  $("#export-back").addEventListener("click", () => goWorkflowStage("review").catch(error => toast(error.message)));
  $("#grouping-toggle").addEventListener("click", toggleGrouping);
  $("#reclassify-run").addEventListener("click", () => reclassifyRun().catch((error) => toast(error.message)));
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
  $("#selection-delete").addEventListener("click", () => openDeletePhotos([...state.selectedIndices]));
  $("#delete-photos-submit").addEventListener("click", deletePhotos);
  $("#score-run").addEventListener("click", () => scoreCurrentGroups().catch(() => {}));
  $("#group-submit").addEventListener("click", moveGroup);
  $("#rollback-submit").addEventListener("click", rollbackXmp);
  $("#delete-project-submit").addEventListener("click", deleteProject);
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-open-cull]")) { setView("cull", "cull", "push"); return; }
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
    const deletePhotoButton = event.target.closest("[data-delete-photo-index]");
    if (deletePhotoButton) openDeletePhotos([Number(deletePhotoButton.dataset.deletePhotoIndex)]);
    const restoreButton = event.target.closest("[data-restore-index]");
    if (restoreButton) updateExcluded([Number(restoreButton.dataset.restoreIndex)], false);
    const restoreAllButton = event.target.closest("[data-restore-all]");
    if (restoreAllButton) updateExcluded(state.currentRun?.results.filter((item) => item.excluded && !item.deleted && !item.read_error).map((item) => item.index) || [], false);
    const rollbackButton = event.target.closest(".rollback-open");
    if (rollbackButton) { state.rollbackId = rollbackButton.dataset.transactionId; $("#rollback-dialog").showModal(); }
    const closeButton = event.target.closest("[data-close-dialog]");
    if (closeButton) { event.preventDefault(); $(`#${closeButton.dataset.closeDialog}`).close(); }

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
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      navigatePhotoGroup(event.key === "ArrowRight" ? 1 : -1);
    }
    if (!event.repeat && !event.ctrlKey && !event.metaKey && !event.altKey && ["0", "3", "4", "5"].includes(event.key)) {
      event.preventDefault();
      setRating(state.dialogIndex, event.key);
    }
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

function renderWorkflow(activeStage) {
  $$(".workflow-step").forEach(node => {
    const stage = node.dataset.workflowStage;
    node.disabled = stage === "export" && !state.currentRun?.xmp_ready;
    node.classList.toggle("active", stage === activeStage);
    node.classList.toggle("completed", stage === "review" && activeStage === "export");
    node.setAttribute("aria-current", stage === activeStage ? "step" : "false");
  });
}
function renderExport() {
  if (!state.currentRun) return;
  $("#export-title").textContent = "导出选择";
  $("#export-summary").textContent = `${state.currentRun.candidate_count || 0} 张候选 · 星级与选择记录`;
  PhotoSelectionOutput.render(state.currentRun, state.selectionExport);
  renderWorkflow("export");
}
