const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const source = readFileSync(require('node:path').join(__dirname, '../aiwatcher_cli/web/index.js'), 'utf8');
function functionSource(name) {
  const start = source.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  const rest = source.slice(start);
  const next = rest.slice(rest.indexOf('{')).search(/\n(?:async function |function |let |const |class )/);
  return rest.slice(0, rest.indexOf('{') + next);
}

// A panel stands in for one of the mounts load() rewrites. Assigning innerHTML
// replaces its children outright, exactly as the browser does, so a restored
// open state has to survive losing every node it was read from.
function block(summaryText, attrs) {
  const summary = {textContent: summaryText};
  return {
    open: false,
    parentElement: null,
    getAttribute: name => (attrs && attrs[name]) || null,
    querySelector: sel => (sel === 'summary' ? summary : null),
  };
}
function card(optimizeId, children) {
  const node = {id: '', getAttribute: name => (name === 'data-optimize-id' ? optimizeId : null)};
  for (const child of children) child.parentElement = node;
  return node;
}
function panel(id) {
  const p = {id, children: [], markup: new Map(), html: ''};
  p.querySelectorAll = sel => (sel === 'details' ? p.children : []);
  Object.defineProperty(p, 'innerHTML', {
    get: () => p.html,
    set(value) {
      p.html = value;
      p.children = (p.markup.get(value) || (() => []))();
      for (const child of p.children) if (!child.parentElement) child.parentElement = p;
    },
  });
  return p;
}
function harness() {
  const context = vm.createContext({});
  vm.runInContext('const openDetailsByPanel = new Map();', context);
  for (const name of ['detailsStateEntries', 'setPanelHtml']) vm.runInContext(functionSource(name), context);
  return context;
}

test('a block left open survives the refresh that rebuilds its panel', () => {
  const ctx = harness(), node = panel('optimizeWorkspaceBody');
  node.markup.set('evidence', () => [block('Inspect 2 exact sessions and ownership')]);
  ctx.setPanelHtml(node, 'evidence');
  node.children[0].open = true;
  ctx.setPanelHtml(node, 'evidence');
  assert.equal(node.children[0].open, true, 'the rebuilt block should still be open');
});

test('a block the user never opened stays shut', () => {
  const ctx = harness(), node = panel('optimizeWorkspaceBody');
  node.markup.set('evidence', () => [block('Inspect 2 exact sessions and ownership')]);
  ctx.setPanelHtml(node, 'evidence');
  ctx.setPanelHtml(node, 'evidence');
  assert.equal(node.children[0].open, false);
});

test('closing a block is remembered too', () => {
  const ctx = harness(), node = panel('optimizeWorkspaceBody');
  node.markup.set('evidence', () => [block('Inspect 2 exact sessions and ownership')]);
  ctx.setPanelHtml(node, 'evidence');
  node.children[0].open = true;
  ctx.setPanelHtml(node, 'evidence');
  node.children[0].open = false;
  ctx.setPanelHtml(node, 'evidence');
  assert.equal(node.children[0].open, false, 'a closed block must not spring back open');
});

test('a count that ticks does not read as a different block', () => {
  const ctx = harness(), node = panel('optimizeWorkspaceBody');
  node.markup.set('two', () => [block('Inspect 2 exact sessions and ownership')]);
  node.markup.set('three', () => [block('Inspect 3 exact sessions and ownership')]);
  ctx.setPanelHtml(node, 'two');
  node.children[0].open = true;
  ctx.setPanelHtml(node, 'three');
  assert.equal(node.children[0].open, true, 'the same block with one more session should stay open');
});

test('identical summaries on different cards do not open each other', () => {
  const ctx = harness(), node = panel('optimizeWorkspaceBody');
  const build = () => {
    const a = block('More evidence'), b = block('More evidence');
    card('sessions:alpha', [a]);
    card('worktree:beta', [b]);
    return [a, b];
  };
  node.markup.set('cards', build);
  ctx.setPanelHtml(node, 'cards');
  node.children[1].open = true;
  ctx.setPanelHtml(node, 'cards');
  assert.deepEqual(node.children.map(c => c.open), [false, true]);
});

test('a rebuild status render does not discard the open choice', () => {
  const ctx = harness(), node = panel('optimizeWorkspaceBody');
  node.markup.set('evidence', () => [block('Inspect 2 exact sessions and ownership')]);
  node.markup.set('updating', () => []);
  ctx.setPanelHtml(node, 'evidence');
  node.children[0].open = true;
  // Optimize shows "Updating Optimize evidence…" with no blocks while its cache
  // rebuilds; that must not read as "the user closed everything".
  ctx.setPanelHtml(node, 'updating');
  assert.equal(node.children.length, 0);
  ctx.setPanelHtml(node, 'evidence');
  assert.equal(node.children[0].open, true, 'the choice should outlive the loading state');
});

test('panels do not restore each other', () => {
  const ctx = harness(), a = panel('optimizeWorkspaceBody'), b = panel('improveResults');
  const make = () => [block('More evidence')];
  a.markup.set('x', make);
  b.markup.set('x', make);
  ctx.setPanelHtml(a, 'x');
  a.children[0].open = true;
  ctx.setPanelHtml(a, 'x');
  ctx.setPanelHtml(b, 'x');
  assert.equal(a.children[0].open, true);
  assert.equal(b.children[0].open, false, 'an unrelated panel must not inherit the choice');
});

test('a missing mount is ignored rather than thrown', () => {
  const ctx = harness();
  assert.doesNotThrow(() => ctx.setPanelHtml(null, 'anything'));
});
