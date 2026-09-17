const { test, expect } = require('@playwright/test');
const { createState, installFixtures } = require('./fixtures.cjs');
let state;
test.beforeEach(async ({ context, page }) => {
  state = createState(); await installFixtures(context, state);
  page.on('pageerror', error => { throw error; });
});
async function select(page, name = 'Qwen3-8B') {
  await page.goto('/#models');
  await page.locator('.mi-row').filter({has: page.locator('.mi-name', {hasText: name})}).locator('.mi-select').click();
  await expect(page.locator('#mi-editor')).toBeVisible();
}
async function apply(page, name = 'Save changes') {
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', {name, exact:true}).click();
}

test('Overview and Models fit responsive breakpoints and retain centered narrow layouts', async ({page}, info) => {
  await page.goto('/');
  await expect(page.locator('#connection-status')).toHaveText('Telemetry live');
  await expect(page.locator('#llm-model')).toHaveText('Qwen3-8B');
  for (const width of [320,390,768,1055,1056,1280,1440]) {
    await page.setViewportSize({width,height:1000});
    await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const ram=await page.locator('#ram-card').boundingBox(), gpu=await page.locator('.gpu-card').boundingBox();
    if (width < 1056) {
      expect(gpu.y).toBeGreaterThan(ram.y);
      expect(Math.abs(ram.x - (width - ram.width)/2)).toBeLessThan(2);
    } else { expect(Math.abs(gpu.y-ram.y)).toBeLessThan(2); expect(gpu.width).toBeGreaterThanOrEqual(500); }
    if ([390,1440].includes(width)) await page.screenshot({path:info.outputPath('overview-'+width+'.png'),fullPage:true});
  }
  await page.locator('#nav-models').click();
  await page.locator('.mi-row[data-name="Qwen3-8B"] .mi-select').click();
  await page.screenshot({path:info.outputPath('models-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await expect(page.locator('.model-library')).not.toBeVisible();
  await page.locator('.mobile-back').click();
  await expect(page.locator('.model-library')).toBeVisible();
  await page.locator('.mi-row[data-name="Qwen3-8B"] .mi-select').click();
  await expect(page.locator('#mi-editor')).toBeVisible();
  await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('models-mobile.png'),fullPage:true});
});

test('section drafts survive navigation; saves and dialogs remain keyboard accessible', async ({page}) => {
  await select(page);
  await page.locator('#mi-model').fill('/models/new.gguf');
  await expect(page.locator('#mi-edit-state')).toHaveText('Unsaved changes');
  await page.getByRole('link',{name:'Overview',exact:true}).click();
  await page.locator('#nav-models').click();
  await expect(page.locator('#mi-model')).toHaveValue('/models/new.gguf');
  await page.locator('#mi-edit-save').click();
  await expect(page.locator('#mi-diff-cancel')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await expect(page.locator('#mi-edit-save')).toBeFocused();
  await expect(page.locator('#mi-edit-state')).toHaveText('Unsaved changes');
  await page.locator('#mi-edit-save').click();
  await apply(page);
  await expect(page.locator('#mi-edit-state')).toHaveText('No unsaved changes');
  expect(state.models.find(s=>s.name==='Qwen3-8B').model).toBe('/models/new.gguf');
  await expect(page.locator('#mi-selection-notice')).not.toBeVisible();
});

test('selection is stable across refresh and cancelled discard; reorder-only saves work', async ({page}) => {
  await select(page);
  await page.locator('#mi-params .mi-param').first().getByRole('button',{name:'Move parameter down',exact:true}).click();
  await expect(page.locator('#mi-edit-state')).toHaveText('Unsaved changes');
  await page.locator('.mi-select').filter({hasText:'Llama-3.3'}).click();
  await page.getByRole('dialog').getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(page.locator('#mi-editor-title')).toHaveText('Qwen3-8B');
  state.models.reverse();
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await expect(page.locator('.mi-row.selected')).toContainText('Qwen3-8B');
  await page.locator('#mi-edit-save').click();
  await apply(page);
  await expect(page.locator('#mi-edit-state')).toHaveText('No unsaved changes');
  expect(state.requests.findLast(r=>r.path.endsWith('/edit')).body.key_order).toEqual(['temp','ctx-size']);
});

test('two tabs cannot overwrite one another; comparison and reload preserve explicit choice', async ({page,context}) => {
  await select(page);
  const second = await context.newPage(); await select(second);
  await page.locator('#mi-model').fill('/first.gguf');
  await page.locator('#mi-edit-save').click(); await apply(page);
  await expect(page.locator('#mi-edit-state')).toHaveText('No unsaved changes');
  await second.locator('#mi-model').fill('/second.gguf');
  await second.locator('#mi-edit-save').click(); await apply(second);
  await expect(second.locator('#mi-edit-conflict')).toBeVisible();
  await second.locator('#mi-edit-conflict').getByRole('button',{name:'Compare with latest'}).click();
  await expect(second.locator('#mi-diff-pre')).toContainText('/first.gguf');
  await expect(second.locator('#mi-diff-pre')).toContainText('/second.gguf');
  await expect(second.locator('#mi-diff-apply')).not.toBeVisible();
  await second.getByRole('dialog').getByRole('button',{name:'Close',exact:true}).click();
  await second.locator('#mi-edit-conflict').getByRole('button',{name:'Reload latest…'}).click();
  await second.getByRole('dialog').getByRole('button',{name:'Cancel',exact:true}).click();
  await expect(second.locator('#mi-model')).toHaveValue('/second.gguf');
  await second.locator('#mi-edit-conflict').getByRole('button',{name:'Reload latest…'}).click();
  await apply(second,'Discard draft');
  await expect(second.locator('#mi-model')).toHaveValue('/first.gguf');
  await expect(second.locator('#mi-edit-state')).toHaveText('No unsaved changes');
});

test('add drafts, validation, copy and creation', async ({page}) => {
  await page.goto('/#models');
  await expect(page.locator('.mi-select')).toHaveCount(5);
  await page.getByRole('button',{name:'+ Add model'}).click();
  await page.locator('#mi-add-name').fill('New preset');
  await page.locator('#mi-add-copy-from').selectOption(JSON.stringify(['Qwen3-8B',false]));
  await page.locator('#mi-add-editor').getByRole('button',{name:'Copy',exact:true}).click();
  await page.locator('#mi-add-params').fill('invalid line');
  await page.locator('#mi-add-save').click();
  await expect(page.locator('#mi-add-status')).toContainText('unique key = value');
  await page.getByRole('link',{name:'Overview',exact:true}).click();
  await page.locator('#nav-models').click();
  await expect(page.locator('#mi-add-name')).toHaveValue('New preset');
  await page.locator('#mi-add-params').fill('temp = 0.4');
  await page.locator('#mi-add-save').click();
  await expect(page.locator('#mi-add-status')).toContainText('Created New preset');
  await expect(page.locator('#mi-add-state')).toHaveText('No unsaved changes');
  await expect(page.locator('.mi-select').filter({hasText:'New preset'})).toBeVisible();
});

test('raw save failure keeps draft; undo reflects server state, not dirty Git', async ({page}) => {
  await page.goto('/#models');
  await page.locator('#models-advanced > summary').click();
  await expect(page.locator('#models-undo-btn')).not.toBeVisible();
  await page.locator('#mi-editor-raw > summary').click();
  await expect(page.locator('#mi-raw-state')).toHaveText('No unsaved changes');
  await page.locator('#mi-raw-text').fill('[test]\nmodel = /test.gguf\n');
  state.failNext = {path:'/api/models/raw',status:500};
  await page.locator('#mi-raw-save').click(); await apply(page);
  await expect(page.locator('#mi-raw-state')).toHaveText('Unsaved changes');
  await expect(page.locator('#mi-raw-text')).toHaveValue('[test]\nmodel = /test.gguf\n');
  await page.locator('#mi-editor-raw > summary').click();
  await page.locator('#mi-editor-raw > summary').click();
  await expect(page.locator('#mi-raw-text')).toHaveValue('[test]\nmodel = /test.gguf\n');
  await page.locator('#mi-raw-save').click(); await apply(page);
  await expect(page.locator('#mi-raw-state')).toHaveText('No unsaved changes');
  await expect(page.locator('#models-undo-btn')).toBeVisible();
  await page.locator('#models-undo-btn').click(); await apply(page,'Undo file edit');
  await expect(page.locator('#models-undo-btn')).not.toBeVisible();
});

test('log lifecycle, search selection and telemetry stream stay stable across views', async ({page}) => {
  await page.goto('/');
  await page.getByRole('button',{name:'View logs',exact:true}).click();
  await expect(page.locator('#log-status-text')).toHaveText('connected');
  await page.evaluate(()=>{logAppendLine('match first');logAppendLine('match second');});
  await page.locator('#log-search').fill('match');
  await page.locator('#log-search-next').click();
  await page.locator('#log-search-next').click();
  await page.evaluate(()=>logAppendLine('match third'));
  await expect(page.locator('.log-cur').locator('..')).toHaveText('match second');
  await page.getByRole('button',{name:'Pause scrolling',exact:true}).click();
  await expect(page.getByRole('button',{name:'Resume scrolling',exact:true})).toBeVisible();
  await page.locator('#nav-models').click();
  await expect.poll(()=>page.evaluate(()=>window.__streams.filter(s=>s.url.startsWith('/api/logs')&&!s.closed).length)).toBe(0);
  await page.getByRole('link',{name:'Overview',exact:true}).click();
  await page.goBack(); await page.goForward();
  expect(await page.evaluate(()=>window.__streams.filter(s=>s.url==='/stream').length)).toBe(1);
});

test('read-only and unavailable probe states remain understandable', async ({page}) => {
  state.writable = false;
  await select(page);
  await expect(page.locator('#models-readonly')).toContainText('read-only');
  await expect(page.locator('#mi-model')).toBeDisabled();
  await page.getByRole('link',{name:'Overview',exact:true}).click();
  await page.evaluate(()=>{window.__tick.llama=null;window.__tick.disk=null;window.__streams[0].tick();});
  await expect(page.locator('#llm-state')).toHaveText('Unavailable');
  await expect(page.locator('#disk-detail')).toHaveText('Unavailable');
});
