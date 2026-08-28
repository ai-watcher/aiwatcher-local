
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}
function queryParam(name) {
  return new URLSearchParams(window.location.search).get(name);
}
async function copyText(value, label = 'Copied') {
  try {
    await navigator.clipboard.writeText(value || '');
    renderSaved(label);
    return true;
  } catch (error) {
    renderSaved('Copy failed. Open dashboard and copy from the Fresh Start drawer.');
    return false;
  }
}
async function recordDecision(decision, bubble) {
  if (!bubble || !bubble.session_id) return;
  try {
    await fetch('/api/handoff-decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: bubble.session_id,
        decision,
        reason: bubble.reason || bubble.body || '',
        expected_saved_context_tokens: bubble.expected_saved_context_tokens || null,
      })
    });
  } catch (error) {}
}
async function recordAmbientAction(action, snoozeMinutes = null) {
  const fingerprint = queryParam('intervention');
  if (!fingerprint) return;
  const payload = { fingerprint, action, channel: 'overlay' };
  if (snoozeMinutes) payload.snooze_minutes = snoozeMinutes;
  try {
    await fetch('/api/ambient-intervention-action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (error) {}
}
async function loadAmbientIntervention() {
  const fingerprint = queryParam('intervention');
  if (!fingerprint) return null;
  try {
    const res = await fetch(`/api/ambient-intervention?id=${encodeURIComponent(fingerprint)}`);
    if (!res.ok) return null;
    return await res.json();
  } catch (error) {
    return null;
  }
}
function renderSaved(message) {
  document.getElementById('bubble').innerHTML = `<div class="top"><div><h1>${esc(message)}</h1><p>You can close this AIWatcher companion and return to your AI tool.</p></div><span class="badge">saved</span></div>
    <div class="body"><div class="actions"><button class="primary" onclick="window.close()">Close</button><a href="/">Open dashboard</a></div></div>`;
}
async function copyHandoff(bubble, decision) {
  const res = await fetch(`/api/handoff-basic?id=${encodeURIComponent(bubble.session_id)}&target=generic`);
  const capsule = await res.json();
  if (capsule.error) {
    renderSaved(capsule.error);
    return;
  }
  const copied = await copyText(capsule.next_brief || '', 'Fresh Start brief copied');
  if (!copied) return;
  await recordDecision(decision, bubble);
  await recordAmbientAction('acted');
}
async function copyFocusedBrief(bubble, intervention) {
  const brief = `AIWatcher focused continuation

Workspace: ${bubble.project || 'current workspace'}
Observed signal: ${intervention.reason || bubble.reason || 'Execution pressure is elevated.'}

Before using more tools:
- Restate the exact outcome for this checkpoint in one sentence.
- Summarize what is already known and avoid repeating broad discovery.
- Inspect only the smallest relevant files or commands.
- Stop and ask before destructive changes or unrelated cleanup.

Continue only the smallest checkpoint, run the narrowest useful verification, and report what remains.`;
  await recordAmbientAction('acted');
  await copyText(brief, 'Focused next step copied');
}
async function inspectIntervention(bubble) {
  await recordAmbientAction('acted');
  window.location.href = `/?session=${encodeURIComponent(bubble.session_id || '')}`;
}
async function snoozeIntervention() {
  await recordAmbientAction('snooze', 15);
  renderSaved('Snoozed for 15 minutes');
}
async function dismissIntervention() {
  await recordAmbientAction('dismiss');
  renderSaved('Dismissed for this session state');
}
// Which button handler an action wants. This stays here because it names
// functions on this page; the wording does not, and no longer lives here.
const PRIMARY_MODES = {
  return_session: 'inspect',
  recover_loop: 'inspect',
  continue_focused: 'focused',
  fresh_chat: 'fresh_chat',
  switch_tool: 'fresh_chat',
};
function interventionPresentation(bubble, intervention) {
  if (!intervention) {
    return {
      title: bubble.title,
      body: bubble.body,
      severity: bubble.severity,
      primaryLabel: 'Copy Fresh Start brief',
      primaryMode: 'fresh_chat',
    };
  }
  const action = intervention.action || 'fresh_chat';
  return {
    // Title and label come from _PRESENTATIONS via /api/ambient-intervention.
    // The copy that used to sit here was keyed on action rather than signal
    // kind, so it could not tell a velocity spike from a heavy session, and it
    // had no entry at all for a waiting session.
    title: intervention.title || bubble.title || 'AIWatcher found something to review',
    body: intervention.reason || bubble.body,
    severity: intervention.severity || bubble.severity,
    primaryLabel: intervention.primary_label || 'Inspect',
    primaryMode: PRIMARY_MODES[action] || 'inspect',
  };
}
function shortSessionId(value) {
  const text = String(value || '');
  return text.length > 12 ? `${text.slice(0, 8)}...${text.slice(-4)}` : text || 'unknown';
}
function renderBubble(bubble, intervention) {
  const presentation = interventionPresentation(bubble, intervention);
  const runtime = bubble.runtime_attachment || {};
  const identityLabel = runtime.identity_label || runtime.label || 'Local session';
  // Only what was actually observed. An intervention record carries a session
  // id and nothing else, so the old fixed template printed "unknown tool ·
  // unknown surface · unknown workspace · sess-1 / Last activity: unknown" --
  // four claims of ignorance where one identifier is the whole honest answer.
  const identityParts = [
    bubble.tool || runtime.tool,
    runtime.surface,
    bubble.project || runtime.project_path,
    shortSessionId(bubble.session_id || runtime.session_id),
  ].filter(Boolean);
  const last = bubble.updated_at ? new Date(bubble.updated_at).toLocaleString() : '';
  const lastLine = last ? `<br>Last activity: ${esc(last)}` : '';
  // The signal's own sentence is already the subtitle. Printing it again below
  // the identity block read as two findings when there is one.
  const detail = bubble.reason
    || (intervention ? '' : 'Use a Fresh Start brief to preserve the outcome without carrying the full chat history.');
  const detailLine = detail && detail !== presentation.body ? `<p>${esc(detail)}</p>` : '';
  const tags = (bubble.tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join('');
  document.getElementById('bubble').innerHTML = `<div class="top">
    <div><h1>${esc(presentation.title || 'AIWatcher found something to review')}</h1><p>${esc(presentation.body || 'Review the local evidence before continuing.')}</p></div>
    <span class="badge">${esc(presentation.severity || 'warning')}</span>
  </div>
  <div class="body">
    <div class="identity"><strong>${esc(identityLabel)}</strong><br>${identityParts.map(esc).join(' · ')}${lastLine}</div>
    <div class="tags">${tags}</div>
    ${detailLine}
    <div class="actions">
      <button class="primary" id="primaryAction">${esc(presentation.primaryLabel)}</button>
      ${presentation.primaryMode === 'inspect' ? '' : '<button id="inspect">Inspect</button>'}
      <button id="snooze">Snooze 15 min</button>
      <button id="dismiss">Dismiss</button>
    </div>
  </div>
  <div class="foot">Local-only. Prompt/source content is not stored in this decision.</div>`;
  document.getElementById('primaryAction').onclick = () => {
    if (presentation.primaryMode === 'inspect') return inspectIntervention(bubble);
    if (presentation.primaryMode === 'focused') return copyFocusedBrief(bubble, intervention || {});
    return copyHandoff(bubble, 'copy_handoff');
  };
  const inspect = document.getElementById('inspect');
  if (inspect) inspect.onclick = () => inspectIntervention(bubble);
  document.getElementById('snooze').onclick = snoozeIntervention;
  document.getElementById('dismiss').onclick = dismissIntervention;
}
async function load() {
  const wanted = queryParam('session');
  const intervention = await loadAmbientIntervention();
  const res = await fetch('/api/summary?days=7');
  const data = await res.json();
  let bubble = data.handoff_bubble;
  if (wanted && (!bubble || bubble.session_id !== wanted)) {
    const health = (data.context_health || []).find(row => row.session_id === wanted);
    if (health) {
      const saved = health.estimated_replayed_context_label || 'context';
      bubble = {
        session_id: health.session_id,
        project: health.project,
        tool: health.tool,
        severity: health.severity,
        title: `Fresh Start recommended before ~${saved} tokens of replayed context compounds`,
        body: health.recommendation || 'This session is getting heavy. Use a Fresh Start brief before continuing.',
        reason: health.recommendation || 'Context pressure is elevated.',
        expected_saved_context_tokens: health.estimated_replayed_context_tokens || null,
        tags: [`${health.latest_turn_tokens} tokens/turn`, `${saved} replayed`].concat(
          health.bloat_measurable ? [`${health.bloat_label} of spend replayed`] : []),
      };
    }
  }
  // A loop, a velocity spike, runway pressure, or a waiting session has no
  // Fresh Start bubble behind it -- handoff_bubble is built from context
  // health alone. Without this the page fell through to "No Fresh Start needed
  // right now", which is the opposite of the signal that opened the window.
  if (!bubble && intervention) {
    bubble = {
      session_id: intervention.session_id || wanted || '',
      severity: intervention.severity || 'warning',
      body: intervention.reason || '',
      reason: intervention.reason || '',
    };
  }
  if (wanted && (!bubble || bubble.session_id !== wanted) && data.context_health_status === 'pending') {
    document.getElementById('bubble').innerHTML = `<div class="top"><div><h1>Loading this session evidence</h1><p>AIWatcher is still building the local context-health index for this deep link.</p></div><span class="badge">checking</span></div>
      <div class="body"><div class="actions"><a class="primary" href="/?session=${encodeURIComponent(wanted)}">Open dashboard</a><button onclick="window.close()">Close</button></div></div>`;
    window.setTimeout(load, 1600);
    return;
  }
  if (!bubble) {
    document.getElementById('bubble').innerHTML = `<div class="top"><div><h1>No Fresh Start needed right now</h1><p>AIWatcher did not find warning or critical context pressure in the current local window.</p></div><span class="badge">healthy</span></div>
      <div class="body"><div class="actions"><a class="primary" href="/">Open dashboard</a><button onclick="window.close()">Close</button></div></div>`;
    return;
  }
  await recordAmbientAction('displayed');
  renderBubble(bubble, intervention);
}
load();
