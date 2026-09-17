const {test,expect} = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;
const {createState,installFixtures} = require('./fixtures.cjs');
let state;
test.beforeEach(async ({context,page})=>{
  state=createState(); await installFixtures(context,state);
  page.on('pageerror',error=>{throw error;});
});
async function choose(page,name='Qwen3-8B') {
  await page.locator('.mi-row').filter({has:page.locator('.mi-name',{hasText:name})}).locator('.mi-select').click();
}
async function action(page,name) {
  await page.locator('.action-menu summary').click();
  await page.locator('.action-menu').getByRole('button',{name,exact:true}).click();
}
async function confirm(page,label) {
  await page.getByRole('dialog').getByRole('button',{name:label,exact:true}).click();
}

test('archive, restore, delete and global-section protections remain available',async({page})=>{
  await page.goto('/#models'); await choose(page);
  await action(page,'Archive'); await confirm(page,'Confirm archive');
  await expect.poll(()=>state.models.find(s=>s.name==='Qwen3-8B').archived).toBe(true);
  await expect(page.locator('#mi-editor')).not.toBeVisible();
  await choose(page);
  await action(page,'Restore'); await confirm(page,'Confirm restore');
  await expect.poll(()=>state.models.find(s=>s.name==='Qwen3-8B').archived).toBe(false);
  await expect(page.locator('#mi-editor')).not.toBeVisible();
  await choose(page);
  await action(page,'Delete permanently…'); await confirm(page,'Delete section');
  await expect.poll(()=>state.models.some(s=>s.name==='Qwen3-8B')).toBe(false);
  await page.locator('.mi-row[data-name="*"] .mi-select').click();
  await expect(page.locator('#mi-newname')).toHaveAttribute('readonly','');
  await page.locator('.action-menu summary').click();
  await expect(page.locator('#mi-delete-btn')).not.toBeVisible();
  await expect(page.locator('#mi-archive-btn')).not.toBeVisible();
});

test('section movement, rename and search preserve identities',async({page})=>{
  await page.goto('/#models'); await choose(page);
  await action(page,'Move section down');
  await expect.poll(()=>state.models.filter(s=>s.region==='models').map(s=>s.name)).toEqual(['Llama-3.3-70B','Qwen3-8B']);
  // Moving changes the file revision, so reload explicitly before editing.
  await page.locator('#mi-selection-notice').getByRole('button',{name:'Reload latest…'}).click();
  await page.locator('#mi-newname').fill('Renamed preset');
  await page.locator('#mi-edit-save').click(); await confirm(page,'Save changes');
  await expect(page.locator('#mi-editor-title')).toHaveText('Renamed preset');
  await expect(page.locator('.mi-row.selected')).toContainText('Renamed preset');
  await page.locator('#mi-filter').fill('no matches anywhere');
  await expect(page.locator('#mi-list-empty')).toHaveText('No sections match your filters.');
  await expect(page.locator('#mi-editor-title')).toHaveText('Renamed preset');
  await page.locator('#mi-filter').fill('Renamed preset');
  await expect(page.locator('#mi-filter-count')).toHaveText('1 / 5');
  await page.locator('#mi-region-filter').selectOption('archived_models');
  await expect(page.locator('#mi-filter-count')).toHaveText('0 / 5');
});

test('Git, backup and restart stay explicit and do not save drafts',async({page})=>{
  await page.goto('/#models'); await choose(page);
  await page.locator('#mi-model').fill('/unsaved.gguf');
  await page.locator('#models-advanced > summary').click();
  await page.locator('#models-commit-msg').fill('Tune context');
  for (const [button,actionName] of [['Commit file','commit'],['Pull updates','pull'],['Push commits','push']]) {
    await page.getByRole('button',{name:button,exact:true}).click();
    await confirm(page,'Confirm '+actionName);
    await expect.poll(()=>state.requests.filter(r=>r.path==='/api/models/git'&&r.body.action===actionName).length).toBe(1);
  }
  await expect(page.locator('#mi-model')).toHaveValue('/unsaved.gguf');
  expect(state.requests.some(r=>r.path.endsWith('/section/edit'))).toBe(false);
  const download=page.waitForEvent('download');
  await page.getByRole('link',{name:'Download backup'}).click();
  expect((await download).suggestedFilename()).toBe('models.ini');
  await page.locator('#nav-overview').click();
  await page.locator('#restart-btn').click();
  await confirm(page,'Cancel');
  expect(state.requests.some(r=>r.path==='/api/restart-llama')).toBe(false);
  await page.locator('#restart-btn').click(); await confirm(page,'Restart server');
  await expect(page.locator('#restart-status')).toContainText('Restart requested');
  expect(state.requests.filter(r=>r.path==='/api/restart-llama').length).toBe(1);
});

test('slow saves block duplicate requests and failed reloads preserve the draft',async({page})=>{
  await page.goto('/#models'); await choose(page);
  let release;
  const hold=new Promise(resolve=>{release=resolve;});
  let saves=0;
  await page.route('**/api/models/section/edit',async route=>{saves++;await hold;await route.fallback();});
  await page.locator('#mi-model').fill('/slow.gguf');
  await page.locator('#mi-edit-save').click();await confirm(page,'Save changes');
  await expect.poll(()=>saves).toBe(1);
  await page.evaluate(()=>miSaveEdit());
  expect(saves).toBe(1);
  await expect(page.locator('#mi-model')).toBeDisabled();
  release();
  await expect(page.locator('#mi-edit-state')).toHaveText('No unsaved changes');
  state.revision++;
  await page.locator('#mi-model').fill('/keep-this.gguf');
  await page.locator('#mi-edit-save').click();await confirm(page,'Save changes');
  await expect(page.locator('#mi-edit-conflict')).toBeVisible();
  state.failNext={path:'/api/models',network:true};
  await page.locator('#mi-edit-conflict').getByRole('button',{name:'Reload latest…'}).click();
  await confirm(page,'Discard draft');
  await expect(page.locator('#mi-model')).not.toBeDisabled();
  await expect(page.locator('#mi-model')).toHaveValue('/keep-this.gguf');
  await expect(page.locator('#mi-edit-conflict')).toBeVisible();
});

test('a late refresh cannot replace a newer library response',async({page})=>{
  await page.goto('/#models'); await choose(page);
  const old=await page.evaluate(()=>miData);
  let release;
  const gate=new Promise(resolve=>{release=resolve;});
  let reads=0;
  await page.route('**/api/models',async route=>{
    if (++reads===1) {await gate;await route.fulfill({contentType:'application/json',body:JSON.stringify(old)});}
    else await route.fallback();
  });
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await expect.poll(()=>reads).toBe(1);
  state.models.find(s=>s.name==='Llama-3.3-70B').name='Latest preset';state.revision++;
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await expect(page.locator('.mi-row[data-name="Latest preset"]')).toBeVisible();
  release();
  await expect.poll(()=>page.evaluate(()=>miData.revision)).toBe('r2');
  await expect(page.locator('.mi-row[data-name="Latest preset"]')).toBeVisible();
});

test('accessible views, dialogs, and 200% zoom-equivalent layout',async({page,context})=>{
  await page.goto('/'); await expect(page.locator('#connection-status')).toHaveText('Telemetry live');
  let results=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(results.violations).toEqual([]);
  await page.locator('#nav-models').click();await choose(page);
  results=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(results.violations).toEqual([]);
  await page.locator('#mi-model').fill('/changed.gguf');
  await page.locator('#mi-edit-save').click();
  results=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(results.violations).toEqual([]);
  await page.keyboard.press('Shift+Tab');
  await expect(page.locator('#mi-diff-pre')).toBeFocused();
  await page.keyboard.press('Escape');
  // 1440px display at 200% browser zoom has a 720px CSS viewport.
  const zoomed=await context.newPage();
  await zoomed.setViewportSize({width:720,height:550});
  await zoomed.goto('/#models');await choose(zoomed);
  await expect(zoomed.locator('.model-library')).not.toBeVisible();
  expect(await zoomed.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await expect(zoomed.locator('#mi-edit-save')).toBeVisible();
  const fields=zoomed.locator('#mi-edit-fields .editor-scroll');
  await fields.evaluate(el=>{el.scrollTop=el.scrollHeight;});
  const region=await fields.boundingBox(),save=await zoomed.locator('#mi-edit-fields .save-bar').boundingBox();
  expect(region.y+region.height).toBeLessThanOrEqual(save.y);
});
