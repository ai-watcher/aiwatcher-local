const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const js = fs.readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.html'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.css'), 'utf8');

function extract(name) {
  const start = js.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1, `missing ${name}`);
  const rest = js.slice(start);
  const brace = rest.indexOf('{');
  const end = rest.slice(brace).search(/\n(?:async function |function |let |const |class |\(async )/);
  return end < 0 ? rest : rest.slice(0, brace + end);
}

function review(status = 'candidate') {
  return {
    receipt_id: `receipt-${status}`,
    ready: status === 'confirmed',
    event: { status, kind: status === 'confirmed' ? 'push' : 'explicit_review', remote_ref: 'feature' },
    snapshot: {
      branch: 'feature', head_sha: 'a'.repeat(40), commit_shas: ['a'.repeat(40)],
      changed_files: ['src/app.py'], changed_file_count: 1, lines_added: 4, lines_removed: 1,
    },
    objective_label: status === 'confirmed' ? 'Objective confirmed; text is not retained' : 'Objective unavailable',
    verifications: [
      { runner: 'unit tests', status: 'passed', scope: 'project_default', exact_state: true },
      { runner: 'old tests', status: 'passed', scope: 'project_default', exact_state: false },
    ],
    workflow: { session_count: 1, user_turns: 3, model_calls: 5, tool_calls: 8 },
    attention: ['Local candidate only; remote push and pull-request status were not confirmed.'],
    summary_text: 'AIWatcher Delivery Review',
  };
}

test('a local candidate never raises the Home ready signal', () => {
  const tile = { hidden: false, innerHTML: 'stale', className: '' };
  const ctx = vm.createContext({
    document: { getElementById: id => id === 'deliveryReviewTile' ? tile : null },
    esc: value => String(value), jsArg: value => JSON.stringify(value),
    deliveryStatusLabel: row => row.event.status,
  });
  vm.runInContext(extract('renderDeliveryReviewTile'), ctx);
  ctx.renderDeliveryReviewTile(review('candidate'));
  assert.equal(tile.hidden, true);
  assert.equal(tile.innerHTML, '');

  ctx.renderDeliveryReviewTile(review('confirmed'));
  assert.equal(tile.hidden, false);
  assert.match(tile.innerHTML, /Delivery review ready/);
  assert.match(tile.innerHTML, /1 commit/);
});

test('review cards distinguish exact verification from stale checks', () => {
  const ctx = vm.createContext({
    esc: value => String(value), jsArg: value => JSON.stringify(value),
    deliveryStatusLabel: row => row.event.status === 'confirmed' ? 'Push confirmed' : 'Local candidate',
    deliveryDestination: () => 'feature',
  });
  vm.runInContext(extract('deliveryReviewCard'), ctx);
  const card = ctx.deliveryReviewCard(review('confirmed'));
  assert.match(card, /Push confirmed/);
  assert.match(card, /unit tests/);
  assert.doesNotMatch(card, /old tests/);
  assert.match(card, /Objective confirmed; text is not retained/);
  assert.match(card, /src\/app.py/);
});

test('loading Prove marks confirmed ready evidence but not candidates', async () => {
  const confirmed = review('confirmed');
  const candidate = review('candidate');
  const marked = [];
  const nodes = {
    deliveryReviews: { hidden: true },
    deliveryReviewBody: { innerHTML: '' },
  };
  const ctx = vm.createContext({
    URLSearchParams,
    location: { search: '' },
    fetch: async () => ({ ok: true, json: async () => ({ reviews: [confirmed, candidate], latest_ready: confirmed }) }),
    deliveryReviewLauncher: () => '', deliveryReviewCard: () => '', esc: value => String(value),
    renderDeliveryReviewTile() {}, focusDeliveryReview() {},
    markDeliveryReviewViewed: async id => marked.push(id),
    document: { getElementById: id => nodes[id] || null },
  });
  vm.runInContext("let deliveryReviewsLoading = false; let deliveryReviewTransient = null; let deliveryReviewsCache = []; let deliveryReviewPendingView = false; let deliveryReviewPrivacy = ''; let deliveryReviewCoverage = '';", ctx);
  vm.runInContext(extract('renderDeliveryReviews'), ctx);
  vm.runInContext(extract('loadDeliveryReviews'), ctx);
  await ctx.loadDeliveryReviews({ markViewed: true });
  assert.deepEqual(marked, ['receipt-confirmed']);
});

test('the real page and responsive stylesheet include the complete delivery surface', () => {
  assert.match(html, /id="deliveryReviewTile"/);
  assert.match(html, /id="deliveryReviews"/);
  assert.match(css, /\.delivery-review-create/);
  assert.match(css, /@media \(max-width: 560px\)[\s\S]*\.delivery-review-grid \{ grid-template-columns: 1fr; \}/);
  assert.match(js, /Objective \(kept only for this preview\)/);
  assert.match(js, /event\.status !== 'confirmed'/);
  assert.match(js, /objective\.setSelectionRange\(selectionStart, selectionEnd\)/);
  assert.match(js, /deliveryReviewPendingView = true/);
  assert.match(js, /targetTop - headerBottom - 12/);
});
