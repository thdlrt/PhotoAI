globalThis.PhotoSelectionOutput = (() => {
  let run = null, summary = null, timer = null, copyJob = null, directory = "";
  const root = () => document.querySelector("#selection-output-root");
  const esc = value => escapeHtml(String(value ?? ""));
  const base = () => `/api/runs/${run.run_id}/selection-export`;
  function render(currentRun, data) {
    if (!data) return;
    if (run?.run_id !== currentRun.run_id) { directory = ""; copyJob = null; clearTimeout(timer); }
    run = currentRun; summary = data; copyJob = copyJob || data.copy_job;
    root().innerHTML = `<div class="selection-export-grid"><section class="panel tool-settings"><h2>选择清单</h2><p class="subtle">包含文件路径、人工星级、AI 星级和分组。</p><div class="tool-actions"><a class="button primary link-button" href="${base()}/download?format=json" download>下载 JSON</a><a class="button link-button" href="${base()}/download?format=csv" download>下载 CSV</a></div></section>
      <section class="panel tool-settings"><h2>复制选中原片</h2><form id="selection-copy-form"><label>新输出目录<input name="output_dir" required placeholder="填写尚不存在的新文件夹" value="${esc(directory)}"></label><label class="check"><input name="include_xmp" type="checkbox" checked>带上已有 XMP</label><button class="button" type="submit" ${!summary.count || summary.missing_count ? "disabled" : ""}>复制 ${summary.count} 张原片</button></form><p class="subtle">保留子目录与文件内容，输出到新文件夹。</p></section>
      <section class="panel tool-settings"><h2>写入选片星级</h2><p>${summary.xmp_create_count} 张 RAW 可创建星级 XMP</p><p class="subtle">已有 XMP ${summary.xmp_existing_count} 个将跳过。写入前需关闭 Lightroom，可在写入记录中撤销。</p><button id="selection-xmp-write" class="button" type="button" ${!summary.xmp_create_count || state.activeJob ? "disabled" : ""}>创建星级 XMP</button></section></div><div id="selection-copy-job" class="tool-job hidden" role="status"></div>
      ${summary.missing_count ? `<p class="tool-notice">${summary.missing_count} 个源文件当前不可用，请恢复位置后再复制。</p>` : ""}<details class="selection-manifest"><summary>查看 ${summary.count} 张选择记录</summary><div class="manifest-table"><table><thead><tr><th>文件</th><th>星级</th><th>来源</th></tr></thead><tbody>${summary.items.map(value => `<tr><td>${esc(value.relative_path)}</td><td>${value.rating}</td><td>${value.manual_override ? "人工确认" : "AI 建议"}</td></tr>`).join("")}</tbody></table></div></details>`;
    root().querySelector("#selection-copy-form").onsubmit = async event => {
      event.preventDefault(); const form = event.currentTarget; directory = form.elements.output_dir.value.trim();
      const button = form.querySelector("button"); button.disabled = true;
      const runId = run.run_id;
      try { const created = await api(`${base()}/copy`, {method:"POST", body:{base_revision:summary.review_revision, output_dir:directory, include_xmp:form.elements.include_xmp.checked, min_rating:3}}); if (run?.run_id !== runId) return; copyJob = created; showCopy(); poll(); }
      catch (error) { toast(error.message); button.disabled = false; }
    };
    root().querySelector("#selection-xmp-write").onclick = async event => {
      event.currentTarget.disabled = true;
      try { const result = await api(`${base()}/xmp`, {method:"POST", body:{base_revision:summary.review_revision,min_rating:3}}); activateJob(result.job); pollJobs(); }
      catch (error) { toast(error.message); event.currentTarget.disabled = false; }
    };
    if (copyJob) { showCopy(); if (["queued", "running", "cancelling"].includes(copyJob.status)) poll(); }
  }
  function showCopy() {
    const node = root().querySelector("#selection-copy-job"); if (!node || !copyJob) return;
    node.classList.remove("hidden");
    const running = ["queued","running","cancelling"].includes(copyJob.status);
    node.innerHTML = `<div><strong>${running ? "正在复制原片" : copyJob.status === "completed" ? "原片复制完成" : "复制已停止"}</strong><span>${copyJob.completed_files || 0} / ${copyJob.file_count || 0} · ${esc(copyJob.message || copyJob.current_file || "")}</span>${running ? '<button class="text-button" data-copy-cancel>停止</button>' : ""}</div><progress max="100" value="${copyJob.percent || 0}"></progress><small class="path-summary">${esc(copyJob.output_dir)}</small>`;
    node.querySelector("[data-copy-cancel]")?.addEventListener("click", async () => { try { copyJob = await api(`/api/selection-copies/${copyJob.id}/cancel`,{method:"POST"}); showCopy(); } catch(error) { toast(error.message); } });
  }
  async function poll() {
    clearTimeout(timer);
    const runId = run.run_id, jobId = copyJob.id;
    try {
      const updated = await api(`/api/selection-copies/${jobId}`);
      if (run?.run_id !== runId || copyJob?.id !== jobId) return;
      copyJob = updated; showCopy();
      if (["queued","running","cancelling"].includes(copyJob.status)) timer = setTimeout(poll,1200);
      else { const button = root().querySelector('#selection-copy-form button[type="submit"]'); if (button) button.disabled = !summary.count || Boolean(summary.missing_count); }
    }
    catch (error) { toast(error.message); }
  }
  return {render};
})();
