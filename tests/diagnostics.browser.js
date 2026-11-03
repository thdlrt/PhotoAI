async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  await page.waitForFunction(()=>Boolean(state.token&&state.bootstrap));
  await page.locator('[data-view="settings"]').click();
  await page.waitForFunction(()=>document.querySelector('#app-update-version').textContent.includes('构建'));
  const report = await page.evaluate(()=>api('/api/diagnostics'));
  if(report.build.status!=='identified')throw Error('build not identified');
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.qaCopied=text;}}}));
  await page.locator('#app-diagnostics-copy').click();
  await page.waitForFunction(()=>Boolean(window.qaCopied));
  if(await page.evaluate(()=>window.qaCopied)!==report.copy_text)throw Error('wrong clipboard payload');
  await page.evaluate(()=>navigator.clipboard.writeText=async()=>{throw Error('permission denied');});
  await page.locator('#app-diagnostics-copy').click();
  await page.locator('#app-diagnostics-fallback').waitFor({state:'visible'});
  if(await page.locator('#app-diagnostics-text').inputValue()!==report.copy_text)throw Error('manual copy mismatch');
  await page.locator('.software-update-settings').screenshot({path:'output/playwright/refactor-round17-diagnostics.png'});
  await page.setViewportSize({width:760,height:900});
  const boxes=await page.locator('.software-update-settings button').evaluateAll(nodes=>nodes.map(node=>{
    const r=node.getBoundingClientRect();return {visible:r.width>0,left:r.left,right:r.right};
  }));
  if(boxes.some(r=>r.visible&&(r.left<0||r.right>760)))throw Error('settings controls overflow');
  return {status:'passed',build:report.build,clipboard:true,manualFallback:true,narrowWindow:true};
}
