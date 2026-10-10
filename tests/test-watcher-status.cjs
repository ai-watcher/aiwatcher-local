const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const js = fs.readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.js'), 'utf8');

function extract(name) {
  const start = js.search(new RegExp(`^function ${name}\\(`, 'm'));
  assert.notEqual(start, -1);
  const rest = js.slice(start);
  const brace = rest.indexOf('{');
  const end = rest.slice(brace).search(/\n(?:async function |function |let |const |class )/);
  return end < 0 ? rest : rest.slice(0, brace + end);
}

function harness() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { className: '', textContent: '', hidden: false });
    return nodes.get(id);
  };
  const ctx = vm.createContext({ document: { getElementById: node } });
  vm.runInContext("let watcherCommand = 'aiwatcher companion start';", ctx);
  vm.runInContext(extract('renderWatcher'), ctx);
  return { ctx, node };
}

test('watcher rendering preserves server status and command safety', () => {
  const h = harness();

  h.ctx.renderWatcher({
    running: true, status: 'running', label: 'Companion running',
    command: 'aiwatcher companion start',
  });
  assert.equal(h.node('watcherPill').textContent, 'Companion running');
  assert.equal(h.node('watcherStart').hidden, true);

  h.ctx.renderWatcher({
    running: false, status: 'stale', label: 'Watcher not recently seen',
    command: 'aiwatcher companion start',
  });
  assert.equal(h.node('watcherPill').textContent, 'Watcher not recently seen');
  assert.equal(h.node('watcherStart').hidden, false);

  h.ctx.renderWatcher({
    running: false, status: 'stopped', label: 'Companion stopped',
    command: 'aiwatcher companion start',
  });
  assert.equal(h.node('watcherPill').textContent, 'Companion stopped');
  assert.equal(h.node('watcherCommandText').textContent, 'aiwatcher companion start');
  assert.equal(h.node('watcherStart').hidden, false);

  h.ctx.renderWatcher({
    running: false, status: 'unknown', label: 'Watcher status unavailable',
  });
  assert.equal(h.node('watcherPill').textContent, 'Watcher status unavailable');
  assert.equal(h.node('watcherStart').hidden, true);
});
