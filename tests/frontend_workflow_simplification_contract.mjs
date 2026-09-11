import assert from "node:assert/strict";
import fs from "node:fs";

const [appPath, templatePath, cssPath] = process.argv.slice(2);
if (!appPath || !templatePath || !cssPath) throw new Error("app.js, index.html and app.css paths are required");

const app = fs.readFileSync(appPath, "utf8");
const template = fs.readFileSync(templatePath, "utf8");
const css = fs.readFileSync(cssPath, "utf8");
const section = (start, end) => template.slice(template.indexOf(start), template.indexOf(end, template.indexOf(start)));

const cull = section('<form id="cull-form"', "</form>");
assert.match(cull, /id="cull-path"/);
assert.doesNotMatch(cull, /id="scoring-mode"|id="retain-ratio"/);
assert.match(cull, />创建工程</);
const projectStart = section('<section id="view-project"', '<section id="view-toolbox"');
assert.match(projectStart, /id="project-group-form"[\s\S]*>开始分析分组</);

const toolbox = section('<section id="view-toolbox"', '<section id="view-review"');
assert.match(toolbox, /data-toolbox-panel="home"[\s\S]*data-toolbox-open="raw-jpeg"[\s\S]*data-toolbox-open="xmp-cleanup"[\s\S]*data-toolbox-open="xmp-records"/);
assert.match(toolbox, /data-toolbox-panel="raw-jpeg"[\s\S]*data-toolbox-back[\s\S]*id="raw-jpeg-form"/);
assert.match(toolbox, /data-toolbox-panel="xmp-cleanup"[\s\S]*data-toolbox-back[\s\S]*id="xmp-cleanup-form"/);
assert.match(toolbox, /data-toolbox-panel="xmp-records"[\s\S]*data-toolbox-back[\s\S]*id="toolbox-xmp"/);
assert.match(toolbox, /id="xmp-cleanup-form"[\s\S]*id="xmp-cleanup-path"/);
assert.match(toolbox, /id="xmp-cleanup-recursive"[\s\S]*id="xmp-cleanup-preview"/);
assert.match(toolbox, /id="xmp-cleanup-result"[^>]*role="status"[^>]*aria-live="polite"/);
assert.match(toolbox, /id="xmp-cleanup-history"[\s\S]*id="xmp-cleanup-transactions"/);
const cleanupDialog = section('<dialog id="xmp-cleanup-confirm-dialog"', "</dialog>");
assert.match(cleanupDialog, /id="xmp-cleanup-confirm-copy"/);
assert.match(cleanupDialog, /id="xmp-cleanup-execute"[^>]*>永久删除</);
assert.match(toolbox, /扫描后永久删除，不可恢复/);
assert.doesNotMatch(cleanupDialog, /<input|文字确认/);
assert.equal((template.match(/data-view="toolbox"/g) || []).length, 1);
assert.doesNotMatch(template, /data-view="xmp|data-view="cleanup/);

const review = section('<section id="view-review"', '<section id="view-develop"');
assert.match(review, /id="score-settings"[\s\S]*id="retain-ratio"[\s\S]*id="scoring-mode"/);
assert.doesNotMatch(review, /id="review-export"|直接导出/);

const develop = section('<section id="view-develop"', '<section id="view-export"');
assert.match(develop, /data-color-mode="auto"/);
assert.match(develop, /data-color-mode="skip"/);
assert.match(develop, /data-style-scope="global">全局统一/);
assert.match(develop, /data-style-scope="group">按组设置/);
assert.match(develop, /id="style-recommend-all"[^>]*>一键为所有组生成</);
assert.match(develop, /id="style-group-batch-status"[^>]*role="status"[^>]*aria-live="polite"/);
assert.doesNotMatch(develop, /id="style-search-open"|id="style-progress"|id="develop-flow-progress"|id="develop-confirm-all"/);

const settings = section('<section id="view-settings"', '<section id="view-resources"');
assert.match(settings, /id="lightroom-detail"/);
assert.match(settings, /id="lightroom-config-form"[\s\S]*id="lightroom-auto-configure"/);
assert.match(settings, /id="lightroom-refresh"/);
assert.match(settings, /style-library-settings/);
assert.doesNotMatch(settings, /id="style-library-files"|style-include-lightroom|style-enable-bw/);
assert.match(template, /data-style-source-open="lightroom"[\s\S]*data-style-source-open="user"/);
assert.match(template, /id="style-library-files"[^>]*multiple/);
assert.match(template, /id="style-library-import"/);
assert.doesNotMatch(template, /id="train-form"|id="model-detail"|id="audit-button"|尚无可用模型|个人偏好/);
assert.match(template, /id="toolbox-xmp"[\s\S]*id="toolbox-transactions"/);

assert.match(template, /id="job-progress-track"[^>]*role="progressbar"/);
assert.match(css, /\.job-progress-fill[^}]*background:\s*var\(--success\)/);
assert.doesNotMatch(css, /\.develop-output-hint\.running/);

assert.doesNotMatch(app, /function (?:setColorEnabled|applyWithLightroom|saveXmpAtStage|submitTrain|startAudit)\b/);
assert.doesNotMatch(app, /data-export-writer|\.open-run|data-develop-style|data-develop-strength|data-style-rerun|data-style-skip/);
assert.doesNotMatch(app, /toast\([^\n]*(?:正在生成|生成中)[^\n]*外观/);
assert.match(app, /styleScope: "global"/);
assert.match(app, /state\.styleScope = "global"/);
assert.match(app, /function renderXmpCleanup\(\)/);
assert.match(app, /function previewXmpCleanup\(event\)/);
assert.match(app, /\/api\/tools\/xmp-cleanup\/preview/);
assert.match(app, /body:\s*\{\s*root_path:[\s\S]*recursive:/);
assert.match(app, /\/api\/tools\/xmp-cleanup\/execute/);
assert.match(app, /\/api\/tools\/xmp-cleanup\/rollback/);
assert.match(app, /xmp_cleanup_execute/);
assert.match(app, /xmp_cleanup_rollback/);
assert.match(app, /state\.xmpCleanupPlan = null; renderToolbox\(\);/);
assert.match(app, /state\.xmpCleanupTransactions = data\.xmp_cleanup_transactions \|\| \[\]/);
assert.match(app, /function openToolboxSection\(section = "home", historyMode = "push"\)/);
assert.match(app, /requested === "toolbox" \|\| requested\.startsWith\("toolbox\/"\)/);
assert.match(app, /function refreshMutationToken\(\)/);
assert.match(app, /POST|method: "POST"/);
assert.match(app, /\/api\/projects\$|api\("\/api\/projects"/);
assert.match(app, /\/api\/projects\/\$\{encodeURIComponent\(project\.project_id\)\}\/group/);
assert.match(app, /\/api\/style-library\/import/);
assert.match(app, /fetch\("\/api\/bootstrap", \{ cache: "no-store" \}\)/);
assert.match(app, /response\.status === 403 && \/操作令牌\.\*失效\//);
assert.match(app, /return api\(path, \{ \.\.\.requestOptions, __tokenRetryCount: retryCount \+ 1 \}\)/);
assert.doesNotMatch(app, /toast\([^\n]*操作令牌[^\n]*失效/);
assert.match(css, /\.xmp-cleanup-card\s*\{[^}]*margin-top:/);
assert.match(css, /\.toolbox-module-grid\s*\{[^}]*grid-template-columns:\s*repeat\(3,/);
assert.match(css, /\.style-group-row\s*\{[^}]*grid-template-columns:/);
assert.match(css, /\.style-group-row \.style-choices\s*\{[^}]*grid-template-columns:\s*repeat\(4,/);
assert.match(app, /updateDevelop\(Number\(cropChoice\.dataset\.developCropChoice\), \{ crop_id: cropChoice\.dataset\.cropId, confirmed: true \}\)/);
assert.match(app, /scope: resolvedScope,[\s\S]*group_id: resolvedScope === "group"/);
assert.match(app, /function recommendStylesForAllGroups\(\)/);
assert.match(app, /\/style-recommendations\/groups/);
assert.match(app, /body:\s*\{\s*base_revision: Number\(state\.developPlan\.revision\)\s*\}/);
assert.match(app, /develop\/style\/global/);
assert.match(app, /body: \{ base_revision: Number\(state\.developPlan\.revision\), scope: state\.styleScope \}/);
assert.match(app, /completedXmp = Boolean\(spec\.targets\?\.xmp\)/);
assert.match(app, /completedJpeg = Boolean\(spec\.targets\?\.jpeg\)/);
assert.match(app, /const selectedExportTargets = \{ \.\.\.state\.exportTargets \}/);
assert.match(app, /state\.exportTargets = selectedExportTargets;[\s\S]*await loadExport\("replace"\)/);

console.log("workflow simplification frontend contract passed");
