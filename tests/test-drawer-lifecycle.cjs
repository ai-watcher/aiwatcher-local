const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.js'), 'utf8');
function functionSource(name) {
  const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1, name);
  const rest = source.slice(start);
  const next = rest.slice(rest.indexOf('{')).search(/\n(?:async function |function |let |const |class )/);
  return next < 0 ? rest : rest.slice(0, rest.indexOf('{') + next);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      classList: new Set(), setAttribute() {}, appendChild() {}, value: 7,
      insertAdjacentHTML() {}, remove() {},
    });
    const item = nodes.get(id);
    item.classList.contains = item.classList.has;
    item.classList.remove = item.classList.delete;
    return item;
  }
  const requests = [], writes = [], notices = [], timers = [];
  function request(url, payload, fetchResponse = false) {
    const pending = deferred();
    requests.push({ url, payload, ...pending });
    return fetchResponse ? pending.promise.then(data => ({ json: async () => data })) : pending.promise;
  }
  const context = vm.createContext({
    document: { getElementById: node, body: node('body'), createElement: () => node('pending') },
    window: { confirm: () => true, setTimeout: callback => timers.push(callback) },
    fetch: url => request(url, null, true), postJson: (url, payload) => request(url, payload),
    setDrawerContent: html => writes.push(html), setDrawerSubtitle() {},
    renderHandoff: data => `brief:${data.id}`, renderSessionSummary: data => `summary:${data.id}`,
    handoffOptionsFromForm: () => ({}), handoffPayload: id => ({ session_id: id }),
    esc: value => String(value), showToast: value => notices.push(value),
    paintDrawerHealth() {}, currentData: { ai_assist: { config: {} } },
    encodeURIComponent,
  });
  vm.runInContext('let drawerRequestToken = 0; const SESSION_LOOKUP_ATTEMPTS = 3;', context);
  for (const name of ['isCurrentDrawer', 'openDrawer', 'closeDrawer', 'openHandoff',
    'selectSession', 'selectProject', 'improveFreshStartWithAiAssist']) {
    vm.runInContext(functionSource(name), context);
  }
  function respond(url, value, occurrence = 0) {
    const req = requests.filter(item => item.url === url)[occurrence];
    assert.ok(req, url);
    req.resolve(value);
  }
  return { context, requests, writes, notices, timers, respond, node };
}

test('basic brief does not wait for summary; late summary cannot overwrite it', async () => {
  const h = harness();
  const done = h.context.openHandoff('A');
  h.respond('/api/handoff-basic', { id: 'A-basic' });
  await flush();
  assert.equal(h.writes.at(-1), 'brief:A-basic');
  h.respond('/api/session-summary?id=A', { id: 'A' });
  await flush();
  assert.equal(h.writes.at(-1), 'brief:A-basic');
  h.respond('/api/handoff', { id: 'A-full' });
  await done;
  assert.equal(h.writes.at(-1), 'brief:A-full');
});

test('full brief does not wait for basic and cannot be replaced by late basic', async () => {
  const h = harness();
  const done = h.context.openHandoff('A');
  h.respond('/api/handoff', { id: 'A-full' });
  await done;
  h.respond('/api/handoff-basic', { id: 'A-basic' });
  h.respond('/api/session-summary?id=A', { id: 'A' });
  await flush();
  assert.equal(h.writes.at(-1), 'brief:A-full');
});

test('failed detail request preserves the copyable basic brief', async () => {
  const h = harness();
  const done = h.context.openHandoff('A');
  h.respond('/api/handoff-basic', { id: 'A-basic' });
  h.requests.find(item => item.url === '/api/handoff').reject(new Error('offline'));
  const result = await done;
  assert.equal(result.id, 'A-basic');
  assert.equal(h.writes.at(-1), 'brief:A-basic');
  assert.match(h.notices.at(-1), /basic local brief/);
});

test('include-prompt request never silently falls back to a different basic brief', async () => {
  const h = harness();
  const done = h.context.openHandoff('A', 'generic', true);
  h.respond('/api/handoff-basic', { id: 'A-basic' });
  h.respond('/api/handoff', { error: 'Evidence unavailable' });
  await done;
  assert.ok(!h.writes.includes('brief:A-basic'));
  assert.match(h.writes.at(-1), /Evidence unavailable/);
});

for (const replacement of ['Session review', 'Project detail', 'Intervention receipt']) {
  test(`old handoff cannot overwrite ${replacement}`, async () => {
    const h = harness();
    const done = h.context.openHandoff('A');
    h.context.openDrawer(replacement);
    const count = h.writes.length;
    h.respond('/api/session-summary?id=A', { id: 'A' });
    h.respond('/api/handoff-basic', { id: 'A-basic' });
    h.respond('/api/handoff', { id: 'A-full' });
    assert.equal(await done, null);
    await flush();
    assert.equal(h.writes.length, count);
  });
}

test('closing and reopening prevents old responses from returning', async () => {
  const h = harness();
  const first = h.context.openHandoff('A');
  h.context.closeDrawer();
  const second = h.context.openHandoff('B');
  h.respond('/api/handoff', { id: 'B' }, 1);
  await second;
  h.respond('/api/handoff', { id: 'A' });
  await first;
  assert.equal(h.writes.at(-1), 'brief:B');
});

for (const pending of [false, true]) {
  test(`session ${pending ? 'indexing' : 'missing'} retry cannot reopen a closed drawer`, async () => {
    const h = harness();
    const done = h.context.selectSession('A');
    h.respond('/api/session-summary?id=A', { id: 'A' });
    h.respond('/api/session?id=A', pending ? { detail_pending: true } : { error: 'not found' });
    await done;
    assert.equal(h.timers.length, 1);
    h.context.closeDrawer();
    h.context.openDrawer('Project detail');
    const count = h.requests.length;
    h.timers[0]();
    assert.equal(h.requests.length, count);
    assert.equal(h.node('drawerTitle').textContent, 'Project detail');
  });
}

test('session and project responses cannot overwrite a later handoff', async () => {
  const h = harness();
  const session = h.context.selectSession('A');
  const project = h.context.selectProject('/demo/project');
  h.context.openDrawer('Fresh Start');
  const count = h.writes.length;
  h.respond('/api/session-summary?id=A', { id: 'A' });
  h.respond('/api/session?id=A', { id: 'A' });
  h.respond('/api/project?days=7&project=%2Fdemo%2Fproject', { project_short: 'wrong' });
  await Promise.all([session, project]);
  assert.equal(h.writes.length, count);
  assert.equal(h.node('drawerTitle').textContent, 'Fresh Start');
});

test('local enrichment cannot replace a newer AI handoff', async () => {
  const h = harness();
  const local = h.context.openHandoff('A');
  h.respond('/api/handoff-basic', { id: 'A-basic' });
  await flush();
  const ai = h.context.improveFreshStartWithAiAssist('A');
  h.respond('/api/handoff-ai-assist', { id: 'A-ai', ai_assist_result: { status: 'used' } });
  await ai;
  h.respond('/api/handoff', { id: 'A-local' });
  await local;
  assert.equal(h.writes.at(-1), 'brief:A-ai');
});

test('AI response cannot replace a newer session drawer', async () => {
  const h = harness();
  h.context.openDrawer('Fresh Start');
  const ai = h.context.improveFreshStartWithAiAssist('A');
  h.context.openDrawer('Session review');
  h.respond('/api/handoff-ai-assist', { id: 'A-ai' });
  await ai;
  assert.equal(h.writes.length, 0);
  assert.equal(h.notices.length, 0);
});

test('abandoned session detail rejection is handled even if the summary is still pending', async () => {
  const h = harness();
  const done = h.context.selectSession('A');
  h.context.openDrawer('Fresh Start');
  h.requests.find(item => item.url === '/api/session?id=A').reject(new Error('offline'));
  await flush();
  h.respond('/api/session-summary?id=A', { id: 'A' });
  await done;
  assert.equal(h.node('drawerTitle').textContent, 'Fresh Start');
});
