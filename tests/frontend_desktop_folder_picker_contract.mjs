import assert from "node:assert/strict";
import fs from "node:fs";

const [appPath, templatePath, cssPath, capabilityPath] = process.argv.slice(2);
const app = fs.readFileSync(appPath, "utf8");
const template = fs.readFileSync(templatePath, "utf8");
const css = fs.readFileSync(cssPath, "utf8");
const capabilities = JSON.parse(fs.readFileSync(capabilityPath, "utf8"));

for (const target of [
  "cull-path",
  "raw-jpeg-mixed-path",
  "raw-jpeg-raw-path",
  "raw-jpeg-jpeg-path",
  "xmp-cleanup-path",
  "export-directory",
]) {
  assert.match(template, new RegExp(`class="[^"]*desktop-folder-picker[^"]*"[^>]*data-folder-target="${target}"`));
}

assert.match(app, /window\.__TAURI__\?\.core\?\.invoke/);
assert.match(app, /button\.classList\.toggle\("hidden", !invoke\)/);
assert.match(app, /invoke\("pick_folder", \{/);
assert.match(app, /invoke\("reconnect_content_root"\)/);
assert.match(app, /content_root_required/);
assert.match(app, /input\.dispatchEvent\(new Event\("input", \{ bubbles: true \}\)\)/);
assert.match(css, /\.path-picker\s*\{/);

const remote = capabilities.find((item) => item.identifier === "main-window-loopback-folder-picker");
assert.ok(remote);
assert.deepEqual(remote.remote.urls, ["http://127.0.0.1:*/*"]);
assert.deepEqual(new Set(remote.permissions), new Set(["allow-pick-folder", "allow-ensure-content-root", "allow-reconnect-content-root"]));
assert.equal(remote.local, false);

console.log("frontend desktop folder picker contract passed");
