const { test, expect } = require('@playwright/test');
const AxeBuilder = require('@axe-core/playwright').default;
const { createState, installFixtures } = require('./fixtures.cjs');
const url = 'https://huggingface.co/owner/repo/blob/main/model.gguf?download=true';
let state;
test.beforeEach(async ({ context, page }) => {
  state = createState();
  await installFixtures(context, state);
  await page.goto('/#models');
  await page.getByRole('button', { name: 'Download GGUF', exact: true }).click();
});

test('default name, progress, cancellation, and reload recovery', async ({ page }) => {
  await page.getByLabel('Hugging Face URL').fill(url);
  await expect(page.locator('#download-preview')).toHaveText('Save to: /models/model.gguf');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await expect(page.locator('#download-jobs')).toContainText('10%');
  await expect(page.getByRole('button', { name: 'Download', exact: true })).toBeDisabled();
  await page.goto('/#overview');
  await page.reload();
  await page.goto('/#models');
  await page.getByRole('button', { name: 'Download GGUF', exact: true }).click();
  await expect(page.locator('#download-jobs')).toContainText('model.gguf');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.locator('#download-jobs')).toContainText('cancelled');
  expect(state.requests.filter(r => r.path === '/api/models/downloads' && r.method === 'POST')).toHaveLength(1);
});

test('rename and destination; completion refreshes pickers without discarding drafts', async ({ page }) => {
  await page.getByRole('button', { name: '+ Add model', exact: true }).click();
  await page.locator('#mi-add-name').fill('Unfinished preset');
  await page.getByLabel('Hugging Face URL').fill(url);
  await page.getByLabel('Destination', { exact: true }).selectOption('mmproj');
  await page.getByLabel('Filename (optional)').fill('vision');
  await expect(page.locator('#download-preview')).toContainText('/models/mmproj/vision.gguf');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await expect(page.locator('#download-jobs')).toContainText('mmproj/vision.gguf');
  const before = state.requests.filter(r => r.path === '/api/models/files').length;
  state.downloads[0].state = 'completed';
  await expect(page.locator('#download-jobs')).toContainText('Saved to /models/mmproj/vision.gguf');
  await expect.poll(() => state.requests.filter(r => r.path === '/api/models/files').length).toBeGreaterThan(before);
  await expect(page.locator('#mi-add-name')).toHaveValue('Unfinished preset');
  expect(state.revision).toBe(1);
});

test('validation, server error, and preserved fields', async ({ page }) => {
  await page.getByLabel('Hugging Face URL').fill('https://example.com/model.gguf');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await expect(page.locator('#download-message')).toContainText('huggingface.co');
  await page.getByLabel('Hugging Face URL').fill(url);
  await page.getByLabel('Filename (optional)').fill('../bad.gguf');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await expect(page.locator('#download-message')).toContainText('plain filename');
  await page.getByLabel('Filename (optional)').fill('good.gguf');
  state.failNext = { path: '/api/models/downloads', status: 409 };
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await expect(page.locator('#download-message')).toContainText('Test failure');
  await expect(page.getByLabel('Filename (optional)')).toHaveValue('good.gguf');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await expect(page.locator('#download-jobs')).toContainText('good.gguf');
  state.downloads[0].state = 'failed'; state.downloads[0].error = 'Not enough disk space';
  await expect(page.locator('#download-jobs')).toContainText('Not enough disk space');
});

test('mobile accessibility and read-only INI independence', async ({ page }) => {
  state.writable = false;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Download', exact: true })).toBeEnabled();
  await page.getByLabel('Hugging Face URL').fill(url);
  await page.getByLabel('Destination', { exact: true }).selectOption('archived');
  await page.getByRole('button', { name: 'Download', exact: true }).click();
  await expect(page.locator('#download-jobs')).toContainText('archived/model.gguf');
  state.downloads[0].total_bytes = null;
  await expect(page.locator('#download-jobs progress')).not.toHaveAttribute('value');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const results = await new AxeBuilder({ page }).include('#download-panel').analyze();
  expect(results.violations).toEqual([]);
});
