globalThis.PhotoAgentPanel = (() => {
  const quote = value => `'${String(value).replaceAll("'", "''")}'`;
  async function open() {
    const info = await api("/api/agent-info");
    const root = document.querySelector("#agent-toolbox-root");
    const arguments_ = info.connection_file ? ["--connection-file",info.connection_file] : ["--url",info.origin];
    const prefix = `& ${[...info.invocation,...arguments_].map(quote).join(" ")}`;
    const commands = [
      {title:"查看能力与命令", command:`${prefix} capabilities`},
      {title:"获取机器可读命令结构", command:`${prefix} schema`},
      {title:"列出审片工程", command:`${prefix} review list`},
    ];
    root.innerHTML = `<section class="panel tool-settings"><h2>与 Codex 共用工作区</h2><p>把以下命令交给 agent。它可以读取当前选择、提交建议和草案，并等待你在界面完成确认。</p><ol class="agent-workflow">${info.workflow.map(value=>`<li>${escapeHtml(value)}</li>`).join("")}</ol><p class="subtle">${escapeHtml(info.revision_policy)}</p></section><div class="section-title"><h2>PowerShell 命令</h2></div>${commands.map((value,index)=>`<section class="panel agent-command"><div><strong>${value.title}</strong><button class="text-button" data-copy-command="${index}">复制</button></div><pre><code>${escapeHtml(value.command)}</code></pre></section>`).join("")}<div class="panel tool-settings"><h3>当前连接</h3><p>${escapeHtml(info.origin)}</p><small class="path-summary">${escapeHtml(info.connection_file || "开发服务：使用本机 URL 连接")}</small><p class="subtle">连接凭据由当前系统账户保护。CLI 输出统一 JSON，调用完成、失败与待确认状态可由 agent 读取。</p></div>`;
    root.querySelectorAll("[data-copy-command]").forEach(button=>button.onclick=async()=>{const text=commands[Number(button.dataset.copyCommand)].command;try{await navigator.clipboard.writeText(text);toast("命令已复制");}catch{const pre=button.closest("section").querySelector("code"),selection=window.getSelection(),range=document.createRange();range.selectNodeContents(pre);selection.removeAllRanges();selection.addRange(range);toast("已选中命令，请按 Ctrl+C");}});
  }
  return {open};
})();
