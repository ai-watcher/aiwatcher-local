const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const js = fs.readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.css'), 'utf8');

function extract(name) {
  const start = js.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1, `missing ${name}`);
  const rest = js.slice(start);
  const brace = rest.indexOf('{');
  const end = rest.slice(brace).search(/\n(?:async function |function |let |const |class |\(async )/);
  return end < 0 ? rest : rest.slice(0, brace + end);
}

function locationState(href) {
  let current = new URL(href);
  return {
    get href() { return current.href; },
    get search() { return current.search; },
    get hash() { return current.hash; },
    get pathname() { return current.pathname; },
    set href(value) { current = new URL(String(value), current); },
  };
}

test('view navigation clears stale hashes before persisting the destination', () => {
  const location = locationState('http://127.0.0.1:8765/?view=watch#contextHealth');
  const views = [{ id: 'view-watch' }, { id: 'view-sessions' }];
  const nav = [
    { dataset: { view: 'watch' }, classList: { toggle() {} } },
    { dataset: { view: 'sessions' }, classList: { toggle() {} } },
  ];
  const history = {
    pushState(_state, _unused, url) { location.href = String(url); },
    replaceState(_state, _unused, url) { location.href = String(url); },
  };
  const ctx = vm.createContext({
    URL,
    location,
    history,
    window: { scrollTo() {} },
    document: {
      querySelectorAll(selector) { return selector === '.view' ? views : nav; },
      querySelector(selector) { return selector === '.product-nav' ? { hidden: false } : null; },
      getElementById(id) { return id === 'days' ? { value: '7' } : null; },
    },
    loadSessions() {},
    setSessionsView() {},
    loadReport() {},
    markFreshStartReceiptsViewed() {},
  });
  vm.runInContext("let sessionsLoadedForDays = '7'; let sessionsViewMode = 'list'; let reportLoadedForDays = '7';", ctx);
  vm.runInContext(extract('showView'), ctx);
  ctx.showView('sessions');
  assert.equal(location.href, 'http://127.0.0.1:8765/?view=sessions');
  assert.equal(views[1].hidden, false);
  assert.equal(views[0].hidden, true);
});

test('a mismatched deep-link hash cannot override the requested view', () => {
  const location = locationState('http://127.0.0.1:8765/?view=sessions#contextHealth');
  const target = { closest: () => ({ id: 'view-watch' }) };
  let shown = null;
  const ctx = vm.createContext({
    URL,
    location,
    history: { replaceState(_state, _unused, url) { location.href = String(url); } },
    document: { getElementById: id => id === 'contextHealth' ? target : null },
    showView(view) { shown = view; },
  });
  vm.runInContext(extract('applyHashTarget'), ctx);
  assert.equal(ctx.applyHashTarget('sessions'), null);
  assert.equal(location.href, 'http://127.0.0.1:8765/?view=sessions');
  assert.equal(shown, null);
});

test('session hero and project row visibly distinguish cumulative token scope', () => {
  const ctx = vm.createContext({
    esc: value => String(value),
    renderIdentityStrip: () => '',
    confidenceLabel: () => ({ tone: 'unknown', label: 'Local metadata' }),
    sessionStatePill: () => '',
    outcomeEvidencePill: () => '',
  });
  vm.runInContext(extract('renderSessionHero'), ctx);
  vm.runInContext(extract('renderProjectSessionRow'), ctx);
  const session = {
    session_id: 'codex-thread', tool: 'codex-cli', model: 'codex',
    tokens_label: '403.8M', tokens_scope_label: 'Cumulative thread total',
  };
  assert.match(ctx.renderSessionHero(session), /Cumulative thread total/);
  assert.match(ctx.renderProjectSessionRow(session), /Cumulative thread total/);
});

test('320px contracts wrap drawer identity content and stack session actions', () => {
  assert.match(css, /\.session-hero \.session-meta \{[^}]*overflow-wrap: anywhere/);
  assert.match(css, /\.session-review-shell \{[^}]*max-width: 100%[^}]*min-width: 0/);
  assert.match(css, /\.sessions-table \.row-action \{ width: 100%; \}/);
  assert.match(css, /\.session-id-chip \{ max-width: 100%; white-space: normal; overflow-wrap: anywhere; \}/);
});
