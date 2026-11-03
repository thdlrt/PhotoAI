globalThis.PhotoTimelapse = (() => {
  const base = "/api/toolbox/timelapse";
  let session = null, capabilities = {}, currentJob = null, timer = null, frameIndex = 0;
  let keyframeSession = null, keyframeValues = {};
  const processPreferences = {}, renderPreferences = {};
  const root = () => document.querySelector("#timelapse-root");
  const esc = value => escapeHtml(String(value ?? ""));
  async function open() {
    capabilities = await api(`${base}/capabilities`);
    const id = location.hash.split("/")[2];
    if (id && session?.id !== id) session = await api(`${base}/sessions/${encodeURIComponent(id)}`);
    if (session) { await restorePreferences(); render(); await resume(); return; }
    await home();
  }
  async function home() {
    const sessions = await api(`${base}/sessions`);
    root().innerHTML = `<form id="timelapse-create-form" class="panel toolbox-create-form"><label class="tool-wide">图像序列目录<span class="path-picker"><input id="timelapse-input-root" name="input_root" required autocomplete="off" placeholder="选择同一机位的照片序列"><button class="button desktop-folder-picker hidden" type="button" data-folder-target="timelapse-input-root">浏览</button></span></label><p class="subtle">RAW 可输出 Lightroom XMP；JPEG / TIFF / PNG 等成片序列可直接合成 MP4。</p><button class="button primary" type="submit">扫描序列</button></form><div id="timelapse-job" class="tool-job hidden" role="status"></div><div class="section-title"><h2>最近序列</h2></div><div class="project-list">${(Array.isArray(sessions) ? sessions : sessions.sessions || []).map(value => `<button class="workspace-row" data-session="${esc(value.id)}"><strong>${esc(value.name)}</strong><small>${value.frame_count || 0} 帧 · ${esc(value.input_root)}</small></button>`).join("") || '<p class="empty">还没有延时序列</p>'}</div>`;
    bindDesktopFolderPickers();
    root().querySelector("form").onsubmit = async event => { event.preventDefault(); await start("/scan", Object.fromEntries(new FormData(event.currentTarget))); };
    root().querySelectorAll("[data-session]").forEach(button => button.onclick = async () => { try { session = await api(`${base}/sessions/${button.dataset.session}`); frameIndex = 0; await restorePreferences(); render(); await resume(); } catch (error) { toast(error.message); } });
    const jobs = await api(`${base}/jobs`);
    const pending = (Array.isArray(jobs) ? jobs : jobs.jobs || []).find(value => ["queued", "running"].includes(value.status));
    if (pending) { currentJob = pending; showJob(); poll(); }
  }
  function render() {
    if (keyframeSession !== session.id) {
      keyframeSession = session.id;
      keyframeValues = {};
      for (const index of [0, session.frame_count - 1]) if (index >= 0) keyframeValues[index] = Number(session.frames[index]?.original_exposure || 0);
      if (processPreferences[session.id]?.keyframe_mode === "manual") keyframeValues = {...processPreferences[session.id].keyframe_exposures};
    }
    history.replaceState(null, "", `#toolbox/timelapse/${session.id}`);
    const running = currentJob && ["queued", "running"].includes(currentJob.status);
    const process = session.last_process, video = session.last_render;
    root().innerHTML = `<div class="workspace-heading"><div><h2>${esc(session.name)}</h2><p class="subtle">${session.frame_count} 帧 · ${session.raw_count ? `RAW ${session.raw_count} 张` : "成片序列"} · ${esc((session.dimensions || []).map(size => size.join("×")).join(" / "))}</p></div><button class="button" data-timelapse-home>所有序列</button></div>
      <div id="timelapse-job" class="tool-job hidden" role="status"></div>
      ${session.warnings?.length ? `<div class="tool-notice">${session.warnings.map(esc).join("<br>")}</div>` : ""}
      <div class="timelapse-layout"><section class="panel timelapse-preview"><div class="timelapse-image"><img id="timelapse-frame" src="${base}/sessions/${session.id}/frames/${frameIndex}" alt="序列原始预览"></div><div class="timelapse-scrubber"><input id="timelapse-scrub" type="range" min="0" max="${Math.max(0, session.frame_count - 1)}" value="${frameIndex}" aria-label="预览帧"><span id="timelapse-frame-label"></span></div><div class="curve-header"><strong>亮度变化</strong><span><i class="legend-original"></i>原始 <i class="legend-corrected"></i>处理后</span></div><div id="timelapse-curve"></div></section>
      <div class="timelapse-settings"><form id="timelapse-process-form" class="panel tool-settings"><h3>1 · 过渡与去闪</h3><div class="review-fields"><label>关键帧<select name="keyframe_mode"><option value="auto">自动定位</option><option value="even">等距</option><option value="existing">沿用 XMP 标记</option></select></label><label>数量<input name="keyframe_count" type="number" min="2" max="1000" value="${Math.max(2, Math.min(8, session.frame_count))}"></label></div><label class="check"><input name="holy_grail" type="checkbox" checked>平滑曝光过渡（圣杯）</label><label class="check"><input name="deflicker" type="checkbox" checked>去闪烁</label><label>去闪强度<input name="strength" type="range" min="0" max="1" step="0.05" value="1"></label><label>平滑窗口<input name="smooth_window" type="number" min="3" max="501" step="2" value="11"></label><button class="button primary" type="submit" ${running ? "disabled" : ""}>分析并生成 XMP</button><p class="subtle">保存到独立输出目录，供 Lightroom 载入后导出序列。</p>${process ? `<a class="button link-button" href="${base}/jobs/${process.job_id}/xmp" download>下载处理后的 XMP</a><small class="path-summary">${esc(process.sidecar_dir)}</small>` : ""}</form>
      <form id="timelapse-render-form" class="panel tool-settings"><h3>2 · 合成视频</h3><div class="review-fields"><label>帧率<select name="fps"><option>25</option><option>24</option><option>30</option><option>50</option><option>60</option></select></label><label>画面尺寸<select name="resolution"><option value="1920x1080">1920 × 1080</option><option value="3840x2160">3840 × 2160</option><option value="1080x1920">1080 × 1920</option><option value="1080x1080">1080 × 1080</option></select></label><label>质量 CRF<input name="crf" type="number" min="0" max="35" value="18"></label></div><p class="subtle" id="timelapse-duration">${(session.frame_count / 25).toFixed(2)} 秒 · 等比缩放，必要时补边</p><label class="check"><input name="apply_corrections" type="checkbox" ${process ? "" : "disabled"}>在成片序列上应用本次曝光修正</label>${session.mixed_dimensions ? '<label class="check"><input name="allow_mixed_dimensions" type="checkbox">允许混合尺寸并补边</label>' : ""}<button class="button primary" type="submit" ${running || !session.renderable || capabilities.can_render === false ? "disabled" : ""}>合成 MP4</button>${!session.renderable ? '<p class="tool-notice">RAW 序列请先在 Lightroom 导出 JPEG / TIFF，再作为新序列合成。</p>' : capabilities.can_render === false ? '<p class="tool-notice">未找到 FFmpeg，暂不能合成视频。请补齐工具依赖后重开此页。</p>' : ""}</form></div></div>
      ${video ? `<section class="panel timelapse-result"><div class="workspace-heading"><div><h3>合成结果</h3><p class="subtle">${video.frames} 帧 · ${video.width} × ${video.height} · ${Number(video.duration).toFixed(2)} 秒</p></div><a class="button link-button" href="${base}/jobs/${video.job_id}/video" download="${esc(session.name)}.mp4">保存视频</a></div><video controls preload="metadata" src="${base}/jobs/${video.job_id}/video"></video><small class="path-summary">${esc(video.video_path)}</small></section>` : ""}`;
    root().querySelector("[data-timelapse-home]").onclick = async () => { session = null; frameIndex = 0; history.replaceState(null, "", "#toolbox/timelapse"); await home(); };
    root().querySelector("#timelapse-scrub").oninput = event => { frameIndex = Number(event.target.value); updateFrame(); };
    const processForm = root().querySelector("#timelapse-process-form");
    processForm.elements.keyframe_mode.insertAdjacentHTML("beforeend", '<option value="manual">手动关键帧</option>');
    processForm.elements.keyframe_count.closest("label").id = "timelapse-keyframe-count";
    processForm.querySelector(".review-fields").insertAdjacentHTML("afterend", '<label id="timelapse-sensitivity">自动定位灵敏度<input name="sensitivity" type="number" min="0.1" max="10" step="0.1" value="1"></label><div id="timelapse-manual-keyframes" class="hidden"><button type="button" class="text-button" data-keyframe-add>＋ 将当前预览帧设为关键帧</button><div id="timelapse-keyframe-values"></div></div><label class="check"><input name="transition" type="checkbox" checked>在关键帧之间插值曝光</label>');
    const updateMode = () => {
      root().querySelector("#timelapse-keyframe-count").classList.toggle("hidden", processForm.elements.keyframe_mode.value !== "even");
      root().querySelector("#timelapse-sensitivity").classList.toggle("hidden", processForm.elements.keyframe_mode.value !== "auto");
      root().querySelector("#timelapse-manual-keyframes").classList.toggle("hidden", processForm.elements.keyframe_mode.value !== "manual");
      renderKeyframes();
    };
    processForm.elements.keyframe_mode.onchange = updateMode;
    root().querySelector("[data-keyframe-add]").onclick = () => { keyframeValues[frameIndex] = Number(session.frames[frameIndex]?.original_exposure || 0); renderKeyframes(); };
    applyPreferences(processForm, processPreferences[session.id]);
    updateMode();
    root().querySelector("#timelapse-process-form").onsubmit = async event => {
      event.preventDefault(); const form = event.currentTarget;
      const manual = form.elements.keyframe_mode.value === "manual";
      if (manual) keyframeValues = Object.fromEntries([...form.querySelectorAll("[data-keyframe-ev]")].map(input => [input.dataset.keyframeEv, Number(input.value)]));
      await start(`/sessions/${session.id}/process`, {keyframe_mode: form.elements.keyframe_mode.value, keyframe_count: Number(form.elements.keyframe_count.value), keyframe_indices: manual ? Object.keys(keyframeValues).map(Number) : [], keyframe_exposures: manual ? keyframeValues : {}, sensitivity: Number(form.elements.sensitivity.value), transition: form.elements.transition.checked, holy_grail: form.elements.holy_grail.checked, deflicker: form.elements.deflicker.checked, strength: Number(form.elements.strength.value), smooth_window: Number(form.elements.smooth_window.value)});
    };
    const renderForm = root().querySelector("#timelapse-render-form");
    applyPreferences(renderForm, renderPreferences[session.id]);
    const previousRender = renderPreferences[session.id];
    if (previousRender) renderForm.elements.resolution.value = `${previousRender.width}x${previousRender.height}`;
    renderForm.elements.fps.onchange = () => root().querySelector("#timelapse-duration").textContent = `${(session.frame_count / Number(renderForm.elements.fps.value)).toFixed(2)} 秒 · 等比缩放，必要时补边`;
    renderForm.elements.fps.onchange();
    renderForm.onsubmit = async event => { event.preventDefault(); const form = event.currentTarget; const [width,height] = form.elements.resolution.value.split("x").map(Number); await start(`/sessions/${session.id}/render`, {fps: Number(form.elements.fps.value), width, height, crf: Number(form.elements.crf.value), apply_corrections: form.elements.apply_corrections.checked, allow_mixed_dimensions: form.elements.allow_mixed_dimensions?.checked || false}); };
    updateFrame(); drawCurve(); showJob();
  }
  function renderKeyframes() {
    const list = root().querySelector("#timelapse-keyframe-values");
    list.innerHTML = Object.keys(keyframeValues).map(Number).sort((a,b) => a-b).map(index => `<div class="keyframe-row"><span>帧 ${index + 1}</span><label>曝光 EV<input data-keyframe-ev="${index}" type="number" min="-5" max="5" step="0.1" value="${keyframeValues[index]}"></label><button class="text-button" type="button" data-keyframe-remove="${index}" aria-label="移除关键帧 ${index + 1}">×</button></div>`).join("");
    list.querySelectorAll("[data-keyframe-ev]").forEach(input => input.oninput = () => { keyframeValues[Number(input.dataset.keyframeEv)] = Number(input.value); });
    list.querySelectorAll("[data-keyframe-remove]").forEach(button => button.onclick = () => { delete keyframeValues[Number(button.dataset.keyframeRemove)]; renderKeyframes(); });
  }
  function updateFrame() {
    const frame = session.frames[frameIndex];
    root().querySelector("#timelapse-frame").src = `${base}/sessions/${session.id}/frames/${frameIndex}`;
    root().querySelector("#timelapse-frame-label").textContent = `${frameIndex + 1} / ${session.frame_count} · ${frame?.name || ""}`;
  }
  function drawCurve() {
    const rows = (session.timeline || []).map((row,index) => {
      if (Number.isFinite(row.luminance) && row.luminance > 0) {
        const measured = Math.log2(row.luminance);
        return {...row, ev:measured, corrected:measured + Number(row.exposure || 0) - Number(session.frames[index]?.original_exposure || 0)};
      }
      return row;
    });
    const values = rows.flatMap(row => [row.ev, row.corrected]).filter(value => Number.isFinite(value));
    if (!values.length) { root().querySelector("#timelapse-curve").innerHTML = '<p class="empty">分析后显示曝光曲线</p>'; return; }
    const low = Math.min(...values), high = Math.max(...values), range = Math.max(0.1, high - low);
    const line = key => rows.map((row, index) => Number.isFinite(row[key]) ? `${(12 + 676 * index / Math.max(1, rows.length - 1)).toFixed(1)},${(125 - (row[key] - low) / range * 110).toFixed(1)}` : null).filter(Boolean).join(" ");
    root().querySelector("#timelapse-curve").innerHTML = `<svg viewBox="0 0 700 145" role="img" aria-label="原始及处理后亮度曲线"><line x1="12" x2="688" y1="125" y2="125" stroke="#363b3b"/><polyline fill="none" stroke="#75888d" stroke-width="2" points="${line("ev")}"/><polyline fill="none" stroke="#d6a45e" stroke-width="2" points="${line("corrected")}"/></svg>`;
  }
  async function start(path, body) {
    if (currentJob && ["queued", "running"].includes(currentJob.status)) return;
    if (session && /\/(process|render)$/.test(path)) body.base_revision = session.revision ?? 0;
    try {
      currentJob = await api(`${base}${path}`, {method: "POST", body});
      if (session && path.endsWith("/process")) processPreferences[session.id] = structuredClone(body);
      if (session && path.endsWith("/render")) renderPreferences[session.id] = structuredClone(body);
      showJob(); root().querySelectorAll('button[type="submit"]').forEach(button => button.disabled = true); poll();
    }
    catch (error) { toast(error.message); }
  }
  function showJob() {
    const node = root().querySelector("#timelapse-job");
    if (!node || !currentJob) return;
    node.classList.remove("hidden");
    const running = ["queued", "running"].includes(currentJob.status);
    const labels = {scan:"扫描序列", process:"分析与去闪", render:"视频合成"};
    node.innerHTML = `<div><strong>${labels[currentJob.action] || "延时处理"} · ${{queued:"等待",running:"进行中",completed:"完成",failed:"失败",cancelled:"已停止",interrupted:"已中断"}[currentJob.status] || currentJob.status}</strong><span>${esc(currentJob.message || currentJob.phase || currentJob.error || "")}</span>${running ? '<button class="text-button" data-timelapse-cancel>停止</button>' : ""}</div><progress max="1" value="${Number(currentJob.progress || 0)}"></progress>`;
    node.querySelector("[data-timelapse-cancel]")?.addEventListener("click", async () => { try { currentJob = await api(`${base}/jobs/${currentJob.id}/cancel`, {method:"POST"}); showJob(); } catch (error) { toast(error.message); } });
  }
  async function poll() {
    clearTimeout(timer);
    const jobId = currentJob.id, sessionId = session?.id;
    try {
      const updated = await api(`${base}/jobs/${jobId}`);
      if (currentJob?.id !== jobId) return;
      currentJob = updated; showJob();
      if (["queued", "running"].includes(currentJob.status)) { timer = setTimeout(poll, 1200); return; }
      if (currentJob.status === "completed" && currentJob.result?.session_id) {
        if (session?.id !== sessionId || (session && currentJob.action !== "scan" && currentJob.result.session_id !== session.id)) return;
        const completedSession = await api(`${base}/sessions/${currentJob.result.session_id}`);
        if (session?.id !== sessionId || currentJob?.id !== jobId) return;
        session = completedSession; frameIndex = Math.min(frameIndex, session.frame_count - 1);
        if (location.hash.startsWith("#toolbox/timelapse")) render();
      } else if (session && location.hash.startsWith("#toolbox/timelapse")) render();
      else root().querySelectorAll('button[type="submit"]').forEach(button => button.disabled = false);
    } catch (error) { toast(error.message); }
  }
  async function resume() {
    const sessionId = session?.id;
    const jobs = await api(`${base}/jobs`);
    if (session?.id !== sessionId) return;
    const pending = (Array.isArray(jobs) ? jobs : jobs.jobs || []).find(value => value.session_id === sessionId && ["queued", "running"].includes(value.status));
    if (pending) { currentJob = pending; showJob(); poll(); }
  }
  function applyPreferences(form, values) {
    for (const [key,value] of Object.entries(values || {})) {
      const field = form.elements.namedItem(key);
      if (!field) continue;
      if (field.type === "checkbox") field.checked = Boolean(value); else field.value = String(value);
    }
  }
  async function restorePreferences() {
    const opened = session;
    const requests = [["last_process",processPreferences],["last_render",renderPreferences]].filter(([key,cache]) => !cache[opened.id] && opened[key]?.job_id);
    await Promise.all(requests.map(async ([key,cache]) => { const job = await api(`${base}/jobs/${opened[key].job_id}`); cache[opened.id] = job.params || {}; }));
  }
  return {open};
})();
