const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const source = readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.js'), 'utf8');
function fn(name) {
  const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1);
  const rest = source.slice(start);
  const next = rest.slice(rest.indexOf('{')).search(/\n(?:async function |function |let |const |class )/);
  return rest.slice(0, rest.indexOf('{') + next);
}
function harness() {
  const writes = [], requests = [], timers = [];
  let current = true;
  const ctx = vm.createContext({
    esc: value => String(value || '').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
    jsArg: JSON.stringify, openDrawer: () => 1, isCurrentDrawer: () => current,
    setDrawerContent: html => writes.push(html),
    fetch: async url => { requests.push(url); return { ok: true, json: async () => ({ compact: { sessions: [
      { session_id: 'target', tool: 'codex-cli', stage: 'nudge', command: '/compact', preservation_prompt: '<checkpoint>', sha: 'abc' },
    ] } }) }; },
    navigator: { clipboard: { writeText: async text => writes.push(text) } },
    window: { setTimeout: task => timers.push(task) }, showToast() {}, copyTextFallback: () => false,
  });
  vm.runInContext([fn('renderCompactReview'), fn('openCompactReview'), fn('copyCompactCommand')].join('\n'), ctx);
  return { ctx, writes, requests, timers, stale: () => { current = false; } };
}
test('guided review keeps preservation message separate from native command and escapes metadata', async () => {
  const h = harness();
  await h.ctx.openCompactReview('target');
  assert.equal(h.requests[0], '/api/companion-state');
  const html = h.writes.at(-1);
  for (const text of ['Copy preservation brief', 'Copy native command', 'estimated', '&lt;checkpoint>', '10 seconds']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('<checkpoint>'));
  assert.equal(h.writes.filter(text => text === '/compact').length, 0);
});
test('stale review response cannot replace a different drawer', async () => {
  const h = harness();
  const pending = h.ctx.openCompactReview('target');
  h.stale(); await pending;
  assert.equal(h.writes.length, 1);
});
test('missing live candidate offers no compact command', async () => {
  const h = harness(); await h.ctx.openCompactReview('missing');
  assert.ok(h.writes.at(-1).includes('no longer has a current compact recommendation'));
  assert.ok(!h.writes.at(-1).includes('Copy native command'));
});
test('copy acknowledgement resets its button and records only the command copy', async () => {
  const h = harness(); const button = { textContent: 'Copy native command', disabled: false };
  await h.ctx.copyCompactCommand('target', 'abc', '/compact', button);
  assert.equal(button.textContent, 'Copied'); assert.equal(button.disabled, true);
  h.timers[0]();
  assert.equal(button.textContent, 'Copy native command'); assert.equal(button.disabled, false);
  assert.equal(h.writes[0], '/compact');
  assert.ok(h.requests.includes('/api/compact-decision'));
});
test('guided review fits a narrow drawer with long paths and an expanded preservation brief', async () => {
  const { chromium } = require('playwright-core');
  const { existsSync } = require('node:fs');
  const browser = await chromium.launch({
    ...(existsSync(chromium.executablePath()) ? {} : { executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' }),
    headless: true,
  });
  try {
    const h = harness();
    const html = h.ctx.renderCompactReview({
      session_id: 'target', name: 'Neighborhood', tool: 'codex-cli', stage: 'nudge',
      command: '/compact', preservation_prompt: 'Preserve the current objective, decisions and unfinished work. '.repeat(15),
      checkout: '/Users/example/projects/' + 'long-directory-name/'.repeat(8),
      dead_label: '120k', latest_label: '160k', after_label: '40k', sha_short: 'abc1234',
    });
    const page = await browser.newPage({ viewport: { width: 320, height: 1000 } });
    await page.setContent(`<main style="padding:16px">${html}</main>`);
    await page.addStyleTag({ content: readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.css'), 'utf8') });
    await page.locator('details').evaluate(node => { node.open = true; });
    const overflows = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(overflows, false);
    assert.equal(await page.getByRole('button', { name: 'Copy native command' }).isVisible(), true);
    if (process.env.AIWATCHER_COMPACT_PREVIEW) await page.screenshot({ path: process.env.AIWATCHER_COMPACT_PREVIEW, fullPage: true });
  } finally { await browser.close(); }
});
