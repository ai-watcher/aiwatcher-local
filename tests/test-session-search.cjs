const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const js = fs.readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.js'), 'utf8');

function extract(name) {
  const start = js.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1);
  const rest = js.slice(start);
  const brace = rest.indexOf('{');
  const end = rest.slice(brace).search(/\n(?:async function |function |let |const |class )/);
  return end < 0 ? rest : rest.slice(0, brace + end);
}

function harness() {
  const nodes = new Map();
  const pending = [];
  const timers = [];
  const rendered = [];
  const node = id => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        value: id === 'days' ? '7' : '',
        innerHTML: '',
        textContent: '',
        attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
      });
    }
    return nodes.get(id);
  };
  const window = {
    setTimeout(callback, delay) {
      const timer = { callback, delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) { if (timer) timer.cleared = true; },
  };
  const fetch = (url, options = {}) => new Promise((resolve, reject) => {
    pending.push({ url, options, resolve, reject });
  });
  const ctx = vm.createContext({
    AbortController,
    URLSearchParams,
    clearTimeout: window.clearTimeout,
    setTimeout: window.setTimeout,
    document: { getElementById: node },
    esc: value => String(value),
    fetch,
    renderSessionRows: (rows, filtered) => rendered.push({ rows, filtered }),
    window,
  });
  vm.runInContext("let sessionSearchTimer = null; let sessionSearchController = null; let sessionSearchToken = 0; const SESSION_SEARCH_TIMEOUT_MS = 15000; let sessionRowsCache = []; let sessionsLoadedForDays = null;", ctx);
  for (const name of ['cancelSessionSearch', 'renderSessionSearchState', 'debounceSessionSearch', 'clearSessionFilters', 'loadSessions']) {
    vm.runInContext(extract(name), ctx);
  }
  return { ctx, node, pending, rendered, timers };
}

test('changing the time window refreshes a visible Sessions list', () => {
  const nodes = {
    'view-sessions': { hidden: false },
    days: { value: '30' },
  };
  let searches = 0;
  const ctx = vm.createContext({
    agentHierarchyCache: null,
    document: { getElementById: id => nodes[id] || null },
    load() {},
    loadAgentHierarchy() {},
    loadSessions() { searches += 1; },
  });
  vm.runInContext("let sessionsLoadedForDays = '7'; let agentHierarchyLoadedForDays = '7'; let reportLoadedForDays = '7'; let sessionsViewMode = 'list'; let agentHierarchyCache = { sessions: [] }; let agentHierarchyError = '';", ctx);
  vm.runInContext(extract('changeWindow'), ctx);
  ctx.changeWindow();
  assert.equal(searches, 1);
});

test('changing the query immediately invalidates and aborts the prior request', async () => {
  const h = harness();
  const oldRequest = h.ctx.loadSessions();
  assert.equal(h.pending.length, 1);
  h.node('sessionSearch').value = 'new query';
  h.ctx.debounceSessionSearch();
  assert.equal(h.pending[0].options.signal.aborted, true);
  h.pending[0].resolve({ ok: true, json: async () => ({ sessions: [{ session_id: 'stale' }], total_scanned: 1 }) });
  await oldRequest;
  assert.equal(h.rendered.length, 0);
  assert.match(h.node('sessionRows').innerHTML, /Searching local sessions/);
});

test('only the latest out-of-order response updates rows and loaded state', async () => {
  const h = harness();
  const oldRequest = h.ctx.loadSessions();
  h.node('sessionSearch').value = 'current';
  const currentRequest = h.ctx.loadSessions();
  h.pending[1].resolve({
    ok: true,
    json: async () => ({ sessions: [{ session_id: 'current' }], total_scanned: 1, total_matched: 1 }),
  });
  await currentRequest;
  h.pending[0].resolve({
    ok: true,
    json: async () => ({ sessions: [{ session_id: 'stale' }], total_scanned: 1, total_matched: 1 }),
  });
  await oldRequest;
  assert.equal(h.rendered.length, 1);
  assert.equal(h.rendered[0].rows[0].session_id, 'current');
  assert.equal(vm.runInContext('sessionRowsCache[0].session_id', h.ctx), 'current');
  assert.equal(vm.runInContext('sessionsLoadedForDays', h.ctx), '7');
  assert.equal(h.node('sessionRows').attributes['aria-busy'], 'false');
});

test('pending state clears the cache so sorting cannot restore stale actions', () => {
  const h = harness();
  vm.runInContext("sessionRowsCache = [{ session_id: 'stale' }]", h.ctx);
  h.ctx.renderSessionSearchState('Searching local sessions...');
  assert.equal(vm.runInContext('sessionRowsCache.length', h.ctx), 0);
  assert.doesNotMatch(h.node('sessionRows').innerHTML, /stale/);
});

test('request failures replace stale rows with retry and clear actions', async () => {
  const h = harness();
  const request = h.ctx.loadSessions();
  h.pending[0].reject(new Error('offline'));
  await request;
  assert.match(h.node('sessionRows').innerHTML, /could not be completed/);
  assert.match(h.node('sessionRows').innerHTML, />Retry</);
  assert.match(h.node('sessionRows').innerHTML, />Clear filters</);
  assert.equal(h.node('sessionRows').attributes['aria-busy'], 'false');
});

test('timed out requests report a bounded failure instead of hanging', async () => {
  const h = harness();
  const request = h.ctx.loadSessions();
  const timeout = h.timers.find(timer => timer.delay === 15000);
  assert.ok(timeout);
  timeout.callback();
  const error = new Error('aborted');
  error.name = 'AbortError';
  h.pending[0].reject(error);
  await request;
  assert.match(h.node('sessionRows').innerHTML, /took too long/);
});
