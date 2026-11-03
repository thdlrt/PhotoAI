/* Collaborative review: model suggestions never overwrite a user's decision. */
globalThis.PhotoReviewWorkspace = (() => {
  let workspace = null, selected = null, dirty = false, busy = false, timer = null;
  let filter = "all", query = "", activeJob = null;
  let proxyJobs = {}, proxyTimer = null, playbackModes = {};
  const labels = {keep: "必用", maybe: "备选", reject: "不用", unreviewed: "未审"};
  const root = () => document.querySelector("#collaborative-review-root");
  const esc = value => escapeHtml(String(value ?? ""));
  const endpoint = () => `/api/review-workspaces/${workspace.workspace_id}`;
  const item = () => workspace?.items.find(value => value.id === selected);
  const visible = () => (workspace?.items || []).filter(value => (filter === "all" || value.user.status === filter)
    && (!query || `${value.name} ${value.relative_path} ${value.user.chapter} ${value.user.notes}`.toLowerCase().includes(query.toLowerCase())))
    .sort((a, b) => (a.user.order ?? a.assistant?.order ?? 0) - (b.user.order ?? b.assistant?.order ?? 0));

  async function open() {
    clearTimeout(timer);
    if (dirty && !await save()) return;
    const id = location.hash.split("/")[2];
    if (id && (!workspace || workspace.workspace_id !== id)) {
      workspace = await api(`/api/review-workspaces/${encodeURIComponent(id)}`);
      selected = workspace.items[0]?.id;
    }
    if (workspace) { render(); await resume(); return; }
    await home();
  }
  async function home() {
    const data = await api("/api/review-workspaces");
    root().innerHTML = `<form id="review-create-form" class="panel toolbox-create-form">
      <label>素材目录<span class="path-picker"><input name="input_root" id="review-input-root" required autocomplete="off" placeholder="照片、视频所在文件夹"><button class="button desktop-folder-picker hidden" data-folder-target="review-input-root" type="button">浏览</button></span></label>
      <label>审片名称<input name="name" placeholder="默认使用文件夹名"></label><button class="button primary" type="submit">创建审片</button>
      <p class="subtle tool-wide">AI 建议与个人选择独立保存，支持照片、视频与多个源片段。</p>
    </form><div class="section-title"><h2>继续审片</h2></div><div class="project-list">${(data.workspaces || []).map(value => `<button class="workspace-row" data-review-open="${esc(value.workspace_id)}"><strong>${esc(value.name)}</strong><small>${esc(value.count ?? value.item_count ?? value.asset_count ?? value.total ?? "")} 个素材 · ${esc(value.updated_at || value.created_at || "")}</small></button>`).join("") || '<div class="empty">还没有审片工程</div>'}</div>`;
    bindDesktopFolderPickers();
    root().querySelector("form").onsubmit = async event => {
      event.preventDefault();
      const button = event.currentTarget.querySelector('button[type="submit"]');
      button.disabled = true; button.textContent = "正在扫描素材…";
      try { workspace = await api("/api/review-workspaces", {method: "POST", body: Object.fromEntries(new FormData(event.currentTarget))}); selected = workspace.items[0]?.id; dirty = false; render(); await resume(); }
      catch (error) { toast(error.message); button.disabled = false; button.textContent = "创建审片"; }
    };
    root().querySelectorAll("[data-review-open]").forEach(button => button.onclick = async () => {
      try { workspace = await api(`/api/review-workspaces/${button.dataset.reviewOpen}`); selected = workspace.items[0]?.id; dirty = false; activeJob = null; render(); await resume(); }
      catch (error) { toast(error.message); }
    });
  }
  function render() {
    if (!workspace) return;
    history.replaceState(null, "", `#toolbox/review/${workspace.workspace_id}`);
    if (!workspace.items.some(value => value.id === selected)) selected = workspace.items[0]?.id;
    const done = workspace.items.filter(value => value.user.status !== "unreviewed").length;
    root().innerHTML = `<div class="workspace-heading"><div><h2>${esc(workspace.name)}</h2><p class="subtle">${done} / ${workspace.items.length} 已审 · 必用 ${workspace.items.filter(value => value.user.status === "keep").length}</p></div><div class="tool-actions"><button class="button" data-review-home>审片工程</button><a class="button link-button" href="${endpoint()}/export?format=json&selected_only=true" download>导出 JSON</a><a class="button link-button" href="${endpoint()}/export?format=csv&selected_only=true" download>导出 CSV</a></div></div>
      <div class="review-assist-bar panel"><button class="button" data-review-organize>按目录编排</button><button class="button" data-review-existing>沿用选片 AI 分数</button><button class="button primary" data-review-vision>AI 视觉建议</button><span class="subtle">每批最多 12 个素材；视频抽取 3 帧</span></div>
      <div id="review-ai-job" class="tool-job hidden" role="status"></div>
      <div class="collaborative-layout"><aside class="review-browser"><div class="review-browser-controls"><input id="review-search" aria-label="搜索素材" placeholder="搜索文件、章节、备注" value="${esc(query)}"><select id="review-filter" aria-label="筛选决定"><option value="all">全部素材</option>${Object.entries(labels).map(([key,label]) => `<option value="${key}" ${filter === key ? "selected" : ""}>${label}</option>`).join("")}</select></div><div id="review-asset-list"></div></aside><section id="review-detail" class="review-detail"></section></div>`;
    renderList(); renderDetail();
    const assistBar = root().querySelector(".review-assist-bar");
    assistBar.insertAdjacentHTML("beforeend", '<button class="button" data-review-storyboard>AI 编排 / 剪辑草案</button>');
    assistBar.querySelector("[data-review-storyboard]").onclick = async () => { if (await save()) PhotoStoryboard.open(workspace).catch(error => toast(error.message)); };
    root().querySelectorAll('a[download]').forEach(link => link.onclick = async event => {
      event.preventDefault();
      const href = link.href;
      if (!await save()) return;
      const download = document.createElement("a"); download.href = href; download.download = ""; document.body.append(download); download.click(); download.remove();
    });
    root().querySelector("[data-review-home]").onclick = async () => { if (!await save()) return; workspace = null; selected = null; activeJob = null; clearTimeout(timer); history.replaceState(null, "", "#toolbox/review"); await home(); };
    root().querySelector("#review-search").oninput = event => { query = event.target.value; renderList(); };
    root().querySelector("#review-filter").onchange = event => { filter = event.target.value; renderList(); };
    root().querySelector("[data-review-organize]").onclick = () => suggest("organize");
    root().querySelector("[data-review-existing]").onclick = () => suggest("existing_ai");
    root().querySelector("[data-review-vision]").onclick = () => suggest("vision");
    if (activeJob) updateJob(activeJob);
  }
  function renderList() {
    const list = root().querySelector("#review-asset-list");
    list.innerHTML = visible().map(value => `<button class="review-asset ${value.id === selected ? "active" : ""}" data-asset="${esc(value.id)}"><img src="${endpoint()}/preview/${value.id}" alt="" loading="lazy"><span><strong>${esc(value.name)}</strong><small>${value.kind === "video" ? "视频" : "照片"}${value.duration ? ` · ${Number(value.duration).toFixed(1)}s` : ""}</small><em class="decision-${value.user.status}">${labels[value.user.status] || "未审"}${value.user.chapter ? ` · ${esc(value.user.chapter)}` : ""}</em></span></button>`).join("") || '<p class="empty">没有符合筛选条件的素材</p>';
    list.querySelectorAll("[data-asset]").forEach(button => button.onclick = () => select(button.dataset.asset));
  }
  function renderDetail() {
    const asset = item(), detail = root().querySelector("#review-detail");
    if (!asset) { detail.innerHTML = '<div class="empty">目录中没有支持的素材</div>'; return; }
    const user = asset.user, assistant = asset.assistant || {};
    const proxy = proxyJobs[asset.id];
    detail.innerHTML = `<div class="review-media">${asset.available === false ? '<p>源文件暂时不可用</p>' : asset.kind === "video" ? `<video id="review-video" controls preload="metadata" src="${endpoint()}/media/${asset.id}"></video>` : `<img src="${endpoint()}/preview/${asset.id}" alt="${esc(asset.name)}">`}</div>
      <div class="review-detail-title"><div><strong>${esc(asset.name)}</strong><small>${esc(asset.relative_path)}</small></div><div class="tool-actions"><button class="button" data-review-prev aria-label="上一素材">←</button><button class="button" data-review-next aria-label="下一素材">→</button></div></div>
      <div id="review-suggestion" class="suggestion-card">${suggestionHtml(assistant)}</div>
      <form id="review-decision-form"><div class="decision-buttons" role="group" aria-label="我的选择">${Object.entries(labels).map(([key,label], index) => `<button class="button ${user.status === key ? "selected" : ""}" type="button" data-decision="${key}" aria-pressed="${user.status === key}">${label}<small>${key === "unreviewed" ? "0" : index + 1}</small></button>`).join("")}</div>
      <div class="review-fields"><label>章节<input name="chapter" value="${esc(user.chapter || "")}"></label><label>顺序<input name="order" type="number" min="0" step="1" value="${esc(user.order ?? assistant.order ?? 0)}"></label>${[["quality","技术质量"],["beauty","画面美感"],["memory","纪念意义"]].map(([key,label]) => `<label>${label}<select name="${key}"><option value="">未评分</option>${[1,2,3].map(n => `<option value="${n}" ${Number(user[key]) === n ? "selected" : ""}>${n} 分</option>`).join("")}</select></label>`).join("")}</div>
      <label class="review-notes">我的备注<textarea name="notes" rows="3" placeholder="保留原因、剪辑想法、需要注意的问题">${esc(user.notes || "")}</textarea></label>
      ${asset.kind === "video" ? `<div class="segment-head"><strong>保留片段（秒）</strong><button class="text-button" type="button" data-add-segment>＋ 添加片段</button></div><div id="review-segments">${(user.segments || []).map(segmentHtml).join("")}</div>` : ""}
      <div class="review-save-bar"><span id="review-save-status" role="status">已保存 · 修订 ${user.revision || 0}</span><button class="button primary" type="submit">保存并下一项</button></div></form>`;
    const form = detail.querySelector("form");
    if (asset.kind === "video") {
      detail.querySelector(".review-media").insertAdjacentHTML("afterend", `<div class="proxy-controls"><label>预览源起点（秒）<input id="review-proxy-start" type="number" min="0" step="0.1" value="${Number(proxy?.source_start || 0)}"></label><button class="button" type="button" data-proxy-create>生成兼容预览（60秒）</button><button class="text-button" type="button" data-proxy-original>播放原片</button><span id="review-proxy-status" role="status"></span></div>`);
      detail.querySelector("[data-proxy-create]").onclick = async () => {
        const start = Number(root().querySelector("#review-proxy-start").value);
        if (!await save()) return;
        const workspaceId = workspace.workspace_id;
        try {
          const result = await api(`${endpoint()}/proxy/${asset.id}`, {method:"POST",body:{start,duration:60}});
          if (workspace?.workspace_id !== workspaceId) return;
          proxyJobs[asset.id] = result.job; playbackModes[asset.id] = "proxy";
          const startField = root().querySelector("#review-proxy-start"); if (startField) startField.value = String(start);
          showProxy(result.job); pollProxy(result.job);
        } catch(error) { toast(error.message); }
      };
      detail.querySelector("[data-proxy-original]").onclick = () => {
        playbackModes[asset.id] = "original";
        const video = detail.querySelector("video"); video.src = `${endpoint()}/media/${asset.id}`; video.dataset.sourceOffset = "0";
        detail.querySelector("#review-proxy-status").textContent = "正在播放原片";
      };
      detail.querySelector("video")?.addEventListener("error", () => { const node = detail.querySelector("#review-proxy-status"); if (node) node.textContent = "此编码无法直接播放，可按需生成兼容预览。"; });
      if (proxy) showProxy(proxy);
    }
    form.oninput = () => { dirty = true; detail.querySelector("#review-save-status").textContent = "有未保存修改"; };
    form.onsubmit = async event => { event.preventDefault(); if (await save(true)) await step(1); };
    detail.querySelectorAll("[data-decision]").forEach(button => button.onclick = () => save(true, {status: button.dataset.decision}));
    detail.querySelector("[data-review-prev]").onclick = () => step(-1);
    detail.querySelector("[data-review-next]").onclick = () => step(1);
    detail.querySelector("[data-add-segment]")?.addEventListener("click", () => {
      const video = detail.querySelector("video");
      const offset = Number(video?.dataset.sourceOffset || 0);
      const current = (video?.currentTime || 0) + offset;
      const visibleEnd = Number.isFinite(video?.duration) ? video.duration + offset : current + 1;
      const end = Math.min(asset.duration || Infinity, visibleEnd);
      detail.querySelector("#review-segments").insertAdjacentHTML("beforeend", segmentHtml({in: Number(current.toFixed(2)), out: Number(end.toFixed(2)), note: ""}));
      dirty = true;
    });
    detail.onclick = event => {
      if (event.target.closest("[data-remove-segment]")) { event.target.closest(".segment-row").remove(); dirty = true; }
      if (event.target.closest("[data-apply-suggestion]")) {
        const changes = {}, latest = item()?.assistant || {};
        for (const key of ["status", "chapter", "order", "segments"]) if (latest[key] != null) changes[key] = latest[key];
        save(true, changes);
      }
    };
  }
  function suggestionHtml(assistant) {
    if (!assistant.reason && !assistant.status) return '<strong>建议</strong><p>尚无建议。可先按时间组织，或运行 AI 视觉评审。</p>';
    return `<div><strong>${assistant.source === "vision-model" || assistant.source === "vision" ? "AI 视觉建议" : "编排建议"} · ${labels[assistant.status] || "待评审"}</strong><button class="text-button" type="button" data-apply-suggestion>采用此项建议</button></div><p>${esc(assistant.reason || "")}</p><small>${esc(assistant.evidence || assistant.sampling || assistant.source || "")}</small>`;
  }
  function segmentHtml(value) { return `<div class="segment-row"><label>起点<input data-segment-in type="number" min="0" step="0.01" value="${esc(value.in)}"></label><label>终点<input data-segment-out type="number" min="0" step="0.01" value="${esc(value.out)}"></label><label>说明<input data-segment-note value="${esc(value.note || "")}"></label><button class="text-button" type="button" data-remove-segment aria-label="删除片段">×</button></div>`; }
  async function save(force = false, changes = {}) {
    if (!workspace || !item()) return true;
    if (busy) return false;
    if (!dirty && !force) return true;
    const asset = item(), form = root().querySelector("#review-decision-form");
    const workspaceId = workspace.workspace_id;
    const data = Object.fromEntries(new FormData(form));
    for (const key of ["quality", "beauty", "memory"]) data[key] = data[key] === "" ? 0 : Number(data[key]);
    data.order = Number(data.order);
    if (asset.kind === "video") data.segments = [...root().querySelectorAll(".segment-row")].map(row => ({in: Number(row.querySelector("[data-segment-in]").value), out: Number(row.querySelector("[data-segment-out]").value), note: row.querySelector("[data-segment-note]").value}));
    Object.assign(data, changes, {revision: asset.user.revision});
    const previousVideo = root().querySelector("video");
    const sourceTime = previousVideo ? previousVideo.currentTime + Number(previousVideo.dataset.sourceOffset || 0) : null;
    busy = true;
    for (const control of form.elements) control.disabled = true;
    root().querySelector("#review-save-status").textContent = "正在保存…";
    try {
      const saved = await api(`${endpoint()}/items/${asset.id}`, {method: "PATCH", body: data});
      if (workspace?.workspace_id !== workspaceId) return false;
      workspace = saved;
      dirty = false;
      render();
      const video = root().querySelector("video");
      if (video && sourceTime != null) video.addEventListener("loadedmetadata", () => { video.currentTime = Math.max(0, Math.min(video.duration, sourceTime - Number(video.dataset.sourceOffset || 0))); }, {once: true});
      return true;
    } catch (error) { root().querySelector("#review-save-status").textContent = `未保存：${error.message}`; toast(error.message); return false; }
    finally { busy = false; const currentForm = root().querySelector("#review-decision-form"); if (currentForm) for (const control of currentForm.elements) control.disabled = false; }
  }
  async function select(id) { if (!await save()) return; selected = id; renderList(); renderDetail(); }
  async function step(delta) { const values = visible(), index = values.findIndex(value => value.id === selected); const next = values[index + delta]; if (next) await select(next.id); else await save(); }
  async function suggest(mode) {
    if (!await save() || activeJob && ["queued", "running", "cancelling"].includes(activeJob.status)) return;
    const workspaceId = workspace.workspace_id;
    try {
      const result = await api(`${endpoint()}/suggest`, {method: "POST", body: {revision: workspace.revision, mode, limit: 12}});
      if (workspace?.workspace_id !== workspaceId) return;
      if (result.job) { activeJob = result.job; updateJob(activeJob); poll(); }
      else { workspace = result; render(); toast(mode === "organize" ? "编排建议已保存，请逐项确认" : "已读取已有 AI 分数"); }
    } catch (error) { toast(error.message); }
  }
  function updateJob(job) {
    const node = root().querySelector("#review-ai-job");
    if (!node) return;
    node.classList.remove("hidden");
    const running = ["queued", "running", "cancelling"].includes(job.status);
    node.innerHTML = `<div><strong>${running ? "AI 正在评审" : job.status === "done" ? "AI 评审完成" : job.status === "cancelled" ? "评审已停止" : "AI 评审未完成"}</strong><span>${job.completed || 0} / ${job.total || 0} · ${esc(job.current || job.error || "")}</span>${running ? '<button class="text-button" data-cancel-vision>停止</button>' : ""}</div><progress max="${job.total || 1}" value="${job.completed || 0}"></progress>${job.errors?.length ? `<small>${esc(job.errors.map(value => typeof value === "string" ? value : value.error || value.message || JSON.stringify(value)).join("；"))}</small>` : ""}`;
    node.querySelector("[data-cancel-vision]")?.addEventListener("click", async () => { try { const result = await api(`${endpoint()}/jobs/${job.job_id}/cancel`, {method: "POST"}); activeJob = result.job; updateJob(activeJob); } catch (error) { toast(error.message); } });
  }
  async function poll() {
    clearTimeout(timer);
    if (!workspace || !activeJob) return;
    const workspaceId = workspace.workspace_id, jobId = activeJob.job_id;
    try {
      const result = await api(`${endpoint()}/jobs/${activeJob.job_id}`);
      if (workspace?.workspace_id !== workspaceId || activeJob?.job_id !== jobId) return;
      activeJob = result.job; updateJob(activeJob);
      if (["queued", "running", "cancelling"].includes(activeJob.status)) timer = setTimeout(poll, 1500);
      else {
        const updated = await api(endpoint());
        if (workspace?.workspace_id !== workspaceId || activeJob?.job_id !== jobId) return;
        if (!dirty) { workspace = updated; if (location.hash === `#toolbox/review/${workspaceId}`) render(); }
        else { workspace.revision = updated.revision; for (const asset of workspace.items) asset.assistant = updated.items.find(value => value.id === asset.id)?.assistant || asset.assistant; const box = root().querySelector("#review-suggestion"); if (box) box.innerHTML = suggestionHtml(item()?.assistant || {}); }
      }
    } catch (error) { toast(error.message); }
  }
  async function resume() {
    const workspaceId = workspace.workspace_id;
    const result = await api(`${endpoint()}/jobs/latest`);
    if (workspace?.workspace_id !== workspaceId) return;
    activeJob = result.job;
    if (activeJob) { updateJob(activeJob); if (["queued", "running", "cancelling"].includes(activeJob.status)) poll(); }
    const proxyResult = await api(`${endpoint()}/proxy-jobs/latest`);
    if (workspace?.workspace_id !== workspaceId) return;
    if (proxyResult.job) { proxyJobs[proxyResult.job.asset_id] = proxyResult.job; showProxy(proxyResult.job); if (["queued", "running", "cancelling"].includes(proxyResult.job.status)) pollProxy(proxyResult.job); }
  }
  function showProxy(job) {
    if (item()?.id !== job.asset_id || !root().querySelector("#review-proxy-status")) return;
    const node = root().querySelector("#review-proxy-status"), video = root().querySelector("video");
    const running = ["queued","running","cancelling"].includes(job.status);
    root().querySelector("[data-proxy-create]").disabled = running;
    if (job.status === "done" || job.status === "completed") {
      if (playbackModes[job.asset_id] === "original") { node.textContent = "正在播放原片 · 已有兼容预览"; return; }
      video.src = job.preview_url; video.dataset.sourceOffset = String(job.source_start || 0);
      node.textContent = `兼容预览 · 原片 ${Number(job.source_start).toFixed(1)}–${(Number(job.source_start)+Number(job.duration)).toFixed(1)} 秒`;
    } else if (running) {
      node.innerHTML = `生成预览 ${Math.round(job.progress || 0)}% <button class="text-button" type="button" data-proxy-cancel>停止</button>`;
      node.querySelector("button").onclick = async () => { try { const result = await api(`${endpoint()}/proxy-jobs/${job.job_id}/cancel`,{method:"POST"}); proxyJobs[job.asset_id] = result.job; showProxy(result.job); } catch(error) {toast(error.message);} };
    } else node.textContent = job.error || "预览已停止";
  }
  async function pollProxy(job) {
    clearTimeout(proxyTimer);
    const workspaceId = workspace?.workspace_id;
    if (workspaceId !== job.workspace_id) return;
    try {
      const result = await api(`${endpoint()}/proxy-jobs/${job.job_id}`);
      if (workspace?.workspace_id !== workspaceId) return;
      proxyJobs[job.asset_id] = result.job; showProxy(result.job);
      if (["queued","running","cancelling"].includes(result.job.status)) proxyTimer = setTimeout(() => pollProxy(result.job),1200);
    } catch(error) { toast(error.message); }
  }
  document.addEventListener("keydown", event => {
    if (!location.hash.startsWith("#toolbox/review") || !workspace || document.querySelector("#storyboard-dialog")?.open || event.ctrlKey || event.altKey || event.metaKey || event.repeat || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    const status = {"1":"keep", "2":"maybe", "3":"reject", "0":"unreviewed"}[event.key];
    if (status) { event.preventDefault(); save(true, {status}); }
    else if (event.key === "ArrowRight" || event.key === "ArrowLeft") { event.preventDefault(); step(event.key === "ArrowRight" ? 1 : -1); }
  });
  window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
  return {open, hasPending: () => dirty || busy, savePending: () => save()};
})();
