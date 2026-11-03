// Run with playwright-cli run-code --filename=tests/review_grid.browser.js
// against output/refactor-review-server.py's isolated fixture.
async (page) => {
  await page.setViewportSize({width:1440,height:1000});
  const report = await page.evaluate(async () => {
    const assert = (condition, message) => {if (!condition) throw new Error(message);};
    await openRun('20261009-230000-123456');
    const fixture = state.currentRun;
    assert(fixture.input_root.includes('refactor-review-'), 'Requires an isolated review fixture');
    const container = document.createElement('div');
    document.body.append(container);
    const image = fixture.results[0].preview_url;
    const card = (index, rating, source=image) => `<article class="photo-card" data-index="${index}"><button class="preview-button"><img src="${source}" loading="lazy"></button><div>${rating}</div><button data-rate-index="${index}" data-rating="5">5</button></article>`;
    const group = (key, cards) => ({key,html:`<section><div class="filmstrip">${cards}</div></section>`});
    const grid = new KeyedReviewGrid(container);
    try {
      let sections = [group('a',card(0,4)+card(1,3)),group('b',card(2,0))];
      grid.render('one',sections);
      const originalImages=[...container.querySelectorAll('img')];
      const unchangedCard=container.querySelectorAll('.photo-card')[1];
      const unchangedGroup=container.children[1];
      container.querySelector('[data-rate-index]').focus();
      sections=[group('a',card(0,5)+card(1,3)),sections[1]];
      const stats=grid.render('one',sections);
      assert(stats.updated===1 && stats.reused===1,'Only changed group should update');
      assert([...container.querySelectorAll('img')].every((node,i)=>node===originalImages[i]),'Decoded images must be retained');
      assert(container.querySelectorAll('.photo-card')[1]===unchangedCard,'Unchanged card must be retained');
      assert(container.children[1]===unchangedGroup,'Unchanged group must be retained');
      assert(document.activeElement.dataset.rateIndex==='0','Rating keyboard focus must survive');
      grid.render('one',[sections[1],sections[0]]);
      assert(container.children[0]===unchangedGroup,'Reordering should move existing groups');
      grid.render('one',[group('b',card(2,0,image+'?changed'))]);
      assert(container.querySelector('img')!==originalImages[2],'Changed preview source must not retain stale image');
      assert(grid.sections.size===1 && container.children.length===1,'Removed groups must be released');
      grid.render('one',[{key:'excluded',html:'<details open><summary>Excluded</summary><span>1</span></details>'}]);
      container.firstElementChild.open=false;
      grid.render('one',[{key:'excluded',html:'<details open><summary>Excluded</summary><span>2</span></details>'}]);
      assert(!container.firstElementChild.open,'Preserve collapsed details');
      const prior=container.firstElementChild;
      grid.render('two',[{key:'excluded',html:'<details open><summary>Excluded</summary><span>2</span></details>'}]);
      assert(container.firstElementChild!==prior,'Never retain nodes across projects');
    } finally {container.remove();}

    const originalGrid=reviewGrid;
    const measurements=[];
    try {
      state.currentRun={...fixture,run_id:'isolated-grid-benchmark',results:Array.from({length:2000},(_,index)=>({
        ...fixture.results[0],index,filename:`BENCH${index}.ARW`,group_id:Math.floor(index/5)+1,
        rating:3,effective_rating:3,ai_rating:3,manual_override:false,
      }))};
      state.groupingMode=false;
      state.filter='all';
      for (const mode of ['full','keyed']) {
        reviewGrid=mode==='keyed'?new KeyedReviewGrid(document.querySelector('#review-groups')):null;
        renderReview();
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        const durations=[];
        let retained=0;
        for (let iteration=0;iteration<12;iteration++) {
          const previous=[...document.querySelectorAll('#review-groups img')];
          const item=state.currentRun.results[0];
          item.effective_rating=iteration%2?0:5;
          item.rating=item.effective_rating;
          item.manual_override=true;
          const start=performance.now();
          renderReview();
          document.querySelector('#review-groups').getBoundingClientRect();
          durations.push(performance.now()-start);
          const current=new Set(document.querySelectorAll('#review-groups img'));
          retained=previous.filter(node=>current.has(node)).length;
        }
        const sorted=durations.slice(2).sort((a,b)=>a-b);
        measurements.push({mode,photos:2000,groups:400,iterations:sorted.length,
          median_ms:sorted[Math.floor(sorted.length/2)],p95_ms:sorted[Math.ceil(sorted.length*.95)-1],
          retained_images:retained});
        if(mode==='keyed') assert(retained===2000,'Rating must retain every preview node');
      }
    } finally {
      state.currentRun=fixture;
      reviewGrid=originalGrid;
      // The benchmark rendered through another instance; invalidate its DOM ownership.
      reviewGrid.scope=null;
      renderReview();
    }
    window.gridBenchmark={scope:'same live Edge page; synthetic metadata and shared preview; synchronous render plus forced layout; first two iterations excluded',
      dom_contract:'passed',measurements};
    return window.gridBenchmark;
  });
  await page.evaluate(() => {window.originalReviewImages=[...document.querySelectorAll('#review-groups img')];});
  await page.locator('#review-groups [data-rate-index="0"][data-rating="5"]').click();
  await page.evaluate(async () => {await ratingSave;});
  await page.locator('[data-filter="candidates"]').click();
  await page.locator('#review-groups [data-rate-index="0"][data-rating="0"]').click();
  const actualEdit = await page.evaluate(async () => {
    await ratingSave;
    const current=new Set(document.querySelectorAll('#review-groups img'));
    const retained=window.originalReviewImages.every(node=>current.has(node));
    const hidden=document.querySelector('.photo-card[data-index="0"]').classList.contains('hidden-by-filter');
    if(!retained || !hidden || state.currentRun.results[0].effective_rating!==0) throw new Error('Real rating/filter update failed');
    return {retained_images:current.size,edited_photo_hidden_by_filter:hidden,review_revision:state.currentRun.review_revision};
  });
  await page.locator('[data-filter="all"]').click();
  await page.locator('#grouping-toggle').click();
  await page.locator('#batch-select-toggle').click();
  // The visible label is the user's target; the native input is visually hidden.
  await page.locator('.card-select').nth(0).click();
  await page.locator('.card-select').nth(1).click();
  await page.locator('.card-select').nth(0).click();
  const batch = await page.evaluate(() => {
    const selected=[...state.selectedIndices];
    const checked=[...document.querySelectorAll('input[data-select-index]:checked')].map(node=>Number(node.dataset.selectIndex));
    if(JSON.stringify(selected)!=='[1]' || JSON.stringify(checked)!=='[1]') throw new Error('Batch checkbox state drifted during DOM reuse');
    return {selected,checked};
  });
  await page.locator('#batch-select-toggle').click();
  await page.locator('#grouping-toggle').click();
  return {...report,actual_edit:actualEdit,batch};
}
