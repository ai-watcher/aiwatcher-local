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
function deferred() {
  let resolve;
  const promise = new Promise(r => resolve = r);
  return {promise, resolve};
}
function harness() {
  const nodes = new Map(), requests = [], writes = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value: 7, shows: 0, closes: 0,
      showModal() {this.shows++;}, close() {this.closes++;}, scrollIntoView() {}});
    return nodes.get(id);
  };
  const context = vm.createContext({
    document: {getElementById: node, querySelectorAll: () => []},
    currentData: {optimize: {candidates: []}},
    esc: String, renderOptimizeReviewEvidence: item => `identity:${item.id}`,
    showToast() {}, window: {setTimeout() {}},
    postJson: (url, payload) => { const d = deferred(); requests.push({url, payload, ...d}); return d.promise; },
    fetch: async (url, options) => {writes.push({url, payload: JSON.parse(options.body)}); return {ok: true};},
  });
  vm.runInContext('let pendingOptimizeReview = null; let optimizeReviewRequestToken = 0;', context);
  for (const name of ['closeOptimizeReview', 'openOptimizeReview', 'confirmOptimizeReview', 'recordOptimizeDecision']) vm.runInContext(functionSource(name), context);
  return {context, node, requests, writes};
}
test('late review response cannot overwrite a newer selection', async () => {
  const h = harness();
  const a = h.context.openOptimizeReview('a'), b = h.context.openOptimizeReview('b');
  h.requests[1].resolve({id: 'b', title: 'B'}); await b;
  h.requests[0].resolve({id: 'a', title: 'A'}); await a;
  assert.match(h.node('optimizeReviewBody').innerHTML, /identity:b/);
  assert.equal(h.node('optimizeReview').shows, 1);
});
test('cancel cancels a pending summary without recording anything', async () => {
  const h = harness(), pending = h.context.openOptimizeReview('a');
  h.context.closeOptimizeReview();
  h.requests[0].resolve({id: 'a', title: 'A'}); await pending;
  assert.equal(h.node('optimizeReview').shows, 0);
  assert.equal(h.writes.length, 0);
});
test('confirmation saves the exact snapshot as a review receipt, not completion', async () => {
  const h = harness(), pending = h.context.openOptimizeReview('a');
  h.requests[0].resolve({id: 'a', title: 'A', evidence_hash: 'snapshot', project_full: '/test'}); await pending;
  await h.context.confirmOptimizeReview({disabled: false});
  assert.equal(h.writes[0].url, '/api/optimize-decision');
  assert.equal(h.writes[0].payload.candidate_id, 'a');
  assert.equal(h.writes[0].payload.evidence_hash, 'snapshot');
  assert.equal(h.writes[0].payload.decision, 'reviewed');
  assert.equal(h.writes[0].payload.confirmed, true);
  assert.equal(h.node('optimizeReview').closes, 1);
});
