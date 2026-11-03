import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const elements=new Map();
const element=key=>{
  if(!elements.has(key))elements.set(key,{disabled:false,value:'',textContent:'',style:{},
    hidden:true,classList:{add(){element(key).hidden=true;},remove(){element(key).hidden=false;}},
    focus(){this.focused=true;},select(){this.selected=true;}});
  return elements.get(key);
};
const context=vm.createContext({console,navigator:{clipboard:{writeText:async()=>{}}},
  document:{querySelector:element,querySelectorAll:()=>[],addEventListener(){}},setTimeout(){},clearTimeout(){}});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const run=code=>vm.runInContext(code,context);
run(`let notices=[];toast=value=>notices.push(value);let calls=0;
  api=async()=>{calls++;return {copy_text:'safe diagnostic'};};
  let copied='';navigator.clipboard.writeText=async text=>{copied=text;};`);
await run('copyDiagnostics()');
assert.equal(run('copied'),'safe diagnostic');
assert.equal(element('#app-diagnostics-fallback').hidden,true);
run(`navigator.clipboard.writeText=async()=>{throw Error('permission denied');};`);
await run('copyDiagnostics()');
assert.equal(element('#app-diagnostics-fallback').hidden,false);
assert.equal(element('#app-diagnostics-text').value,'safe diagnostic');
assert.ok(element('#app-diagnostics-text').focused&&element('#app-diagnostics-text').selected);
run(`api=async()=>{throw Error('connection lost');};`);
await run('copyDiagnostics()');
assert.equal(element('#app-diagnostics-copy').disabled,false);
assert.equal(element('#app-diagnostics-fallback').hidden,true);
assert.equal(run('notices.at(-1)'),'connection lost');
console.log('Diagnostic clipboard success, manual fallback and request failure PASS');
