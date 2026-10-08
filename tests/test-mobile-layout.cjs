const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright-core');

function chromeCommand() {
  const candidates = process.platform === 'darwin'
    ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Chromium.app/Contents/MacOS/Chromium']
    : ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'];
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ['--version'], { encoding: 'utf8' });
    if (!probe.error && probe.status === 0) return candidate;
  }
  throw new Error(`Chrome/Chromium is required for the 320px layout test; tried: ${candidates.join(', ')}`);
}

test('Home, Sessions, and Fresh Start render without horizontal clipping at 320px', async () => {
  const bundledChromium = chromium.executablePath();
  const browser = await chromium.launch({
    ...(fs.existsSync(bundledChromium) ? {} : { executablePath: chromeCommand() }),
    headless: true,
  });
  try {
    const page = await browser.newPage({ viewport: { width: 320, height: 800 } });
    await page.goto(pathToFileURL(path.join(__dirname, 'mobile-layout-fixture.html')).href);
    await page.waitForFunction(() => document.getElementById('layout-result')?.textContent !== 'pending');
    const metrics = JSON.parse(await page.locator('#layout-result').textContent());
    assert.deepEqual(metrics, {
      viewportWidth: 320,
      overflow: false,
      homeFits: true,
      actionVisible: true,
      drawerFits: true,
      shellFits: true,
      chipVisible: true,
    });
  } finally {
    await browser.close();
  }
});
