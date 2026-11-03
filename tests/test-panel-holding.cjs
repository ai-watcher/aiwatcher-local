// What a panel does while the evidence behind it is being rebuilt.
//
// Measured before this existed: one forced refresh took changeRows from 5,005
// characters to 60 and insightFeed from 8,521 to 534, for as long as the
// rebuild ran. The panel has to keep the last completed answer instead, say so,
// and refuse to let anyone act on it until the new evidence lands.
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

function button(disabled = false) {
  const attrs = {};
  return {
    disabled,
    tagName: 'BUTTON',
    setAttribute: (name, value) => { attrs[name] = value; },
    removeAttribute: name => { delete attrs[name]; },
    getAttribute: name => (name in attrs ? attrs[name] : null),
    attrs,
  };
}

// Only the shape setPanelHtmlHolding and markPanelUpdating actually touch.
function panel(id, text, buttons = []) {
  const attrs = {};
  const node = {
    id,
    html: '',
    textContent: text,
    buttons,
    banner: null,
    setAttribute: (name, value) => { attrs[name] = value; },
    removeAttribute: name => { delete attrs[name]; },
    getAttribute: name => (name in attrs ? attrs[name] : null),
    prepend: child => { child.remove = () => { node.banner = null; }; node.banner = child; },
    querySelector: sel => (sel === ':scope > .panel-updating' ? node.banner : null),
    querySelectorAll: sel => {
      if (sel === 'button:not([disabled])') return node.buttons.filter(b => !b.disabled);
      if (sel === '[data-updating-disabled]') return node.buttons.filter(b => b.getAttribute('data-updating-disabled'));
      return [];
    },
    attrs,
  };
  Object.defineProperty(node, 'innerHTML', {
    get: () => node.html,
    set(value) { node.html = value; node.textContent = value; },
  });
  return node;
}

function harness() {
  const context = vm.createContext({
    document: {
      // Serves both callers: the throwaway node used to measure an incoming
      // render, and the banner element.
      createElement: () => {
        const attrs = {};
        const el = {
          textContent: '',
          className: '',
          setAttribute: (name, value) => { attrs[name] = value; },
          getAttribute: name => (name in attrs ? attrs[name] : null),
          attrs,
        };
        Object.defineProperty(el, 'innerHTML', {set(value) { el.textContent = value; }});
        return el;
      },
    },
  });
  vm.runInContext('const openDetailsByPanel = new Map();', context);
  vm.runInContext(`const PANEL_UPDATING_MIN_CHARS = ${/PANEL_UPDATING_MIN_CHARS = (\d+)/.exec(source)[1]};`, context);
  vm.runInContext(`const PANEL_REBUILD_KEEP_RATIO = ${/PANEL_REBUILD_KEEP_RATIO = ([\d.]+)/.exec(source)[1]};`, context);
  for (const name of ['detailsStateEntries', 'setPanelHtml', 'markPanelUpdating', 'setPanelHtmlHolding', 'summaryIsRebuilding']) {
    vm.runInContext(functionSource(name), context);
  }
  return context;
}

const LONG = 'x'.repeat(5000);

test('a rebuild that answers with nothing does not empty the panel', () => {
  const ctx = harness();
  const node = panel('changeRows', LONG, [button()]);
  ctx.setPanelHtmlHolding(node, '', true);
  assert.equal(node.textContent, LONG, 'the last completed answer must stay on screen');
  assert.equal(node.getAttribute('aria-busy'), 'true');
  assert.ok(node.banner, 'the reader has to be told the panel is not current');
});

test('controls are disabled while the panel is stale, and only those', () => {
  const ctx = harness();
  const mine = button();
  const alreadyOff = button(true);
  const node = panel('changeRows', LONG, [mine, alreadyOff]);

  ctx.setPanelHtmlHolding(node, '', true);
  assert.equal(mine.disabled, true, 'stale evidence must not be actionable');
  assert.equal(mine.getAttribute('data-updating-disabled'), '1');
  assert.equal(alreadyOff.getAttribute('data-updating-disabled'), null,
    'a button something else disabled is not ours to re-enable');

  ctx.setPanelHtmlHolding(node, LONG, false);
  assert.equal(mine.disabled, false, 'the control comes back when the evidence does');
  assert.equal(alreadyOff.disabled, true, 'and the other one is left exactly as it was');
  assert.equal(node.getAttribute('aria-busy'), null);
  assert.equal(node.banner, null);
});

test('a rebuild carrying real content still replaces the panel', () => {
  const ctx = harness();
  const node = panel('insightFeed', LONG);
  ctx.setPanelHtmlHolding(node, 'y'.repeat(4000), true);
  assert.equal(node.textContent, 'y'.repeat(4000), 'news during a rebuild is still news');
  assert.equal(node.banner, null);
});

test('an empty panel shows what arrives, because there is nothing to hold', () => {
  const ctx = harness();
  const node = panel('insightFeed', '');
  ctx.setPanelHtmlHolding(node, 'first answer', true);
  assert.equal(node.textContent, 'first answer');
});

test('a panel too small to label is held without a banner over it', () => {
  const ctx = harness();
  const node = panel('changeTotals', 'x'.repeat(200));
  ctx.setPanelHtmlHolding(node, '', true);
  assert.equal(node.textContent, 'x'.repeat(200), 'small panels are held too');
  assert.equal(node.banner, null, 'a notice taller than its panel is noise');
});

test('nothing is held when the cache is not rebuilding', () => {
  const ctx = harness();
  const node = panel('changeRows', LONG);
  ctx.setPanelHtmlHolding(node, '', false);
  assert.equal(node.textContent, '', 'an empty answer with fresh evidence is the answer');
});

test('the rebuild test reads every signal the cache uses', () => {
  const ctx = harness();
  assert.equal(ctx.summaryIsRebuilding({refreshing: true}), true);
  for (const status of ['building', 'refreshing', 'stale']) {
    assert.equal(ctx.summaryIsRebuilding({status}), true, `${status} is a rebuild`);
  }
  assert.equal(ctx.summaryIsRebuilding({status: 'ready'}), false);
  assert.equal(ctx.summaryIsRebuilding(null), false);
});
