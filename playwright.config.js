const { defineConfig } = require('@playwright/test');
const path = require('node:path');
process.env.PLAYWRIGHT_BROWSERS_PATH ||= path.resolve('.playwright-browsers');
module.exports = defineConfig({
  testDir: './tests/ui', testMatch: '**/*.spec.cjs',
  fullyParallel: true, workers: 2, timeout: 30000,
  use: { baseURL: 'http://127.0.0.1:8765', viewport: { width: 1440, height: 1100 },
    browserName: 'chromium', headless: true, reducedMotion: 'reduce',
    screenshot: 'only-on-failure', trace: 'retain-on-failure' },
  webServer: { command: '.venv/bin/python tests/ui/server.py', url: 'http://127.0.0.1:8765', reuseExistingServer: false },
});
