const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const js = fs.readFileSync(path.join(__dirname, '../aiwatcher_cli/web/index.js'), 'utf8');
function extract(name) {
  const start = js.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1);
  const rest = js.slice(start), brace = rest.indexOf('{');
  const end = rest.slice(brace).search(/\n(?:async function |function |let |const |class )/);
  return end < 0 ? rest : rest.slice(0, brace + end);
}
function fixture(tool, id) {
  return { tool, selection_id: tool + ':' + id, session_id: id, project_full: 'C:\\demo\\app',
    active_count: 0, agent_count: 2, status: 'unknown', agents: [
      { agent_id: id, parent_agent_id: null, name: 'Root', status: 'unknown' },
      { agent_id: 'child', parent_agent_id: id, name: 'Child', status: 'unknown' },
    ] };
}
function harness() {
  const nodes = new Map(), pending = [];
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '7', dataset: {}, innerHTML: '', textContent: '', classList: { toggle() {} }, setAttribute() {} });
    return nodes.get(id);
  };
  let focused = 0;
  const agent = { dataset: { agent: 'root' }, focus: () => focused++ };
  const document = { getElementById: node, querySelectorAll: () => [agent], activeElement: agent };
  const ctx = vm.createContext({ document, encodeURIComponent, Date, JSON, Map, Set,
    dateLabel: value => value || 'unknown',
    fetchDashboardJson: () => new Promise((resolve, reject) => pending.push({ resolve, reject })),
  });
  vm.runInContext("let agentHierarchyCache = {sessions: []}; let selectedAgentSessionId = ''; let selectedAgentId = ''; let agentMapMode = 'all'; let agentHierarchyToken = 0; let agentHierarchyLoadedForDays = null; let agentBranchChoices = new Map(); const AGENT_BRANCH_AUTO_COLLAPSE_CHILDREN = 6;", ctx);
  for (const name of ['esc', 'projectName', 'sessionName', 'agentStatusLabel', 'agentEventLabel', 'selectedAgentSession', 'selectAgentSession',
    'visibleAgentNodes', 'agentBranchKey', 'agentBranchHasRunning', 'agentBranchOpen', 'toggleAgentBranch', 'renderAgentBranch', 'renderAgentHierarchy', 'loadAgentHierarchy']) vm.runInContext(extract(name), ctx);
  return { ctx, node, document, pending, focusCount: () => focused };
}
const payload = { available: true, sessions: [fixture('codex-cli', 'root-a'), fixture('codex-cli', 'root-b'), fixture('claude-code', 'root-a')] };

test('tool-qualified selections distinguish same-project and same-id sessions', async () => {
  const h = harness();
  const done = h.ctx.loadAgentHierarchy();
  h.pending[0].resolve(payload);
  await done;
  const options = h.node('agentSessionSelect').innerHTML;
  for (const session of payload.sessions) assert.ok(options.includes(session.selection_id));
  assert.match(options, /root-a/);
  assert.match(options, /root-b/);
  h.ctx.selectAgentSession('claude-code:root-a');
  assert.equal(h.ctx.selectedAgentSession().tool, 'claude-code');
  assert.match(h.node('agentMapBody').innerHTML, /C:\\demo\\app/);
});

test('refresh does not steal focus after user moved to another control', async () => {
  const h = harness();
  const done = h.ctx.loadAgentHierarchy();
  h.document.activeElement = { dataset: {}, name: 'Search input' };
  h.pending[0].resolve(payload);
  await done;
  assert.equal(h.focusCount(), 0);
});

test('refresh preserves agent focus only when rebuilding the still-focused control', async () => {
  const h = harness();
  const first = h.ctx.loadAgentHierarchy();
  h.pending[0].resolve(payload);
  await first;
  assert.equal(h.focusCount(), 1);
  const second = h.ctx.loadAgentHierarchy();
  h.pending[1].resolve(payload);
  await second;
  assert.equal(h.focusCount(), 1);
});

test('running mode does not silently display unknown agents', () => {
  const h = harness();
  vm.runInContext("agentMapMode = 'active'", h.ctx);
  assert.equal(h.ctx.visibleAgentNodes(payload.sessions[0]).length, 0);
});

test('late responses cannot overwrite a newer window', async () => {
  const h = harness();
  const old = h.ctx.loadAgentHierarchy();
  h.node('days').value = '30';
  const fresh = h.ctx.loadAgentHierarchy();
  h.pending[1].resolve(payload);
  await fresh;
  h.pending[0].resolve({ available: false, sessions: [] });
  await old;
  assert.match(h.node('agentSessionSelect').innerHTML, /root-a/);
});

test('failures retain last data with an error, and successful refresh clears it', async () => {
  const h = harness();
  const first = h.ctx.loadAgentHierarchy();
  h.pending[0].resolve(payload);
  await first;
  const failed = h.ctx.loadAgentHierarchy();
  h.pending[1].reject(new Error('offline'));
  await failed;
  assert.match(h.node('agentSessionSelect').innerHTML, /root-a/);
  assert.equal(h.node('agentMapStatus').textContent, 'offline');
  const recovered = h.ctx.loadAgentHierarchy();
  h.pending[2].resolve(payload);
  await recovered;
  assert.match(h.node('agentMapStatus').textContent, /^Updated/);
});

test('deep trees are clipped rather than overflowing the stack', () => {
  const h = harness();
  const nodes = Array.from({ length: 1000 }, (_, i) => ({ agent_id: String(i), parent_agent_id: i ? String(i - 1) : null, status: 'unknown' }));
  assert.match(h.ctx.renderAgentBranch(nodes[0], nodes), /Deeper relationships omitted/);
});

test('local metadata is escaped before rendering', async () => {
  const h = harness();
  const session = fixture('claude-code', '<script>secret</script>');
  const done = h.ctx.loadAgentHierarchy();
  h.pending[0].resolve({ available: true, sessions: [session] });
  await done;
  assert.ok(!h.node('agentSessionSelect').innerHTML.includes('<script>'));
  assert.ok(!h.node('agentMapBody').innerHTML.includes('<script>'));
});

test('a named chat shows its name beside the id, never instead of it', async () => {
  const h = harness();
  const named = fixture('claude-code', 'root-a');
  named.session_title = 'Context health calibration';
  const done = h.ctx.loadAgentHierarchy();
  h.pending[0].resolve({ available: true, sessions: [named] });
  await done;
  const body = h.node('agentMapBody').innerHTML;
  assert.match(body, /Context health calibration/);
  assert.match(body, /root-a/);
  const select = h.node('agentSessionSelect').innerHTML;
  assert.match(select, /Context health calibration/);
  assert.match(select, /root-a/);
});

test('an unnamed chat is labelled by its id alone', async () => {
  const h = harness();
  const done = h.ctx.loadAgentHierarchy();
  h.pending[0].resolve({ available: true, sessions: [fixture('claude-code', 'root-a')] });
  await done;
  assert.match(h.node('agentMapBody').innerHTML, /Session: root-a/);
});

test('a long chat name is clipped so the id stays visible in the select', async () => {
  const h = harness();
  const named = fixture('claude-code', 'root-a');
  named.session_title = 'x'.repeat(80);
  const done = h.ctx.loadAgentHierarchy();
  h.pending[0].resolve({ available: true, sessions: [named] });
  await done;
  const select = h.node('agentSessionSelect').innerHTML;
  assert.match(select, /root-a/);
  assert.ok(!select.includes('x'.repeat(41)));
});

function wideSession(runningChild) {
  const agents = [{ agent_id: 'owner', parent_agent_id: null, name: 'Owner', status: 'unknown' }];
  for (let i = 0; i < 17; i++) agents.push({ agent_id: 'a' + i, parent_agent_id: 'owner', name: 'Agent ' + i,
    status: runningChild && i === 3 ? 'running' : 'unknown' });
  return { tool: 'claude-code', selection_id: 'claude-code:wide', session_id: 'wide', project_full: '/demo',
    active_count: runningChild ? 1 : 0, agent_count: agents.length, status: 'unknown', agents };
}

test('wide branches start collapsed and the reader can open them', () => {
  const h = harness();
  vm.runInContext('agentHierarchyCache = ' + JSON.stringify({ available: true, sessions: [wideSession(false)] }), h.ctx);
  h.ctx.renderAgentHierarchy();
  let html = h.node('agentMapBody').innerHTML;
  assert.doesNotMatch(html, /Agent 16/);
  assert.match(html, /17 subagents collapsed/);
  assert.match(html, /aria-expanded="false"/);
  h.ctx.toggleAgentBranch('owner', true);
  html = h.node('agentMapBody').innerHTML;
  assert.match(html, /Agent 16/);
  assert.match(html, /aria-expanded="true"/);
});

test('a wide branch hiding a running agent starts open', () => {
  const h = harness();
  vm.runInContext('agentHierarchyCache = ' + JSON.stringify({ available: true, sessions: [wideSession(true)] }), h.ctx);
  h.ctx.renderAgentHierarchy();
  assert.match(h.node('agentMapBody').innerHTML, /Agent 16/);
});

test('small branches stay open', async () => {
  const h = harness();
  const done = h.ctx.loadAgentHierarchy();
  h.pending[0].resolve(payload);
  await done;
  assert.match(h.node('agentMapBody').innerHTML, />Child</);
});
