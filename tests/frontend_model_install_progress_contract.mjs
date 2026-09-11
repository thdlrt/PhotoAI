import assert from "node:assert/strict";
import fs from "node:fs";

const [templatePath, scriptPath, cssPath] = process.argv.slice(2);
const template = fs.readFileSync(templatePath, "utf8");
const script = fs.readFileSync(scriptPath, "utf8");
const css = fs.readFileSync(cssPath, "utf8");

for (const id of ["job-live-detail", "model-resource-list", "model-resource-task-note", "settings-content-root"]) {
  assert.match(template, new RegExp(`id=["']${id}["']`), `${id} must remain in the UI`);
}
assert.doesNotMatch(template, /id=["']model-install-progress["']/, "the duplicate standalone model progress module must be removed");
assert.match(template, /AI 模型安装与 Lightroom 相互独立/);

for (const field of [
  "detail",
  "current_resource",
  "downloaded_bytes",
  "total_bytes",
  "bytes_per_second",
  "eta_seconds",
  "resumed_bytes",
  "elapsed_seconds",
  "heartbeat_at",
]) {
  assert.match(script, new RegExp(`\\b${field}\\b`), `${field} must be consumed`);
}

assert.match(script, /progress\.unit === "B" \? current : null/, "legacy byte progress must remain compatible");
assert.match(script, /\["queued", "running", "cancelling", "failed", "interrupted"\]/, "active, failed and interrupted installs remain visible");
assert.doesNotMatch(script, /\["queued", "running", "cancelling", "completed"/, "completed installs must not leave a stale progress panel");
assert.match(script, /已中断；可以继续安装，已下载内容会复用/, "interrupted installs must explain resume behavior");
assert.match(script, /data-model-install-target/, "progress must render inside the matching resource row");
assert.match(script, /data-model-install-cancel/, "the active resource row must expose cancellation");
assert.match(script, /renderModelInstallProgress\(job\)/, "the global job renderer must refresh model details");
assert.match(script, /AI 环境尚未开始/);
assert.match(script, /data-model-offline-import="16gb"/);
assert.match(script, /invoke\("pick_offline_bundle"\)/);
assert.match(script, /\/api\/model-resources\/import-offline/);
assert.match(css, /\.model-profile-actions\s*\{/);
assert.match(css, /\.model-resource-inline-progress\s*\{/);
assert.match(css, /\.model-resource-progress-track\.indeterminate/);

console.log("model install progress frontend contract passed");
