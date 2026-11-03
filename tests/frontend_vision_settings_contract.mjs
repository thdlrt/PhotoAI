import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const elements = new Map();
const element = key => {
  if (!elements.has(key)) elements.set(key, {value: "", checked: false, textContent: "", innerHTML: "", disabled: false, addEventListener() {}});
  return elements.get(key);
};
const context = vm.createContext({document: {querySelector: element, addEventListener() {}},
  console, URLSearchParams, setTimeout, clearTimeout});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const run = code => vm.runInContext(code, context);
const deepseek = {default:"off", options:[{value:"off",label:"关闭"},{value:"low",label:"低"},
  {value:"high",label:"高"},{value:"max",label:"最高"},{value:"auto",label:"服务默认"}], hint:"test hint"};
context.deepseek = deepseek;
run("renderVisionThinkingOptions(deepseek, 'high')");
assert.equal(element("#vision-thinking").value, "high");
assert.equal(element("#vision-thinking").disabled, false);
assert.ok(!element("#vision-thinking").innerHTML.includes('value="medium"'));
run("renderVisionThinkingOptions(deepseek, 'medium')");
assert.equal(element("#vision-thinking").value, "off");
run("renderVisionThinkingOptions({default:'auto',options:[{value:'auto',label:'服务默认'}]}, 'off')");
assert.equal(element("#vision-thinking").value, "auto");
assert.equal(element("#vision-thinking").disabled, true);
for (const [name,value] of Object.entries({mode:"cloud",url:"https://api.deepseek.com",model:"deepseek-flash",
  key:"private-key-sentinel",thinking:"high",timeout:"120"})) element(`#vision-${name}`).value = value;
element("#vision-consent").checked = true;
let body;
context.capture = value => {body = value;};
run("api = async (_path, options) => {capture(options.body); return {thinking_capabilities:deepseek, thinking_effort:'high'};}; refreshModelResources = async () => {};");
const button = {disabled:false};
context.event = {preventDefault() {}, submitter:button};
await run("saveVisionProviderSettings(event)");
assert.equal(body.thinking_effort, "high");
assert.equal(body.upload_consent, true);
assert.equal(element("#vision-thinking").value, "high");
assert.equal(element("#vision-key").value, "");
assert.equal(button.disabled, false);
assert.ok(!element("#vision-status").textContent.includes("private-key-sentinel"));

// Old in-flight option requests must not replace a newer provider's options.
const resolvers = [];
context.queue = () => new Promise(resolve => resolvers.push(resolve));
run("api = async () => queue()");
const old = run("refreshVisionThinkingOptions()");
const latest = run("refreshVisionThinkingOptions()");
resolvers[1]({default:"auto", options:[{value:"auto",label:"服务默认"}], hint:"latest"});
await latest;
resolvers[0](deepseek);
await old;
assert.equal(element("#vision-thinking").disabled, true);
assert.equal(element("#vision-thinking-hint").textContent, "latest");
console.log("vision settings: supported strengths, saved selection, key clearing and stale response protection PASS");
