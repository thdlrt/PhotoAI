globalThis.PhotoStoryboard = (() => {
  let workspace = null, proposal = null, dirty = false, busy = false, job = null, timer = null;
  let historical = false, historyRows = [];
  let viewEpoch = 0, sessionEpoch = 0, activeView = null;
  const dialog = () => document.querySelector("#storyboard-dialog");
  const root = () => document.querySelector("#storyboard-root");
  const esc = value => escapeHtml(String(value ?? ""));
  const base = () => `/api/review-workspaces/${workspace.workspace_id}`;
  const exportUrl = format => `${base()}/storyboards/${proposal.proposal_id}/export?format=${format}&revision=${proposal.revision}`;
  const labels = {draft:"待确认",approved:"已批准",rejected:"已退回"};
  const running = value => value && ["queued", "running", "cancelling"].includes(value.status);
  function lockViewControls() {
    if (!activeView) return;
    root().querySelectorAll("input,textarea,select,button").forEach(control => {
      if (!activeView.disabled.has(control)) activeView.disabled.set(control, control.disabled);
      control.disabled = true;
    });
  }
  async function changeView(action) {
    if (busy) return false;
    const ticket = {epoch: ++viewEpoch, disabled: new Map()};
    activeView = ticket; busy = true; lockViewControls();
    try { return await action(ticket); }
    catch (error) { if (ticket.epoch === viewEpoch) toast(error.message); return false; }
    finally {
      if (activeView === ticket) { busy = false; activeView = null; }
      for (const [control, disabled] of ticket.disabled) if (control.isConnected) control.disabled = disabled;
    }
  }
  const currentView = ticket => ticket.epoch === viewEpoch && activeView === ticket;
  async function open(current) {
    if (busy) return false;
    if (dirty && !await save()) return;
    return changeView(async ticket => {
      const workspaceId = current.workspace_id, session = ++sessionEpoch;
      clearTimeout(timer);
      const fresh = await api(`/api/review-workspaces/${workspaceId}`);
      if (!currentView(ticket) || session !== sessionEpoch) return false;
      workspace = fresh;
      proposal = null; job = null; dirty = false; historical = false; historyRows = [];
      if (!dialog().open) dialog().showModal();
      if (!await home(ticket)) return false;
      const latest = await api(`/api/review-workspaces/${workspaceId}/storyboard-jobs/latest`);
      if (!currentView(ticket) || session !== sessionEpoch || workspace?.workspace_id !== workspaceId) return false;
      job = latest.job;
      if (job) { showJob(); if (running(job)) poll(); }
      return true;
    });
  }
  async function home(owner = null) {
    if (!owner) {
      if (busy || dirty && !await save()) return false;
      return changeView(ticket => home(ticket));
    }
    const workspaceId = workspace.workspace_id;
    const result = await api(`/api/review-workspaces/${workspaceId}/storyboards`);
    if (!currentView(owner) || workspace?.workspace_id !== workspaceId) return false;
    proposal = null; dirty = false; historical = false; historyRows = [];
    renderHome(result);
    return true;
  }
  function renderHome(result) {
    root().innerHTML = `<div class="workspace-heading"><div><h2>${esc(workspace.name)} · 剪辑草案</h2><p class="subtle">必用、弃用、备注与已选片段作为编排约束。</p></div><button class="text-button" data-story-close>关闭</button></div>
      <form id="story-create-form" class="panel tool-settings"><div class="review-fields"><label>草案标题<input name="title" value="${esc(workspace.name)}"></label><label>生成方式<select name="mode"><option value="rules">按当前选择建立初稿</option><option value="model">让已配置的 AI 编排</option></select></label></div><label>创作要求<textarea name="brief" rows="3" placeholder="例如：按地点分章，开场简短，先视频后照片，保留重要人物和原声片段。"></textarea></label><label class="check"><input name="include_unreviewed" type="checkbox">允许未审素材进入待确认草案</label><div class="tool-actions"><button class="button primary" type="submit">生成草案</button><button class="button" type="button" data-story-import>导入 agent 提案</button><input class="hidden" type="file" id="story-import-file" accept=".json,application/json"></div><p class="subtle">AI 依据已有评价和备注编排；未分析的画面会标注缺少依据。</p></form>
      <div id="story-job" class="tool-job hidden" role="status"></div><div class="section-title"><h3>草案与版本</h3></div><div class="project-list">${result.proposals.map(value => `<button class="workspace-row" data-proposal="${esc(value.proposal_id)}"><strong>${esc(value.title)}</strong><small>${labels[value.status] || value.status} · 修订 ${value.revision}${value.stale ? " · 素材选择已更新" : ""}</small></button>`).join("") || '<p class="empty">还没有草案</p>'}</div>`;
    root().querySelector("[data-story-close]").onclick = close;
    root().querySelectorAll("[data-proposal]").forEach(button => button.onclick = () => load(button.dataset.proposal));
    root().querySelector("#story-create-form").onsubmit = async event => {
      event.preventDefault(); if (busy) return;
      const form = event.currentTarget, body = {workspace_revision:workspace.revision, mode:form.elements.mode.value, title:form.elements.title.value, brief:form.elements.brief.value, include_unreviewed:form.elements.include_unreviewed.checked};
      const workspaceId = workspace.workspace_id;
      await changeView(async ticket => {
        const result = await api(`/api/review-workspaces/${workspaceId}/storyboards`, {method:"POST", body});
        if (!currentView(ticket) || workspace?.workspace_id !== workspaceId) return false;
        if (result.job) { job=result.job; showJob(); poll(); }
        else { proposal=result.proposal; historical=false; historyRows=[]; dirty=false; render(); }
        return true;
      });
    };
    root().querySelector("[data-story-import]").onclick = () => root().querySelector("#story-import-file").click();
    root().querySelector("#story-import-file").onchange = async event => {
      const file = event.target.files[0]; if (!file || busy) return;
      const workspaceId = workspace.workspace_id, workspaceRevision = workspace.revision;
      const includeUnreviewed = root().querySelector('[name="include_unreviewed"]').checked;
      await changeView(async ticket => {
        if(file.size > 2*1024*1024) throw new Error("提案文件不得超过2 MiB");
        const value=JSON.parse(await file.text()), data=value?.proposal || value;
        if (!currentView(ticket) || workspace?.workspace_id !== workspaceId) return false;
        const result=await api(`/api/review-workspaces/${workspaceId}/storyboards`,{method:"POST",body:{workspace_revision:workspaceRevision, mode:"import",title:data.title||workspace.name,brief:data.brief||"",chapters:data.chapters,evidence:data.evidence||"由外部 agent 导入",include_unreviewed:includeUnreviewed}});
        if (!currentView(ticket) || workspace?.workspace_id !== workspaceId) return false;
        proposal=result.proposal; historical=false; historyRows=[]; dirty=false; render(); return true;
      });
    };
    showJob(); lockViewControls();
  }
  async function load(id, revision = null) {
    if (busy) return false;
    if (dirty && !await save()) return;
    return changeView(async ticket => {
      const workspaceId = workspace.workspace_id;
      const path = `/api/review-workspaces/${workspaceId}/storyboards/${id}`;
      const [result,history]=await Promise.all([api(`${path}${revision == null ? "" : `?revision=${revision}`}`),api(`${path}/history`)]);
      if (!currentView(ticket) || workspace?.workspace_id !== workspaceId) return false;
      proposal=result.proposal;historyRows=history.revisions;dirty=false;
      historical=proposal.is_latest === false || proposal.revision<Math.max(...historyRows.map(value=>typeof value==="number"?value:value.revision),proposal.revision);
      render(); return true;
    });
  }
  function render() {
    const count=proposal.chapters.reduce((sum,value)=>sum+value.clips.length,0);
    root().innerHTML=`<div class="workspace-heading"><div><h2>${esc(proposal.title)}</h2><p class="subtle">${proposal.chapters.length} 章 · ${count} 个片段 · ${labels[proposal.status]} · 修订 ${proposal.revision} · ${{rules:"规则初稿","configured-model":"AI 编排","external-agent":"Agent 提案"}[proposal.source]||esc(proposal.source)}</p></div><div class="tool-actions"><button class="button" data-story-home>全部草案</button><button class="text-button" data-story-close>关闭</button></div></div>
      ${proposal.stale ? '<div class="tool-notice">素材选择已更新。请生成新草案，或重读最新选择后重新整理；此旧版本不能直接批准。</div>' : ""}
      ${proposal.warnings?.length?`<div class="tool-notice">${proposal.warnings.map(esc).join("<br>")}</div>`:""}
      <div id="story-job" class="tool-job hidden" role="status"></div>
      <form id="story-edit-form"><div class="review-fields"><label>标题<input name="title" value="${esc(proposal.title)}"></label><label>历史版本<select id="story-history"><option value="${proposal.revision}">修订 ${proposal.revision}</option></select></label></div><label>创作要求<textarea name="brief" rows="2">${esc(proposal.brief)}</textarea></label>
      <div id="story-chapters">${proposal.chapters.map((chapter,ci)=>`<section class="panel story-chapter" data-chapter="${ci}"><div class="story-chapter-head"><span>${String(ci+1).padStart(2,"0")}</span><input data-chapter-title value="${esc(chapter.title)}" aria-label="章节 ${ci+1} 标题"><button class="text-button" type="button" data-chapter-up="${ci}" ${ci===0?"disabled":""}>上移章节</button><button class="text-button" type="button" data-chapter-down="${ci}" ${ci===proposal.chapters.length-1?"disabled":""}>下移章节</button></div><label>章节说明<input data-chapter-summary value="${esc(chapter.summary||"")}"></label><div class="story-clips">${chapter.clips.map((clip,ii)=>clipHtml(clip,ci,ii)).join("")}</div></section>`).join("")}</div>
      <div class="story-footer"><span id="story-save-status" role="status">已保存</span><div class="tool-actions"><button class="button" type="submit">保存新修订</button><button class="button danger-quiet" type="button" data-story-reject>退回</button><button class="button primary" type="button" data-story-approve ${proposal.stale?"disabled":""}>批准此草案</button><a class="button link-button" data-story-download href="${exportUrl("json")}" download>导出 JSON</a><a class="button link-button" data-story-download href="${exportUrl("csv")}" download>导出 CSV</a></div></div></form>
      <p class="subtle story-evidence">${esc(proposal.evidence||"")}${proposal.model?` · ${esc(proposal.model)}`:""}</p>`;
    const form=root().querySelector("form");
    const historySelector=root().querySelector("#story-history");
    if(historyRows.length)historySelector.innerHTML=historyRows.map(value=>{const rev=typeof value==="number"?value:value.revision;return `<option value="${rev}" ${rev===proposal.revision?"selected":""}>修订 ${rev}</option>`;}).join("");
    root().querySelector(".story-footer .tool-actions").insertAdjacentHTML("afterbegin",'<button class="button" type="button" data-story-local>导出本地草案</button><button class="button" type="button" data-story-reload>暂存修改并读取最新版</button>');
    root().querySelector("[data-story-local]").onclick=()=>{if(busy)return;collect();downloadLocal();};
    root().querySelector("[data-story-reload]").onclick=reloadLatest;
    form.oninput=onEditorInput;
    form.onsubmit=event=>{event.preventDefault();save();};
    root().querySelector("[data-story-close]").onclick=close;
    root().querySelector("[data-story-home]").onclick=()=>home();
    root().querySelector("[data-story-approve]").onclick=()=>save("approved");
    root().querySelector("[data-story-reject]").onclick=()=>save("rejected");
    root().querySelector("#story-history").onchange=event=>load(proposal.proposal_id,Number(event.target.value)).catch(error=>toast(error.message));
    root().querySelectorAll("[data-story-download]").forEach(link=>link.onclick=async event=>{
      event.preventDefault();if(busy)return;
      const format=new URL(link.href).searchParams.get("format"), workspaceId=workspace.workspace_id, proposalId=proposal.proposal_id;
      if(!await save()||workspace?.workspace_id!==workspaceId||proposal?.proposal_id!==proposalId)return;
      const a=document.createElement("a");a.href=exportUrl(format);
      a.download="";document.body.append(a);a.click();a.remove();
    });
    root().querySelectorAll("[data-chapter-up],[data-chapter-down],[data-clip-up],[data-clip-down]").forEach(button=>button.onclick=()=>{
      if(busy||historical)return;collect();
      if(button.dataset.chapterUp!=null||button.dataset.chapterDown!=null){const from=Number(button.dataset.chapterUp??button.dataset.chapterDown),to=from+(button.dataset.chapterUp!=null?-1:1);[proposal.chapters[from],proposal.chapters[to]]=[proposal.chapters[to],proposal.chapters[from]];}
      else {const [ci,from]=(button.dataset.clipUp??button.dataset.clipDown).split(":").map(Number),to=from+(button.dataset.clipUp!=null?-1:1);const clips=proposal.chapters[ci].clips;[clips[from],clips[to]]=[clips[to],clips[from]];}
      dirty=true;render();root().querySelector("#story-save-status").textContent="有未保存修改";
    });
    if(historical){
      root().querySelector("#story-save-status").textContent="历史版本 · 只读";
      form.querySelectorAll('input,textarea,button[type="submit"],[data-story-approve],[data-story-reject],[data-chapter-up],[data-chapter-down],[data-clip-up],[data-clip-down]').forEach(control=>control.disabled=true);
    }
    showJob(); lockViewControls();
  }
  function onEditorInput(event) {
    // History selection is navigation inside the form, not a content edit.
    // Browsers emit input before change, so marking it dirty here would cause
    // load() to save a new revision before opening the selected old version.
    if(busy||historical||event.target?.id==="story-history")return;
    dirty=true;root().querySelector("#story-save-status").textContent="有未保存修改";
  }
  function clipHtml(clip,ci,ii) {
    const asset=workspace.items.find(value=>value.id===clip.asset_id), video=asset?.kind==="video";
    const locked=Boolean(asset?.user.segments?.length);
    return `<article class="story-clip" data-clip="${ii}"><img src="${base()}/preview/${clip.asset_id}" alt="" loading="lazy"><div class="story-clip-copy"><strong>${esc(asset?.name||clip.asset_id)}</strong><p>${esc(clip.reason||"")}</p><label>片段备注<input data-clip-note value="${esc(clip.notes||"")}"></label><div class="review-fields">${video?`<label>原片起点（秒）<input data-clip-in type="number" step="0.01" min="0" value="${clip.source_in??""}" ${locked?"disabled":""} placeholder="待标注"></label><label>原片终点（秒）<input data-clip-out type="number" step="0.01" min="0" value="${clip.source_out??""}" ${locked?"disabled":""} placeholder="待标注"></label>`:`<label>照片停留（秒）<input data-photo-duration type="number" step="0.1" min="0.1" max="120" value="${clip.photo_duration||4}"></label>`}</div>${locked?'<small class="subtle">沿用你的源片段；修改选段请回到审片页。</small>':""}</div><div class="story-clip-actions"><button class="button" type="button" data-clip-up="${ci}:${ii}" ${ii===0?"disabled":""} aria-label="上移 ${esc(asset?.name)}">↑</button><button class="button" type="button" data-clip-down="${ci}:${ii}" ${ii===proposal.chapters[ci].clips.length-1?"disabled":""} aria-label="下移 ${esc(asset?.name)}">↓</button></div></article>`;
  }
  function collect() {
    const form=root().querySelector("#story-edit-form");if(!form)return;
    proposal.title=form.elements.title.value;proposal.brief=form.elements.brief.value;
    root().querySelectorAll("[data-chapter]").forEach(section=>{
      const chapter=proposal.chapters[Number(section.dataset.chapter)];chapter.title=section.querySelector("[data-chapter-title]").value;chapter.summary=section.querySelector("[data-chapter-summary]").value;
      section.querySelectorAll("[data-clip]").forEach(row=>{const clip=chapter.clips[Number(row.dataset.clip)];clip.notes=row.querySelector("[data-clip-note]").value;const input=row.querySelector("[data-clip-in]"),out=row.querySelector("[data-clip-out]");if(input){clip.source_in=input.value===""?null:Number(input.value);clip.source_out=out.value===""?null:Number(out.value);}else clip.photo_duration=Number(row.querySelector("[data-photo-duration]").value);});
    });
  }
  async function save(status=null) {
    if(busy)return false;if(!proposal||!dirty&&!status)return true;
    if(historical){toast("历史版本只读，请读取最新版后编辑。");return false;}
    collect();
    const workspaceId=workspace.workspace_id, proposalId=proposal.proposal_id;
    const body={revision:proposal.revision,workspace_revision:workspace.revision,title:proposal.title,brief:proposal.brief,chapters:proposal.chapters,...(status?{status}:{})};
    return changeView(async ticket=>{
      const node=root().querySelector("#story-save-status");if(node)node.textContent="正在保存…";
      try {
        const result=await api(`/api/review-workspaces/${workspaceId}/storyboards/${proposalId}`,{method:"PATCH",body});
        if(!currentView(ticket)||workspace?.workspace_id!==workspaceId||proposal?.proposal_id!==proposalId)return false;
        proposal=result.proposal;dirty=false;historical=false;
        historyRows=[{revision:proposal.revision},...historyRows.filter(row=>(typeof row==="number"?row:row.revision)!==proposal.revision)];
        render();toast(status==="approved"?"草案已批准，agent 可读取确认结果":"草案已保存");return true;
      } catch(error){
        if(currentView(ticket)){const statusNode=root().querySelector("#story-save-status");if(statusNode)statusNode.textContent=`未保存：${error.message}`;}
        throw error;
      }
    });
  }
  async function reloadLatest() {
    if(busy||!proposal)return false;
    const workspaceId=workspace.workspace_id, proposalId=proposal.proposal_id;
    return changeView(async ticket=>{
      if(dirty){
        collect();downloadLocal();
        try{localStorage.setItem(`photoai-story-draft:${workspaceId}:${proposalId}`,JSON.stringify(proposal));}
        catch(_error){toast("浏览器暂存不可用，当前草稿已另行下载。");}
      }
      const path=`/api/review-workspaces/${workspaceId}`;
      const [fresh,result,history]=await Promise.all([api(path),api(`${path}/storyboards/${proposalId}`),api(`${path}/storyboards/${proposalId}/history`)]);
      if(!currentView(ticket)||workspace?.workspace_id!==workspaceId||proposal?.proposal_id!==proposalId)return false;
      workspace=fresh;proposal=result.proposal;historyRows=history.revisions;historical=proposal.is_latest===false;dirty=false;render();return true;
    });
  }
  function downloadLocal(){const blob=new Blob([JSON.stringify(proposal,null,2)],{type:"application/json"});const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=`storyboard-${proposal.proposal_id}-r${proposal.revision}.json`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  async function close(){
    if(busy)return false;
    if(!await save())return false;
    ++sessionEpoch;++viewEpoch;clearTimeout(timer);dialog().close();return true;
  }
  function showJob(){
    const node=root().querySelector("#story-job");if(!node||!job)return;
    node.classList.remove("hidden");
    node.innerHTML=`<div><strong>AI 编排 · ${{queued:"等待",running:"进行中",cancelling:"正在停止",done:"完成",failed:"失败",cancelled:"停止"}[job.status]||job.status}</strong><span>${esc(job.error||job.stage||"")}</span>${running(job)?'<button class="text-button" data-story-cancel>停止</button>':job.proposal_id?'<button class="button" data-story-job-open>打开新草案</button>':""}</div><progress max="100" value="${job.progress||0}"></progress>`;
    node.querySelector("[data-story-job-open]")?.addEventListener("click",()=>{if(!busy)load(job.proposal_id);});
    node.querySelector("[data-story-cancel]")?.addEventListener("click",async()=>{
      if(busy)return;
      const workspaceId=workspace.workspace_id,jobId=job.job_id,session=sessionEpoch;
      try{
        const result=await api(`/api/review-workspaces/${workspaceId}/storyboard-jobs/${jobId}/cancel`,{method:"POST"});
        if(session!==sessionEpoch||workspace?.workspace_id!==workspaceId||job?.job_id!==jobId||!dialog().open)return;
        job=result.job;showJob();
      }catch(error){if(session===sessionEpoch)toast(error.message);}
    });
    lockViewControls();
  }
  async function poll(){
    clearTimeout(timer);if(!workspace||!job||!dialog().open)return;
    const workspaceId=workspace.workspace_id,jobId=job.job_id,session=sessionEpoch;
    try{
      const result=await api(`/api/review-workspaces/${workspaceId}/storyboard-jobs/${jobId}`);
      if(session!==sessionEpoch||workspace?.workspace_id!==workspaceId||job?.job_id!==jobId||!dialog().open)return;
      job=result.job;showJob();
      // Completion never navigates or saves another draft. The user explicitly
      // opens the new proposal when ready, so their current edit stays intact.
      if(running(job))timer=setTimeout(poll,1200);
    }catch(error){if(session===sessionEpoch&&dialog().open)toast(error.message);}
  }
  document.addEventListener("DOMContentLoaded",()=>dialog().addEventListener("cancel",event=>{event.preventDefault();close();}));
  window.addEventListener("beforeunload",event=>{if(dirty||busy){event.preventDefault();event.returnValue="";}});
  return {open,close,hasPending:()=>dirty||busy};
})();
