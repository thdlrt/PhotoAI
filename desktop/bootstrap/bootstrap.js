const invoke = window.__TAURI__.core.invoke;

const statusText = document.querySelector("#status");
const detailText = document.querySelector("#detail");
const actions = document.querySelector("#actions");
const retryButton = document.querySelector("#retry");
const logsButton = document.querySelector("#logs");
const reconnectButton = document.querySelector("#reconnect");

function render(status) {
  const failed = status.phase === "error";
  statusText.textContent = failed ? "启动失败" : "正在启动…";
  detailText.hidden = !failed;
  detailText.textContent = failed ? status.message || "内部服务未能启动。" : "";
  actions.hidden = !failed;
  retryButton.disabled = status.phase === "starting";
}

async function refresh() {
  try {
    render(await invoke("startup_status"));
  } catch (error) {
    render({ phase: "error", message: String(error) });
  }
}

retryButton.addEventListener("click", async () => {
  retryButton.disabled = true;
  statusText.textContent = "正在重试…";
  detailText.hidden = true;
  try {
    await invoke("retry_service");
  } catch (error) {
    render({ phase: "error", message: String(error) });
  } finally {
    retryButton.disabled = false;
  }
});

logsButton.addEventListener("click", async () => {
  try {
    await invoke("open_logs");
  } catch (error) {
    render({ phase: "error", message: String(error) });
  }
});

reconnectButton.addEventListener("click", async () => {
  reconnectButton.disabled = true;
  try {
    await invoke("reconnect_content_root");
  } catch (error) {
    render({ phase: "error", message: String(error) });
    reconnectButton.disabled = false;
  }
});

refresh();
setInterval(refresh, 750);

