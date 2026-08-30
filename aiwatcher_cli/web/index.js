// The window the tile sparklines were last drawn for.
let tileSparkWindow = null;

// These are product names, not ids. The buttons used to render the raw target
// key, so the row read "Generic claude codex cursor vscode".
const HANDOFF_TARGET_LABELS = {
  generic: 'Generic',
  claude: 'Claude Code',
  codex: 'Codex',
  cursor: 'Cursor',
  vscode: 'VS Code',
};
const HANDOFF_TYPES = [
  { id: 'coding', label: 'Coding continuation' },
  { id: 'product', label: 'Product/strategy continuation' },
  { id: 'review', label: 'Review continuation' },
  { id: 'bugbash', label: 'Bug bash continuation' },
  { id: 'investigation', label: 'Investigation continuation' },
  { id: 'general', label: 'General work continuation' },
];
function maxValue(rows, key = 'api_value_usd') {
  return Math.max(0.000001, ...rows.map(r => Number(r[key] || 0)));
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[ch]));
}
function jsArg(value) {
  return `'${String(value ?? '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\r/g, '\\r')
    .replace(/\n/g, '\\n')}'`;
}
let receiptCache = [];
function riskFlow(receipt) {
  const original = `<span class="risk-chip ${esc(receipt.original_risk)}">${esc(receipt.original_risk)} · ${esc(riskScore(receipt.original_score))}</span>`;
  if (receipt.selected_score === null || receipt.selected_score === undefined) return `<div class="risk-flow">${original}</div>`;
  return `<div class="risk-flow">${original}<span class="risk-arrow">→</span><span class="risk-chip ${esc(receipt.selected_risk || 'low')}">${esc(receipt.selected_risk || 'unknown')} · ${esc(riskScore(receipt.selected_score))}</span><span class="pill">${esc(receipt.risk_points_reduced || 0)} points reduced</span></div>`;
}
function predictedStats(receipt) {
  const p = receipt.predicted || {};
  if (!p.available) return `<div class="empty">Savings estimate unavailable. ${esc(p.basis || 'More comparable local history is required.')}</div>`;
  return `<div class="mini-grid">
    <div class="mini"><span class="label">Predicted token savings</span><strong>${esc(p.tokens_label || '—')}</strong></div>
    <div class="mini"><span class="label">Model calls avoided</span><strong>${esc(p.model_calls_label || '—')}</strong></div>
    <div class="mini"><span class="label">Tool calls avoided</span><strong>${esc(p.tool_calls_label || '—')}</strong></div>
    <div class="mini"><span class="label">API-equivalent savings</span><strong>${esc(p.api_value_label || '—')}</strong></div>
  </div><p class="receipt-note">${esc(p.confidence || 'unknown')} confidence · ${esc(p.basis || '')}</p>`;
}
function actionRow(item) {
  const meta = (item.meta || []).map(value => `<span class="pill">${esc(value)}</span>`).join('');
  return `<div class="action-row ${esc(item.severity || 'medium')}">
    <div>
      <div class="action-title"><span>${esc(item.title)}</span><span class="pill">${esc(item.evidence || 'local evidence')}</span></div>
      <p>${esc(item.body)}</p>
      ${meta ? `<div class="action-meta">${meta}</div>` : ''}
    </div>
    <div class="actions">${(item.actions || []).map(action => `<button class="${esc(action.primary ? 'btn-primary' : 'btn-quiet')}" onclick="${esc(action.onclick)}">${esc(action.label)}</button>`).join('')}</div>
  </div>`;
}
function renderReceiptRows(receipts) {
  if (!receipts.length) return '<tr><td colspan="6"><div class="empty">No interventions recorded in this window.</div></td></tr>';
  return receipts.map(receipt => `<tr class="clickable" onclick="openReceipt('${esc(receipt.id)}')">
    <td>${esc(dateLabel(receipt.created_at))}</td>
    <td><strong>${esc(receipt.tool)}</strong><br><span class="sub" title="${esc(projectTitle(receipt))}">${esc(projectName(receipt))}</span></td>
    <td>${esc(receipt.decision_label)}</td>
    <td>${esc(riskScore(receipt.original_score))} → ${esc(riskScore(receipt.selected_score))}</td>
    <td>${esc(receipt.outcome || receipt.session_status)}</td>
    <td><button class="row-action">Review</button></td>
  </tr>`).join('');
}
function handoffDecisionLabel(value) {
  const labels = {
    new_chat: 'Prepared Fresh Start',
    continue_here: 'Continued in current session',
    copy_handoff: 'Copied Fresh Start brief',
    dismissed: 'Dismissed'
  };
  return labels[value] || value || 'unknown';
}
function renderHandoffDecisionRows(decisions) {
  if (!decisions.length) return '<tr><td colspan="6"><div class="empty">No Fresh Start receipts recorded yet.</div></td></tr>';
  return decisions.map(decision => `<tr>
    <td>${esc(dateLabel(decision.created_at))}</td>
    <td>${esc(handoffDecisionLabel(decision.decision))}</td>
    <td>${esc(decision.expected_saved_context_label ? `~${decision.expected_saved_context_label}` : '—')}</td>
    <td><strong>${esc(decision.proof_status || 'Proof pending')}</strong>${(() => {
      // The generic pending reason was the same 22 words on every pending row.
      // It is stated once in the card subtitle now; the cell keeps anything
      // specific to that row.
      const specific = decision.observed_followup ? decision.observed_followup.label
        : (decision.proof_status && decision.proof_status !== 'Proof pending'
            ? decision.proof_reason || decision.outcome || decision.inferred_outcome || decision.proof_confidence
            : decision.outcome || decision.inferred_outcome);
      return specific ? `<br><span class="sub">${esc(specific)}</span>` : '';
    })()}${decision.proof_evidence ? `<br><span class="sub">${esc(decision.proof_evidence.label)} · ${esc(decision.proof_evidence.commits)} commits · ${esc(decision.proof_evidence.tests)} tests</span>` : ''}</td>
    <td><span class="sub">source</span> ${esc(decision.source_session_id || decision.session_id || 'unknown')}<br><span class="sub">next</span> ${esc(decision.next_session_id || 'waiting')}</td>
    <td>${decision.next_session_id ? `<button class="row-action" onclick="selectSession('${esc(decision.next_session_id)}')">Inspect next</button>` : decision.source_session_id ? `<button class="row-action" onclick="selectSession('${esc(decision.source_session_id)}')">Inspect source</button>` : ''}</td>
  </tr>`).join('');
}
function openReceipt(receiptId) {
  const receipt = receiptCache.find(item => item.id === receiptId);
  if (!receipt) return;
  openDrawer('Intervention receipt');
  const actual = receipt.actual
    ? `<section class="detail-section"><h3>Observed session</h3>
       <div class="mini-grid">
         <div class="mini"><span class="label">Tokens</span><strong>${esc(receipt.actual.tokens_label)}</strong></div>
         <div class="mini"><span class="label">Model calls</span><strong>${esc(receipt.actual.model_calls)}</strong></div>
         <div class="mini"><span class="label">Tool calls</span><strong>${esc(receipt.actual.tool_calls)}</strong></div>
         <div class="mini"><span class="label">API value</span><strong>${esc(receipt.actual.api_value_label)}</strong></div>
       </div>${receipt.actual.reliable ? '' : `<p>${esc(receipt.actual.reason)}</p>`}
       ${receipt.session_id ? `<button class="btn-quiet" onclick="selectSession('${esc(receipt.session_id)}')">Open resulting session</button>` : ''}
      </section>`
    : `<section class="detail-section"><h3>Observed session</h3><div class="empty">Waiting for a matching local session. Refresh after the agent finishes.</div></section>`;
  const inferred = receipt.inferred
    ? `<section class="detail-section"><h3>${esc(receipt.inferred.label)}</h3>
       <div class="mini-grid">
         <div class="mini"><span class="label">Tokens below baseline</span><strong>${esc(receipt.inferred.tokens_label || '—')}</strong></div>
         <div class="mini"><span class="label">Model calls</span><strong>${esc(receipt.inferred.model_calls ?? '—')}</strong></div>
         <div class="mini"><span class="label">Tool calls</span><strong>${esc(receipt.inferred.tool_calls ?? '—')}</strong></div>
         <div class="mini"><span class="label">API-equivalent</span><strong>${esc(receipt.inferred.api_value_label || '—')}</strong></div>
       </div><p>${esc(receipt.inferred.disclaimer)}</p></section>`
    : '';
  setDrawerContent(`<section class="detail-section">
    <h2>${esc(receipt.decision_label)}</h2>
    <p>${esc(receipt.tool)} · ${esc(receipt.project)} · ${esc(dateLabel(receipt.created_at))}</p>
    ${riskFlow(receipt)}
    <div class="pill-row"><span class="pill">${esc(receipt.session_status)}</span>${outcomePill(receipt.outcome)}</div>
  </section><section class="detail-section"><h3>Predicted before execution</h3>${predictedStats(receipt)}</section>${actual}${inferred}
  <section class="detail-section"><h3>Privacy evidence</h3><p>Prompt text is not stored. This receipt contains hashes, policy findings, aggregate usage, and your outcome only.</p></section>`);
}
// Feedback lands on the button that was pressed. The toast renders bottom-right,
// which on this layout is up to 750px from the control -- far enough that a
// successful copy read as nothing happening, and two copy buttons fired no
// toast at all, so "no visible feedback" did not reliably mean failure.
function flashCopied(button, text = 'Copied') {
  if (!button || button.dataset.copyRestore !== undefined) return false;
  button.dataset.copyRestore = button.textContent;
  button.textContent = text;
  button.classList.add('copied');
  window.setTimeout(() => {
    button.textContent = button.dataset.copyRestore;
    delete button.dataset.copyRestore;
    button.classList.remove('copied');
  }, 2000);
  return true;
}

// The pressed button is recorded on the way down, because window.event is only
// set while a synchronous handler runs -- and several copy paths await a fetch
// first (startFreshFromBubble builds the brief before copying it), by which
// time it is gone. Capture phase, so it fires before the handler.
let lastPressedButton = null;
document.addEventListener('click', event => {
  const node = event.target && event.target.closest ? event.target.closest('button') : null;
  lastPressedButton = node || null;
}, true);

function pressedButton() {
  const button = lastPressedButton;
  // Ignore a button that has since been re-rendered out of the document.
  return button && button.isConnected ? button : null;
}

async function copyText(value, label = 'Copied') {
  const button = pressedButton();
  try {
    await navigator.clipboard.writeText(value || '');
    // Falls back to the toast when there is no button to write on.
    if (!flashCopied(button)) showToast(label);
    return true;
  } catch (error) {
    flashCopied(button, 'Copy failed');
    showToast('Copy failed. Select the text manually.', 'error');
    return false;
  }
}
async function recordOptimizeDecision(decision, project = '', impact = '', button = null) {
  try {
    const res = await fetch('/api/optimize-decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, project, reason: decision === 'skipped' ? 'User skipped workspace cleanup nudge.' : 'User marked workspace cleanup reviewed.' }),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => ({}));
      throw new Error(payload.error || 'save failed');
    }
    const row = button && button.closest ? button.closest('.action-row') : null;
    if (row) {
      row.style.opacity = '.45';
      row.style.pointerEvents = 'none';
      window.setTimeout(() => row.remove(), 180);
    }
    const reward = document.getElementById('optimizeReward');
    if (reward) {
      reward.hidden = false;
      reward.className = decision === 'skipped' ? 'verdict-card' : 'verdict-card low';
      reward.innerHTML = `<h3>${decision === 'skipped' ? 'Nudge skipped' : 'Review saved'}</h3>
        <p>${decision === 'skipped' ? 'AIWatcher will quiet this Optimize nudge for 3 days.' : 'AIWatcher recorded that you reviewed this item and will quiet it for 3 days.'}</p>
        ${impact ? `<div class="pill-row"><span class="pill">${esc(impact)}</span><span class="pill">No deletion performed</span></div>` : '<div class="pill-row"><span class="pill">No deletion performed</span></div>'}`;
      reward.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    showToast(decision === 'skipped' ? 'Optimize nudge skipped for 3 days' : 'Optimize review saved for 3 days');
  } catch (error) {
    showToast(`Could not save Optimize decision: ${error.message || 'unknown error'}`, 'error');
  }
}
// Scoped to the textarea. It used to wipe the Route result too -- the route,
// risk score, findings, suggestions and paste-ready brief -- with no
// confirmation, from a quiet button sitting 20px from the primary action. An
// accidental press now costs the prompt text and nothing else; the analysis
// stands until the next Plan run replaces it.
function clearPromptCompanion() {
  document.getElementById('promptInput').value = '';
  document.getElementById('promptInput').focus();
}
function renderPlanAction(action) {
  const route = action || {};
  const kind = route.kind || 'continue';
  const primaryUrl = route.primary_url || '';
  const primary = primaryUrl
    ? `<button class="btn-primary" onclick="location.href='${esc(primaryUrl)}'">${esc(route.primary_label || 'Open')}</button>`
    : `<button class="btn-primary" onclick="copyText(document.getElementById('promptBrief').value, 'Execution brief copied — paste it into your AI tool now')">${esc(route.primary_label || 'Copy brief')}</button>`;
  return `<div class="plan-route-card ${esc(kind)}">
    <span class="pill plan-label">${esc(route.label || 'Continue')} · ${esc(route.confidence || 'observed')}</span>
    <h3>${esc(route.title || 'Continue in this chat')}</h3>
    <p>${esc(route.why || 'AIWatcher did not find a reason to interrupt.')}</p>
    <p class="plan-next-step"><strong>Next:</strong> ${esc(route.next_step || 'Continue with a scoped checkpoint.')}</p>
    <div class="copy-row">${primary}</div>
  </div>`;
}
/* The Plan result is three zones, in this order, and the order is the point.
   It used to be one undifferentiated block, which read as though the whole
   thing had been worked out from the prompt. It had not: diffing two unrelated
   prompts produced identical output apart from the tool name, which comes from
   a dropdown. Separating what was derived from what is house advice is the
   honesty fix, and it ships before there is anything to put in zone A. */

// Zone A. Anything actually derived from this specific prompt. Empty until a
// second opinion exists to fill it -- but never silent, because an empty zone
// that says why is what makes the difference between the zones legible.
function derivedZoneShell(chipClass, chipLabel, body) {
  return `<section class="detail-section plan-zone plan-zone-derived" id="planDerivedZone">
    <div class="section-title">
      <div><h3>From your prompt</h3><p>Worked out from what you wrote, not from a template.</p></div>
      <span class="confidence-chip ${esc(chipClass)}">${esc(chipLabel)}</span>
    </div>
    ${body}
  </section>`;
}
function renderDerivedZone(data) {
  const second = data.second_opinion || {};
  if (second.pending) {
    // Spec 4.2: zones B and C are complete and usable throughout, so this waits
    // in place rather than holding the panel. A real run takes about 30s.
    return derivedZoneShell('unknown', 'asking',
      `<p class="zone-empty">Asking your own agent for a second opinion. Zones below are complete already.</p>
       <div class="ai-loading-bar" aria-hidden="true"></div>`);
  }
  const gatePassed = Number(data.score || 0) >= RISK_MEDIUM_AT;
  const reason = second.reason
    || (gatePassed
        ? 'Local signals reached the medium band, but no analyst is configured on this build.'
        : 'Nothing in this prompt matched a signal worth a second opinion.');
  return derivedZoneShell('unknown', 'unavailable', `<p class="zone-empty">${esc(reason)}</p>`);
}
// Zone A, filled. Every heading here maps to one schema field, so a reader can
// see that this was extracted rather than composed -- and an empty field is
// omitted rather than rendered as a heading with nothing under it.
function renderSecondOpinion(second) {
  if (second && second.needs_consent) {
    // Spec 7: asked once per project, with the cost in the question. Nothing
    // has been spawned at this point and nothing will be until this is answered.
    return derivedZoneShell('unknown', 'your call',
      `<p class="zone-empty">${esc(second.reason)}</p>
       <div class="copy-row">
         <button class="btn-primary" data-project="${esc(second.project_path || '')}"
           onclick="answerSecondOpinionConsent(this.dataset.project, true)">Ask my agent (${esc(second.estimate_label || 'about $0.04')})</button>
         <button class="btn-quiet" data-project="${esc(second.project_path || '')}"
           onclick="answerSecondOpinionConsent(this.dataset.project, false)">Not for this project</button>
       </div>`);
  }
  if (!second || !second.available || !second.analysis) {
    const budget = second && second.budget;
    const note = second && second.capped && budget
      ? `<p class="second-opinion-cost mono">${esc(budgetLabel(budget))}</p>`
      : '';
    return derivedZoneShell('unknown', 'unavailable',
      `<p class="zone-empty">${esc((second && second.reason) || 'Second opinion unavailable.')}</p>${note}`);
  }
  const a = second.analysis;
  const list = (items) => `<ul class="prompt-list">${items.map(item => `<li>${esc(item)}</li>`).join('')}</ul>`;
  const removals = (a.removals || []).filter(item => item && item.requested);
  const parts = [
    `<h4>What this asks for</h4><p>${esc(a.outcome)}</p>`,
    `<h4>Done when</h4><p>${esc(a.success_check)}</p>`,
  ];
  if ((a.scope_paths || []).length) {
    parts.push(`<h4>Likely in scope</h4>${list(a.scope_paths)}`);
  }
  if ((a.unresolved_nouns || []).length) {
    // The highest-value line in the block: a noun with no file behind it is
    // usually the thing that makes the task bigger than it looks.
    parts.push(`<h4>Could not locate</h4><div class="pill-row">${a.unresolved_nouns.map(noun =>
      `<span class="signal-chip sensitive">${esc(noun)}</span>`).join('')}</div>`);
  }
  if (removals.length) {
    // Above the guardrails, always: a bullet telling the agent to avoid cleanup
    // must never sit above the deletion the prompt actually asked for.
    parts.push(`<h4>Requested removals</h4><ul class="prompt-list">${removals.map(item =>
      `<li>${esc(item.what)}${item.path ? ` <span class="sub">${esc(item.path)}</span>` : ''}</li>`).join('')}</ul>`);
  }
  if ((a.ambiguities || []).length) {
    parts.push(`<h4>Worth deciding before you send</h4>${list(a.ambiguities)}`);
  }
  parts.push(`<h4>First checkpoint</h4><p>${esc(a.first_checkpoint)}</p>`);
  if (a.dropped_paths) {
    parts.push(`<p class="receipt-note">${esc(a.dropped_paths)} path(s) the analyst named do not exist in this repository and were dropped. Confidence lowered a step.</p>`);
  }
  parts.push(secondOpinionCost(second));
  if (second.budget) {
    // The counter is visible before the cap bites, not only once it has.
    parts.push(`<p class="second-opinion-cost mono">${esc(budgetLabel(second.budget))}</p>`);
  }
  // Everything in this zone came from a model, so the dot says "inferred" --
  // never "observed", which is zone B's word for things read off the machine.
  // Low confidence means more than half the prompt's nouns resolved to nothing,
  // which is nearer to unknown than to inferred, and is the one case where the
  // colour should stop a reader leaning on it.
  const tone = a.confidence === 'low' ? 'unknown' : 'inferred';
  return derivedZoneShell(tone, a.confidence, parts.join(''));
}
// Two ceilings, because one of them cannot bind on every host: Codex prices its
// runs at nothing we can read, so a dollars-only counter would sit at $0.00
// however many analysts had run. Both are shown, and either one stops the next.
function budgetLabel(budget) {
  const spent = `${moneyLabel(budget.spent_usd)} of ${moneyLabel(budget.cap_usd)}`;
  const runs = `${formatCount(budget.runs)} of ${formatCount(budget.run_cap)} runs`;
  return `this month: ${spent} · ${runs}`;
}
// Spec 5. On the screen the money was spent on, not buried in Settings.
//
// Codex reports no machine-readable cost, and printing "$0.00" for it would
// claim the run was free -- true for a subscription, false for anyone on an API
// key, and the local logs cannot tell those two apart. So a host that does not
// price its own runs says so instead of being given a number it never gave us.
function secondOpinionCost(second) {
  const priced = second.cost_reported !== false && Number.isFinite(second.cost_usd);
  const bits = [
    'second opinion',
    second.cli_label || second.cli || 'local agent',
    second.model ? String(second.model) : '',
    Number.isFinite(second.tokens) && second.tokens ? `${formatCount(second.tokens)} tokens` : '',
    priced ? moneyLabel(second.cost_usd) : 'cost not reported by this CLI',
    // Spec 7. "contents: on" is the state the user opted into. With it off the
    // chip says which kind of off: a rule this CLI enforced, or an instruction
    // it was given and followed. Codex cannot deny its own shell, so on that
    // host it is the weaker one and does not get to claim the stronger.
    second.contents ? 'contents: on'
      : (second.contents_enforced ? 'contents: blocked' : 'contents: not requested'),
  ].filter(Boolean);
  return `<p class="second-opinion-cost mono">${esc(bits.join(' · '))}</p>`;
}
// Answering yes spends money, so it is a button the reader pressed, not a
// consequence of having opened the Plan screen.
async function answerSecondOpinionConsent(project, allowed) {
  try {
    await postJson('/api/second-opinion-consent', { project_path: project, allowed });
  } catch (error) {
    showToast('Could not record that choice.', 'error');
    return;
  }
  if (!allowed) {
    const node = document.getElementById('planDerivedZone');
    if (node) node.outerHTML = renderSecondOpinion({ reason: 'Second opinion is turned off for this project.' });
    return;
  }
  const node = document.getElementById('planDerivedZone');
  if (node) node.outerHTML = derivedZoneShell('unknown', 'asking',
    `<p class="zone-empty">Asking your own agent for a second opinion.</p>
     <div class="ai-loading-bar" aria-hidden="true"></div>`);
  loadSecondOpinion(document.getElementById('promptInput').value,
                    document.getElementById('promptTool').value, project);
}
// Spec 4.2 again: if the reader copies the brief before this lands, the copy
// says so rather than quietly omitting a zone they were told was coming.
async function loadSecondOpinion(prompt, tool, cwd) {
  const node = document.getElementById('planDerivedZone');
  if (!node) return;
  let second;
  try {
    second = await postJson('/api/second-opinion', { prompt, tool, cwd });
  } catch (error) {
    second = { available: false, reason: 'Could not reach the local AIWatcher server.' };
  }
  const current = document.getElementById('planDerivedZone');
  // The reader may have re-run Plan while this was in flight.
  if (!current || current !== node) return;
  current.outerHTML = renderSecondOpinion(second);
}

// Zone B. Stage 1: local, lexical and free, so it never has an unavailable
// state. Everything here was read out of the prompt on this machine.
function renderObservedZone(data, riskTone) {
  const signals = data.signals || {};
  // Every point in the score, itemised. The gate that decides whether to pay
  // for a second opinion reads this number, so it has to be accountable.
  const blast = ((data.blast || {}).reasons) || [];
  const removals = (data.removals || []).filter(item => item && item.requested);
  const chips = [
    ...(signals.destructive_verbs || []).map(v => ['destructive', v]),
    ...(signals.sensitive_keywords || []).map(v => ['sensitive', v]),
    ...(signals.breadth_words || []).map(v => ['breadth', v]),
    ...(signals.terse ? [['terse', `${signals.word_count} words`]] : []),
  ];
  return `<section class="detail-section plan-zone risk-card ${esc(riskTone)}">
    <div class="section-title">
      <div><h3>Observed locally</h3><p>Read from your prompt on this machine. No file was opened and nothing was sent anywhere.</p></div>
      <span class="confidence-chip observed">observed</span>
    </div>
    <p><strong>Risk: ${esc(data.risk)} · ${esc(riskScore(data.score))}</strong> <span class="risk-scale">(${esc(RISK_SCALE_NOTE)})</span></p>
    ${blast.length ? `<ul class="prompt-list blast-list">${blast.map(r =>
      `<li><span class="blast-points ${Number(r.points) < 0 ? 'down' : ''}">${Number(r.points) > 0 ? '+' : ''}${esc(r.points)}</span> ${esc(r.text || r.detail || r.signal)}</li>`).join('')}</ul>` : ''}
    <p>${esc(data.impact_label)}</p>
    ${chips.length ? `<div class="pill-row signal-row">${chips.map(([kind, word]) =>
      `<span class="signal-chip ${esc(kind)}">${esc(kind)}: ${esc(word)}</span>`).join('')}</div>` : ''}
    ${removals.length ? `<h4>Requested removals</h4><ul class="prompt-list">${removals.map(item =>
      `<li>${esc(item.what)}${item.path ? ` <span class="sub">${esc(item.path)}</span>` : ''}</li>`).join('')}</ul>` : ''}
    <h4>Findings</h4>
    <ul class="prompt-list">${(data.findings || []).map(item => `<li>${esc(item)}</li>`).join('')}</ul>
  </section>`;
}

// Zone C. The same advice every prompt gets. Collapsed, and labelled as what it
// is -- it used to sit beside the derived material with nothing separating them.
function renderGuardrailZone(data) {
  const suggestions = data.suggestions || [];
  if (!suggestions.length) return '';
  return `<details class="detail-section plan-zone plan-zone-guardrails">
    <summary>Standard execution guardrails (${suggestions.length})</summary>
    <div class="details-body">
      <p>House advice applied to every prompt of this shape. Not derived from yours.</p>
      <ul class="prompt-list">${suggestions.map(item => `<li>${esc(item)}</li>`).join('')}</ul>
    </div>
  </details>`;
}

async function preflightPrompt() {
  const prompt = document.getElementById('promptInput').value;
  const tool = document.getElementById('promptTool').value;
  const cwd = document.getElementById('promptCwd').value;
  const resultNode = document.getElementById('promptResult');
  if (!prompt.trim()) {
    resultNode.innerHTML = '<div class="empty">Write or paste a prompt first.</div>';
    return;
  }
  resultNode.innerHTML = '<div class="loading">Choosing the safest route for this prompt...</div>';
  try {
    const res = await fetch('/api/preflight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt, tool, cwd })
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      resultNode.innerHTML = `<div class="empty">${esc(data.error || 'Could not preflight prompt.')}</div>`;
      return;
    }
    const riskTone = data.risk || 'low';
    resultNode.innerHTML = `${renderPlanAction(data.plan_action)}
    ${renderDerivedZone(data)}
    ${renderObservedZone(data, riskTone)}
    ${renderGuardrailZone(data)}
    <div class="detail-section">
      <h3>Paste-ready brief</h3>
      <textarea id="promptBrief" class="brief-box">${esc(data.suggested_prompt)}</textarea>
      <div class="copy-row">
        <button class="btn-primary" onclick="copyText(document.getElementById('promptBrief').value, 'Execution brief copied — paste it into your AI tool now')">Copy execution brief</button>
        <button class="btn-quiet" onclick="copyText(document.getElementById('promptInput').value, 'Original prompt copied — paste it into your AI tool now')">Copy my prompt</button>
      </div>
      <p style="margin-top:10px">Paste whichever you choose as the next message in your AI tool. If the recommended route is Fresh Start or Fork, open that route first and paste the brief there.</p>
      <p style="margin-top:6px">${esc(data.privacy)}</p>
    </div>`;
    // Stage 1 is on screen and complete before the expensive half is even
    // asked for. The gate decided this, not the click: nothing spawns unless
    // the free local score reached the medium band.
    if ((data.second_opinion || {}).pending) loadSecondOpinion(prompt, tool, cwd);
  } catch (error) {
    resultNode.innerHTML = '<div class="empty">Could not reach the local AIWatcher server.</div>';
  }
}
let toastTimer;
function showToast(message, kind = 'success') {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.className = `toast ${kind === 'error' ? 'error' : ''} show`;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { toast.className = 'toast'; }, 3200);
}
function openAskPanel(question = '') {
  document.getElementById('askBackdrop').classList.add('open');
  document.getElementById('askPanel').classList.add('open');
  document.getElementById('askPanel').setAttribute('aria-hidden', 'false');
  const input = document.getElementById('askInput');
  if (question) input.value = question;
  window.setTimeout(() => input.focus(), 50);
}
function closeAskPanel() {
  document.getElementById('askBackdrop').classList.remove('open');
  document.getElementById('askPanel').classList.remove('open');
  document.getElementById('askPanel').setAttribute('aria-hidden', 'true');
  const params = new URLSearchParams(location.search);
  if (params.has('ask')) {
    params.delete('ask');
    const query = params.toString();
    history.replaceState(null, '', `${location.pathname}${query ? `?${query}` : ''}${location.hash || ''}`);
  }
}
function appendAskMessage(kind, html) {
  const node = document.getElementById('askMessages');
  const item = document.createElement('div');
  item.className = `ask-message ${kind}`;
  item.innerHTML = html;
  node.appendChild(item);
  node.scrollTop = node.scrollHeight;
}
function renderAskResponse(data) {
  const bullets = (data.bullets || []).length
    ? `<ul>${data.bullets.map(item => `<li>${esc(item)}</li>`).join('')}</ul>`
    : '';
  const actions = (data.actions || []).length
    ? `<div class="ask-actions">${data.actions.map(action => `<a href="${esc(action.url || '/')}" onclick="closeAskPanel()">${esc(action.label || 'Open')}</a>`).join('')}</div>`
    : '';
  appendAskMessage('aiw', `<strong>${esc(data.confidence || 'Local answer')}</strong><p>${esc(data.answer || 'No answer available.')}</p>${bullets}${actions}<p class="receipt-note">${esc(data.privacy || 'Local metadata only.')}</p>`);
}
function askTemplate(question) {
  openAskPanel(question);
  askAIWatcher();
}
function handleAskKey(event) {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    askAIWatcher();
  }
}
async function askAIWatcher() {
  const input = document.getElementById('askInput');
  const button = document.getElementById('askSendButton');
  const question = input.value.trim();
  if (!question) {
    showToast('Ask a question first.', 'error');
    return;
  }
  appendAskMessage('user', `<p>${esc(question)}</p>`);
  input.value = '';
  button.disabled = true;
  button.textContent = 'Checking...';
  try {
    const res = await fetch('/api/ask-aiwatcher', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, days: Number(document.getElementById('days').value || 7) }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      appendAskMessage('aiw', `<strong>Local answer unavailable</strong><p>${esc(data.error || 'AIWatcher could not answer from local evidence.')}</p>`);
      return;
    }
    renderAskResponse(data);
  } catch (error) {
    appendAskMessage('aiw', '<strong>Local answer unavailable</strong><p>Could not reach the local AIWatcher server.</p>');
  } finally {
    button.disabled = false;
    button.textContent = 'Ask';
  }
}
async function requestRuntimeReturn(sessionId) {
  return fetch('/api/runtime-return', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: sessionId})
  });
}
async function returnToRuntime(sessionId) {
  try {
    const res = await requestRuntimeReturn(sessionId);
    const data = await res.json();
    if (!res.ok || data.error) {
      showToast(data.error || 'Could not find this local session.', 'error');
      return;
    }
    showToast(data.message || 'Return action requested.', data.ok ? 'success' : 'error');
  } catch (error) {
    showToast('Could not reach the local AIWatcher server.', 'error');
  }
}
// Resume reopens the CONVERSATION in a new terminal via the tool's own
// `--resume`. It is offered whatever the live-attachment tier above says,
// because it is the one return path that survives a closed terminal -- and on
// Windows it is the only one that can fire at all.
//
// Launching a terminal is the convenience; copying the command always works.
// A failed launch therefore falls back to the clipboard rather than reporting
// a window that never opened.
async function resumeSession(sessionId, launch = true) {
  try {
    const res = await fetch('/api/session-resume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId, launch})
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      showToast(data.error || 'Could not find this local session.', 'error');
      return;
    }
    if (!data.available) {
      showToast(data.message || 'This tool has no local resume command.', 'error');
      return;
    }
    if (data.ok && launch) {
      showToast(data.message || 'Opened a terminal running this session.', 'success');
      return;
    }
    const copied = await copyText(data.command, 'Resume command copied — paste it in a terminal');
    if (copied && launch) showToast(data.message || 'Could not open a terminal; command copied instead.', 'error');
  } catch (error) {
    showToast('Could not reach the local AIWatcher server.', 'error');
  }
}
function openDrawer(title) {
  const content = document.getElementById('detailContent');
  if (content) content.aiwSettled = null;
  document.getElementById('drawerTitle').textContent = title;
  document.getElementById('drawerBackdrop').classList.add('open');
  document.getElementById('detailDrawer').classList.add('open');
  document.getElementById('detailDrawer').setAttribute('aria-hidden', 'false');
  document.body.classList.add('drawer-open');
}
function closeDrawer() {
  document.getElementById('drawerBackdrop').classList.remove('open');
  document.getElementById('detailDrawer').classList.remove('open');
  document.getElementById('detailDrawer').setAttribute('aria-hidden', 'true');
  document.body.classList.remove('drawer-open');
}
// A drawer populates in up to three writes: a loading line at ~0ms, a fast
// summary, then full evidence anywhere from 200ms to 4.5s later. Each write
// replaced the node wholesale, so the populated state landed as a jump.
// Fading the whole node instead makes the last write blink the hero, which is
// byte-identical across the summary -> detail swap and has already settled.
// So diff against what is on screen and animate only the blocks that changed.
// prefers-reduced-motion is honoured by the global reduce block in index.css,
// which collapses the animation to nothing.
function drawerArrivalBlocks(node) {
  // A session review wraps everything in one .session-review-shell, so the
  // blocks that arrive independently sit one level further down.
  const children = Array.from(node.children);
  if (children.length === 1 && children[0].children.length > 1) return Array.from(children[0].children);
  return children;
}
function setDrawerContent(html) {
  const node = document.getElementById('detailContent');
  if (!node) return null;
  const settled = node.aiwSettled || new Set();
  node.innerHTML = html;
  const blocks = drawerArrivalBlocks(node);
  // Recorded before the class is added, so the next write compares against
  // pristine markup rather than markup carrying a leftover .aiw-arrive.
  node.aiwSettled = new Set(blocks.map(el => el.outerHTML));
  // The arrival animation fills backwards, so a pending one holds the block at
  // opacity 0. This page spends most of its life hidden on a second monitor,
  // where animations never start -- so a write that lands while hidden is left
  // alone rather than parked invisible. Nobody is watching it resolve anyway.
  if (document.hidden) return node;
  blocks.forEach(el => { if (!settled.has(el.outerHTML)) el.classList.add('aiw-arrive'); });
  return node;
}
function outcomePill(outcome) {
  const value = outcome || 'not marked';
  const tone = outcome || '';
  return `<span class="pill outcome-pill ${tone}">Outcome: ${esc(value)}</span>`;
}
function outcomeEvidencePill(session) {
  if (!session || session.outcome) return '';
  if (session.inferred_outcome === 'churned') return '<span class="pill outcome-pill rework">Evidence: reverted/rewritten</span>';
  if (session.inferred_outcome === 'useful') return '<span class="pill outcome-pill useful">Evidence: likely useful</span>';
  if (session.inferred_outcome === 'needs_review') return '<span class="pill outcome-pill rework">Evidence: review changes</span>';
  if (session.evidence_captured) return '<span class="pill outcome-pill evidence">Evidence captured</span>';
  return '';
}
function healthPill(health) {
  if (!health) return '<span class="health-pill limited">Limited data</span>';
  return `<span class="health-pill ${esc(health.tone || health.status || 'limited')}">${esc(health.label || 'Review')}</span>`;
}
function sessionStatePill(state) {
  if (!state) return '';
  return `<span class="session-state ${esc(state.status || 'unknown')}">${esc(state.label || state.status || 'unknown')}</span>`;
}
function shortSessionId(value) {
  const text = String(value || '');
  return text.length > 12 ? `${text.slice(0, 8)}...${text.slice(-4)}` : text || 'unknown';
}
function identityTone(runtime) {
  const level = (runtime || {}).identity_level || (runtime || {}).level || 'historical_log';
  if (level === 'exact_session' || level === 'active_process') return 'active';
  if (level === 'likely_workspace' || level === 'app' || level === 'workspace') return 'recent';
  return 'ended';
}
function renderIdentityStrip(item, runtime, sourcePath) {
  runtime = runtime || {};
  const sessionId = item.session_id || runtime.session_id || '';
  const project = item.project || item.project_short || runtime.project_path || 'unknown';
  const surface = runtime.surface || item.surface || 'unknown surface';
  const identityLabel = runtime.identity_label || runtime.label || 'Historical log only';
  return `<div class="session-identity-card">
    <div class="session-identity-row">
      <span class="confidence-chip ${esc(identityTone(runtime))}">${esc(identityLabel)}</span>
      <div class="session-identity-main">${esc(item.tool || runtime.tool || 'unknown tool')} · ${esc(surface)} · ${esc(project)}</div>
      <span class="session-id-chip">${esc(shortSessionId(sessionId))}</span>
    </div>
  </div>`;
}
function confidenceLabel(s) {
  if (s.outcome) return { label: 'Verified outcome', tone: 'verified' };
  if (s.inferred_outcome) return { label: 'Inferred outcome', tone: 'inferred' };
  if (s.evidence_captured) return { label: 'Observed evidence', tone: 'observed' };
  return { label: 'Local metadata', tone: 'unknown' };
}
function runtimeReturnPanel(runtime, sourcePath) {
  runtime = runtime || {};
  const available = !!runtime.available;
  const level = runtime.level || 'unavailable';
  const targetLabel = available
    ? (level === 'app' ? 'App return available' : level === 'active_process' ? 'Workspace return available' : 'Workspace available')
    : 'Log only';
  const reason = runtime.reason || 'AIWatcher found a local session log, but no live process, window handle, or platform deep link for this exact chat.';
  const exactLabel = runtime.exact_return_label || 'Exact chat unavailable';
  const exactReason = runtime.exact_return_reason || 'Exact chat return needs a verified app window, terminal pane, or host deep link.';
  const source = sourcePath || 'unknown';
  const identityReason = runtime.identity_reason || runtime.reason || 'AIWatcher found local session evidence, but no verified exact chat attachment.';
  const updated = runtime.updated_at || runtime.updated_at_label || '';
  const action = available
    ? `<button class="btn-quiet" data-session="${esc(runtime.session_id || '')}" onclick="returnToRuntime(this.dataset.session)">${esc(runtime.action_label || 'Open workspace')}</button>`
    : `<span class="action-note">No exact return — ${esc(exactReason)}</span>`;
  // "Resume in terminal", never "Return to session": this reopens the
  // conversation somewhere new, and the label has to say which.
  const resumeAction = runtime.resume_available
    ? `<button class="btn-quiet" data-session="${esc(runtime.session_id || '')}" onclick="resumeSession(this.dataset.session)">${esc(runtime.resume_label || 'Resume in terminal')}</button>`
      + `<button class="btn-quiet" data-session="${esc(runtime.session_id || '')}" onclick="resumeSession(this.dataset.session, false)">Copy resume command</button>`
    : '';
  return `<section class="detail-section runtime-return">
    <details class="aiw-details">
      <summary>Return, share, and source log</summary>
      <div class="details-body">
        <div class="section-title">
          <div><h3>Return target</h3><p>${esc(reason)}</p></div>
          <span class="session-state ${available ? 'active' : 'ended'}">${esc(targetLabel)}</span>
        </div>
        <div class="copy-row">${resumeAction}${action}<button class="btn-quiet" data-source="${esc(source)}" onclick="copyText(this.dataset.source, 'Session log path copied')">Copy log path</button></div>
        <div class="runtime-source">
          <strong>Resume</strong><span>${esc(runtime.resume_reason || 'No local resume command for this tool.')}</span>
          <strong>Last activity</strong><span>${esc(updated ? dateLabel(updated) : 'unknown')}</span>
          <strong>Identity</strong><span>${esc(identityReason)}</span>
          <strong>Session log</strong><span>${esc(source)}</span>
        </div>
        <p class="tool-link-note"><strong>${esc(exactLabel)}:</strong> ${esc(exactReason)}</p>
      </div>
    </details>
  </section>`;
}
let watcherCommand = 'aiwatcher watch --notify --overlay --interval 60';
// The dropdown defaulted to whichever option came first in the markup, which
// was Codex, while every observed session on this machine is claude-code. Set
// it from the most recent session instead, and only before the user has touched
// it -- re-picking on every 10s poll would fight them mid-edit.
let promptToolTouched = false;

function setDefaultPromptTool(data) {
  const select = document.getElementById('promptTool');
  if (!select || promptToolTouched) return;
  const recent = (data.recent_sessions || [])[0];
  const tool = String((recent && recent.tool) || '').toLowerCase();
  const match = tool.includes('claude') ? 'claude'
    : tool.includes('codex') ? 'codex'
    : tool.includes('cursor') ? 'cursor'
    : null;
  // Falls back to Claude Code rather than to the first option in the list.
  select.value = match || 'claude';
}

function renderWatcher(watcher) {
  const pill = document.getElementById('watcherPill');
  const start = document.getElementById('watcherStart');
  const commandText = document.getElementById('watcherCommandText');
  watcherCommand = (watcher && watcher.command) || watcherCommand;
  if (watcher && watcher.running) {
    pill.className = 'cache-pill fresh';
    pill.textContent = 'Watcher running';
    start.hidden = true;
    return;
  }
  // Stopped is a warning, not a neutral state: every screen is reporting on
  // data the watcher is not currently collecting. It used to read in the same
  // blue as "building".
  pill.className = 'cache-pill refreshing';
  pill.textContent = watcher && watcher.status === 'stale' ? 'Watcher stale' : 'Watcher stopped';
  commandText.textContent = watcherCommand || 'aiwatcher watch';
  start.hidden = false;
}
// ---------------------------------------------------------------------------
// What is running right now.
//
// The strip states sessions, never sub-agents: a sub-agent runs inside its
// parent's session, so counting it here would inflate the one number this line
// exists to give. It also only ever sees this machine -- another computer, or a
// cloud session, writes nothing locally -- which is why the copy says "on this
// machine" rather than implying a total.
//
// Nothing live means nothing rendered. A permanent "0 running" would be a row
// of zeros on an idle machine, and this strip is on every view.
// ---------------------------------------------------------------------------
function presenceText(presence) {
  const tools = (presence && presence.tools) || [];
  return tools
    .filter(tool => tool.live > 0)
    .map(tool => {
      const bits = [];
      if (tool.waiting) bits.push(tool.waiting + ' waiting');
      if (tool.working) bits.push(tool.working + ' working');
      if (tool.quiet) bits.push(tool.quiet + ' quiet');
      // AIWatcher spawns its own sessions for Second Opinion. Counted --
      // hiding what a feature costs is the thing this product refuses to do --
      // but named on the tool it belongs to. Floated to the end of the line it
      // read as a comment on whichever tool happened to be listed last.
      const mine = Number(tool.analyst_runs) || 0;
      const note = mine
        ? ' <span class="presence-note">(' + mine + " is AIWatcher's own)</span>"
        : '';
      return esc(tool.label || tool.tool) + ' ' + bits.join(', ') + note;
    })
    .join('  ·  ');
}

// A session that has stopped and cannot continue without you. This is the only
// state here that does not come from a timestamp -- the tool reports it through
// a hook -- which is why it is the only one allowed to say "it needs you"
// rather than "it went quiet". Everything else on this line is an observation;
// this one is a request.
//
// It leads, ahead of even the shared-tree warning: that one predicts work you
// might lose, this one is time you are losing now.
function waitingMarkup(presence) {
  const blocked = ((presence && presence.sessions) || [])
    .filter(row => row.state === 'waiting')
    .sort((a, b) => (b.idle_seconds || 0) - (a.idle_seconds || 0));
  if (!blocked.length) return '';
  // The longest wait, not the count of them: how long you have been the
  // bottleneck is the part that should make you go and look.
  const longest = esc(String(blocked[0].label || 'waiting on you').replace('waiting ', ''));
  const text = blocked.length === 1
    ? 'waiting on you ' + longest
    : blocked.length + ' waiting on you, longest ' + longest;
  return '<span class="presence-alert">' + text + '</span>  ·  ';
}

// Two live sessions in one checkout can overwrite each other with no git
// conflict to show for it -- one working tree, last writer wins. It leads the
// line rather than trailing it: the line ellipsises from the right, and the one
// clause here that predicts lost work must not be the first thing dropped.
function collisionMarkup(presence) {
  const clashes = (presence && presence.collisions) || [];
  if (!clashes.length) return '';
  const first = clashes[0];
  const more = clashes.length > 1 ? ' +' + (clashes.length - 1) + ' more' : '';
  const why = first.live + ' live sessions share this working tree ('
    + (first.tools || []).join(', ') + '). Edits from one can overwrite the '
    + "other's, and git sees one tree so nothing conflicts.";
  return '<span class="presence-clash" title="' + esc(why) + '">'
    + first.live + ' sharing ' + esc(first.label) + esc(more) + '</span>  ·  ';
}

function renderPresence(presence) {
  const node = document.getElementById('presenceStrip');
  if (!node) return;
  const live = Number(presence && presence.live) || 0;
  if (!live) {
    node.hidden = true;
    node.textContent = '';
    return;
  }
  // Total first, breakdown after. This line ellipsises on a narrow window --
  // the normal case for a dashboard sharing a screen with an editor -- and with
  // the breakdown leading, the truncation swallowed the second tool whole and
  // read as though the first was all of it. Leading with the total means
  // running out of room costs detail, never the headline.
  // Skipped at one session, where "1 live · Claude 1 working" says it twice.
  const lead = live > 1 ? live + ' live  ·  ' : '';
  // Scope trails, after its own separator: mid-line it read as a qualifier on
  // whichever tool happened to be listed last rather than on the whole count.
  const markup = waitingMarkup(presence) + collisionMarkup(presence) + lead + presenceText(presence)
    + '  <span class="presence-note presence-scope">·  on this machine</span>';
  if (node.dataset.markup === markup) return;
  node.dataset.markup = markup;
  node.innerHTML = markup;
  node.hidden = false;
}

function renderCacheStatus(cache) {
  const status = document.getElementById('cacheStatus');
  if (!cache) {
    status.textContent = 'Local data loaded';
    return;
  }
  const source = cache.source === 'disk' ? 'cached local index' : cache.source === 'memory' ? 'memory cache' : 'local scan';
  status.textContent = cache.refreshing
    ? `Showing ${source}; updating evidence in background...`
    : `Showing ${source}`;
}
function copyWatcherCommand() {
  copyText(watcherCommand, 'Copied. Paste in a terminal to start the watcher.');
}
function survivalStatus(survival) {
  // The status itself, not a rendered label: "survived" | "churned" | "unknown"
  // for the earliest bucket that has run. Callers need to tell an answer from a
  // non-answer, and "unknown (7-day check)" is a non-answer.
  if (!survival) return null;
  for (const bucket of ['7', '14', '30']) {
    if (survival[bucket]) return { status: survival[bucket], bucket };
  }
  return null;
}
function survivalLabel(survival) {
  // evidence.survival is {bucket: "survived"|"churned"|"unknown"} -- already
  // flattened server-side (see ui.py's _survival_for_session), not the raw
  // {status, checked_at} shape local_state.py stores it in.
  if (!survival) return null;
  for (const bucket of ['7', '14', '30']) {
    const status = survival[bucket];
    if (status) return `${status} (${bucket}-day check)`;
  }
  return null;
}
function renderEvidence(evidence) {
  if (!evidence) return '';
  const commits = evidence.commits || [];
  const files = evidence.changed_files || [];
  const tests = evidence.tests || [];
  const reasons = evidence.reasons || [];
  const survival = survivalLabel(evidence.survival);
  return `<section class="detail-section"><details class="aiw-details"><summary>Outcome evidence</summary><div class="details-body">
    <p>Local git/test signals. AIWatcher stores metadata, not source diffs.</p>
    <div class="mini-grid">
      <div class="mini"><span class="label">Inferred outcome</span><strong>${esc(evidence.inferred_outcome || 'not enough evidence')}</strong></div>
      <div class="mini"><span class="label">Confidence</span><strong>${esc(evidence.confidence || 'low')}</strong></div>
      <div class="mini"><span class="label">Nearby commits</span><strong>${esc(commits.length)}</strong></div>
      <div class="mini"><span class="label">Changed files</span><strong>${esc(files.length)}</strong></div>
    </div>
    ${survival ? `<div class="pill-row"><span class="pill">Survival: ${esc(survival)}</span></div>` : ''}
    ${evidence.same_file_reprompt ? '<div class="pill-row"><span class="pill rework">A later session touched the same file(s) again soon after</span></div>' : ''}
    ${tests.length ? `<div class="pill-row"><span class="pill">${esc(tests.length)} recent test artifact${tests.length === 1 ? '' : 's'}</span></div>` : ''}
    ${reasons.length ? `<ul class="insight-list">${reasons.map(reason => `<li>${esc(reason)}</li>`).join('')}</ul>` : ''}
    ${commits.length ? `<div class="pill-row">${commits.slice(0, 4).map(commit => `<span class="pill">commit ${esc(commit.sha)}</span>`).join('')}</div>` : ''}
    ${files.length ? `<details class="aiw-details"><summary>${esc(files.length)} changed file${files.length === 1 ? '' : 's'}</summary><div class="details-body"><div class="pill-row">${files.slice(0, 12).map(file => `<span class="pill">${esc(file)}</span>`).join('')}</div></div></details>` : ''}
  </div></details></section>`;
}
function eventTypeLabel(type) {
  const labels = {
    assistant: 'Model reasoning',
    assistant_tool_use: 'Tool-driven work',
    tool_result: 'Tool results',
    user: 'User turns',
    'last-prompt': 'Prompt metadata',
    mode: 'Mode metadata'
  };
  return labels[type] || type;
}
function compactText(value, limit = 420) {
  const text = String(value || '');
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}
// A session gets judged on three separate questions, because they become
// answerable at different times and collapsing them is what broke the old
// verdict. "How much room is left" is knowable now and is the only urgent one.
// "Did it cost more than it needed to" is knowable once the session stops.
// "Was it worth it" needs its commits to age seven days before survival means
// anything (see survival.py's MIN_AGE_DAYS -- a commit from this morning scores
// ~100% because nothing has had time to touch it, not because it stuck).
//
// The old rule answered all three with one absolute token threshold, which fired
// for two sessions in three and so distinguished nothing.
function verdictLines(s) {
  const v = s.verdict || {};
  const lines = [];

  const p = v.pressure || {};
  if (p.measurable) {
    const critical = compactTokens(p.critical_tokens);
    const pressure = compactTokens(p.pressure_tokens);
    let body;
    if (p.turns_to_critical === null || p.turns_to_critical === undefined) {
      body = p.latest_turn_tokens >= p.critical_tokens
        ? `${p.latest_turn_label} per turn, past the ${critical} mark. No headroom left to project.`
        : `${p.latest_turn_label} per turn. Not enough turns yet to project a trend.`;
    } else {
      body = `${p.latest_turn_label} per turn. About ${p.turns_to_critical} turn${p.turns_to_critical === 1 ? '' : 's'} of headroom at this rate.`;
    }
    lines.push({ key: 'room', label: 'Room left', tone: p.severity, body });
  }

  const r = v.replay || {};
  if (r.measurable) {
    // "High" is a claim about this owner's own history, not about a fixed line,
    // so with no baseline the honest answer is that we cannot tell yet -- drawn
    // as unknown rather than as the healthy branch.
    const c = r.comparison || {};
    const spent = `${r.share_label} of what this cost went on re-sending history, ${r.replayed_cost_label} of it.`;
    let tone;
    let body;
    if (!c.available) {
      tone = 'unknown';
      body = `${spent} ${c.reason || 'There is no baseline to compare it against yet.'}`;
    } else if (r.high) {
      tone = 'warning';
      body = `${spent} That is higher than ${Math.round(c.rank_pct)}% of your other sessions, which usually run at ${Math.round(c.baseline_median_pct)}%.`;
    } else {
      tone = 'healthy';
      body = `${spent} Your sessions usually run at ${Math.round(c.baseline_median_pct)}%, so this is ordinary for you.`;
    }
    lines.push({ key: 'cost', label: 'Cost to run', tone, body });
  } else if (r.reason) {
    lines.push({ key: 'cost', label: 'Cost to run', tone: 'unknown', body: r.reason });
  }

  const evidence = s.outcome_evidence || {};
  const commits = evidence.commits || [];
  const checked = survivalStatus(evidence.survival);
  const plural = commits.length === 1 ? '' : 's';
  let worth;
  let tone = 'unknown';
  if (!commits.length) {
    worth = 'No commit has landed near this session yet, so there is nothing to judge it by.';
  } else if (checked && checked.status === 'survived') {
    tone = 'healthy';
    worth = `${commits.length} commit${plural} landed, and the work was still standing at the ${checked.bucket}-day check.`;
  } else if (checked && checked.status === 'churned') {
    tone = 'warning';
    worth = `${commits.length} commit${plural} landed, but the work was gone by the ${checked.bucket}-day check.`;
  } else if (checked) {
    // The check ran and could not tell. That is not a pass, and colouring it as
    // one is how a non-answer starts reading like an answer.
    worth = `${commits.length} commit${plural} landed. The ${checked.bucket}-day check could not tell whether the work stuck.`;
  } else {
    worth = `${commits.length} commit${plural} landed near this session. Whether the work stuck is judged after 7 days.`;
  }
  lines.push({ key: 'worth', label: 'Was it worth it', tone, body: worth });

  return lines;
}
function renderVerdict(s) {
  const lines = verdictLines(s);
  if (!lines.length) return '';
  return `<section class="detail-section verdict-lines">
    ${lines.map(line => `<div class="verdict-line tone-${esc(line.tone || 'unknown')}">
      <span class="verdict-label">${esc(line.label)}</span>
      <p>${esc(line.body)}</p>
    </div>`).join('')}
  </section>`;
}
function renderEvidenceRail(s, costliest, meaningfulEvents) {
  const evidence = s.outcome_evidence || {};
  const nodes = [
    {
      tone: 'observed',
      title: 'Session observed',
      body: `${s.tool || 'AI tool'} · ${dateLabel(s.started_at || s.updated_at)} · ${s.tokens_label || s.tokens || 'unknown'} tokens`,
    },
  ];
  if (costliest) {
    nodes.push({
      tone: 'observed',
      title: 'Costliest step',
      body: `${eventTypeLabel(costliest.event_type)} · ${costliest.tokens_label || 'unknown'} tokens · ${costliest.api_value || 'unknown value'}`,
    });
  }
  if (evidence.inferred_outcome) {
    nodes.push({
      tone: 'inferred',
      title: `Outcome inferred: ${evidence.inferred_outcome}`,
      body: evidence.explanation || 'Based on local git/test signals. Confirm manually before treating it as value.',
    });
  } else if (s.outcome) {
    nodes.push({
      tone: 'verified',
      title: `Outcome marked: ${s.outcome}`,
      body: 'User-confirmed local outcome. AIWatcher can use this in value metrics.',
    });
  } else {
    nodes.push({
      tone: 'unknown',
      title: 'Outcome not marked',
      body: 'Mark useful, needs rework, or abandoned so value metrics are about outcomes, not raw tokens.',
    });
  }
  if ((s.actions || []).some(action => action.id === 'handoff')) {
    nodes.push({
      tone: 'inferred',
      title: 'Fresh Start available',
      body: 'AIWatcher can build a continuation brief and watch for follow-up proof.',
    });
  }
  nodes.push({
    tone: meaningfulEvents && meaningfulEvents.length ? 'observed' : 'unknown',
    title: meaningfulEvents && meaningfulEvents.length ? `${meaningfulEvents.length} meaningful events` : 'No meaningful timeline yet',
    body: meaningfulEvents && meaningfulEvents.length ? 'Full event detail remains below for debugging.' : 'This surface may only expose history or metadata.',
  });
  return `<section class="detail-section">
    <details class="aiw-details"><summary>Evidence trail &mdash; what AIWatcher knows, and how confident it is</summary><div class="details-body">
    <div class="evidence-rail">${nodes.map(node => `<div class="evidence-node ${esc(node.tone)}">
      <div class="evidence-dot" aria-hidden="true"></div>
      <div class="evidence-copy"><strong>${esc(node.title)} <span class="confidence-chip ${esc(node.tone)}">${esc(node.tone)}</span></strong><p>${esc(node.body)}</p></div>
    </div>`).join('')}</div>
  </div></details></section>`;
}
function splitLines(value) {
  return String(value || '').split(/\n+/).map(item => item.trim().replace(/\s+/g, ' ')).filter(Boolean).slice(0, 8);
}
function handoffOptionsFromForm(defaultType = 'coding') {
  const typeEl = document.getElementById('handoffType');
  const objectiveEl = document.getElementById('handoffObjective');
  const sourceEl = document.getElementById('handoffSources');
  const constraintEl = document.getElementById('handoffConstraints');
  const acceptanceEl = document.getElementById('handoffAcceptance');
  return {
    type: typeEl ? typeEl.value : defaultType,
    objective: objectiveEl ? objectiveEl.value.trim() : '',
    sources: splitLines(sourceEl ? sourceEl.value : ''),
    constraints: splitLines(constraintEl ? constraintEl.value : ''),
    acceptance: splitLines(acceptanceEl ? acceptanceEl.value : ''),
  };
}
function handoffPayload(sessionId, target, includePrompt, options) {
  const next = options || {};
  return {
    session_id: sessionId || '',
    target: target || 'generic',
    prompt: !!includePrompt,
    type: next.type || 'coding',
    objective: next.objective || '',
    source_refs: next.sources || [],
    constraints: next.constraints || [],
    acceptance_criteria: next.acceptance || [],
  };
}
async function postJson(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  return res.json();
}
function renderHandoffForm(capsule) {
  const selected = capsule.handoff_type || 'coding';
  const sourceRefs = (capsule.source_refs || []).join('\n');
  const constraints = (capsule.constraints || []).join('\n');
  const acceptance = (capsule.acceptance_criteria || []).join('\n');
  return `<div class="handoff-form">
    <div class="handoff-form-head">
      <div>
        <h3>Shape the next session</h3>
        <p>Keep this brief specific enough that the new chat can continue from evidence, not from hidden memory.</p>
      </div>
      <span class="pill">${esc(capsule.demo ? 'sample data' : 'local only')}</span>
    </div>
    <div class="handoff-form-grid">
      <label><span class="label">Work type</span><select id="handoffType">${HANDOFF_TYPES.map(item => `<option value="${esc(item.id)}" ${item.id === selected ? 'selected' : ''}>${esc(item.label)}</option>`).join('')}</select></label>
      <label><span class="label">Objective</span><input id="handoffObjective" value="${esc(capsule.objective || '')}" placeholder="What should the next chat accomplish?"></label>
      <label><span class="label">Source of truth</span><textarea id="handoffSources" placeholder="One file, PR, issue, or local state per line">${esc(sourceRefs)}</textarea></label>
      <label><span class="label">Constraints</span><textarea id="handoffConstraints" placeholder="Scope, privacy, files not to touch, decisions already made">${esc(constraints)}</textarea></label>
      <label><span class="label">Acceptance checks</span><textarea id="handoffAcceptance" placeholder="How should the next chat know it is done?">${esc(acceptance)}</textarea></label>
    </div>
    <div class="copy-row"><button class="btn-quiet" onclick="regenerateHandoff('${esc(capsule.session_id)}','${esc(capsule.target || 'generic')}', ${capsule.include_prompt_excerpt ? 'true' : 'false'}, ${capsule.demo ? 'true' : 'false'})">Regenerate brief</button></div>
  </div>`;
}
// Everything rendered into the brief preview has to be a string first.
// Decision records are objects ({summary, reasoning, alternatives_rejected}),
// and the old `item.text || item` fell through to the object itself, so the
// preview showed "• [object Object]" where the reasoning should be. The copied
// brief was never affected -- handoff.py reads .summary correctly -- but the
// preview is what the user judges the artefact by.
// Counts are printed with separators everywhere: "12206" and "4045" were
// rendered raw next to compacted values like "55.5k" in the same column.
// The prompt risk score is an unbounded sum of penalty points, not a rating out
// of N -- cli.py's _risk_for_score bands it at >=3 medium and >=6 high, and
// nothing caps the total. So the scale is made visible by naming those bands
// rather than by inventing a denominator: writing it "2 of 5" would be a
// confident-looking figure with a made-up maximum.
const RISK_MEDIUM_AT = 3;
const RISK_HIGH_AT = 6;
const RISK_SCALE_NOTE = `medium at ${RISK_MEDIUM_AT}, high at ${RISK_HIGH_AT}`;

function riskScore(score) {
  if (score === null || score === undefined) return '—';
  return `${score} ${Math.abs(Number(score)) === 1 ? 'pt' : 'pts'}`;
}

function formatCount(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('en-US') : String(value ?? '');
}

function briefText(item) {
  if (item == null) return '';
  if (typeof item === 'string') return item;
  if (typeof item === 'object') {
    const text = item.summary || item.text || item.label || '';
    if (!text) {
      // Drop it rather than render a placeholder. A missing bullet is
      // recoverable; a bullet reading "[object Object]" is not.
      console.error('AIWatcher: no renderable text on a brief item', item);
    }
    return text;
  }
  return String(item);
}

function listPreview(items, fallback) {
  const values = (items || []).map(briefText).filter(Boolean).filter(value => {
    // Backstop for anything stringified before it reached us, so this class of
    // bug cannot ship silently again.
    if (/\[object \w+\]/.test(value)) {
      console.error('AIWatcher: an object was stringified into the brief', value);
      return false;
    }
    return true;
  });
  if (!values.length) return esc(fallback);
  return values.slice(0, 4).map(item => `• ${esc(item)}`).join('<br>');
}
function renderFreshStartPreview(capsule) {
  const objective = capsule.objective || 'Reconstruct the current work from repo state, recent commits, changed files, and the evidence below.';
  const decisions = capsule.decisions || [];
  return `<div class="fresh-preview">
    <div class="fresh-preview-head">
      <div><h3>Ready to copy</h3><p>This is the structured context the next AI session receives. Refine it above first if anything is missing.</p></div>
      <span class="confidence-chip observed">Metadata only</span>
    </div>
    <div class="fresh-preview-grid">
      <div class="fresh-preview-row"><strong>Objective</strong><p>${esc(objective)}</p></div>
      <div class="fresh-preview-row"><strong>Source of truth</strong><p>${listPreview(capsule.source_refs, 'Repository state, local session metadata, changed files, and the source log.')}</p></div>
      <div class="fresh-preview-row"><strong>Decisions</strong><p>${listPreview(decisions, 'No explicit decisions detected yet; the next session should infer from files and recent evidence.')}</p></div>
      <div class="fresh-preview-row"><strong>Constraints</strong><p>${listPreview(capsule.constraints, 'Do not assume access to hidden chat history. Do not invent unobserved outcomes or savings.')}</p></div>
      <div class="fresh-preview-row"><strong>Done when</strong><p>${listPreview(capsule.acceptance_criteria, 'Identify the smallest next checkpoint and verify it with local evidence.')}</p></div>
    </div>
  </div>`;
}
function freshStartReceiptWidget({ reason = '', expected = '', copy = '', controls = '' } = {}) {
  return `<div class="receipt-widget">
    <div class="receipt-widget-head">
      <div><h3>Fresh Start ready</h3><p>Fresh Start receipt saved. AIWatcher will look for the follow-up before claiming improvement.</p></div>
      <span class="confidence-chip observed">Observed</span>
    </div>
    <div class="receipt-steps">
      <div class="receipt-step"><span>1</span><div><strong>Copied brief</strong><p>${esc(reason || 'Fresh Start brief copied from local session evidence.')}</p></div></div>
      <div class="receipt-step"><span>2</span><div><strong>Next user action</strong><p>${esc(copy || 'Open a fresh AI chat in the same workspace and paste the copied brief.')}</p></div></div>
      <div class="receipt-step"><span>3</span><div><strong>Proof pending</strong><p>AIWatcher will not claim saved tokens until it observes a later same-project session.</p></div></div>
    </div>
    <div class="pill-row"><span class="pill">${esc(expected || 'context at risk')}</span><span class="pill">No saved-token claim yet</span><span class="pill">Fresh Start receipt saved</span></div>
    ${controls ? `<div class="actions" style="margin-top:14px">${controls}</div>` : ''}
  </div>`;
}
function renderHandoff(capsule) {
  const usage = capsule.usage || {};
  const evidence = capsule.evidence || {};
  const changedFiles = evidence.changed_files || [];
  const runtime = capsule.runtime_attachment || {};
  const target = capsule.target || 'generic';
  const includePrompt = !!capsule.include_prompt_excerpt;
  const isDemo = !!capsule.demo;
  const canOpenRuntime = !!runtime.available;
  const enrichment = capsule.basic
    ? '<div class="loading">Basic brief is ready. Loading timeline, git evidence, and prompt enrichment...</div>'
    : '';
  const primaryLabel = canOpenRuntime ? 'Copy brief + open workspace' : 'Copy brief';
  const primaryHelp = canOpenRuntime
    ? 'AIWatcher will copy the brief, open the safest available workspace or app target, and save a local Fresh Start receipt.'
    : 'AIWatcher will copy the brief and save a local Fresh Start receipt. Open the correct AI chat or workspace yourself before pasting.';
  return `<section class="detail-section">
    <h2>Fresh Start</h2>
    <p>AIWatcher prepared a restart brief for the next ${esc(capsule.target_label || 'AI tool')} session. Copy it into a fresh chat only after the identity below matches the work you intend to continue.</p>
    ${renderIdentityStrip(capsule, runtime, capsule.source_path)}
    <div class="mini-grid">
      <div class="mini"><span class="label">Previous usage</span><strong>${esc(usage.tokens_label || '—')}</strong></div>
      <div class="mini"><span class="label">API value</span><strong>${esc(usage.api_value_label || '—')}</strong></div>
      <div class="mini"><span class="label">Model calls</span><strong>${esc(usage.model_calls ?? '—')}</strong></div>
      <div class="mini"><span class="label">Evidence</span><strong>${esc((evidence.commits || []).length)} commits</strong></div>
    </div>
    <div id="handoffStatus" class="verdict-card useful" style="margin-top:14px">
      <h3>Best next action: start fresh in the same workspace</h3>
      <p>${esc(primaryHelp)} AIWatcher will watch for a later same-project session as proof.</p>
      <div class="copy-row" style="margin-top:12px">
        <button class="btn-primary" data-runtime="${canOpenRuntime ? '1' : '0'}" onclick="copyFreshStartFromDrawer('${esc(capsule.session_id)}', this.dataset.runtime === '1', ${isDemo ? 'true' : 'false'})">${esc(isDemo ? 'Copy demo brief' : primaryLabel)}</button>
        ${isDemo ? `<button class="btn-quiet" onclick="showView('sessions'); closeDrawer()">Find real sessions</button>` : `<button class="btn-quiet" onclick="selectSession('${esc(capsule.session_id)}')">Inspect source session</button>`}
      </div>
    </div>
    <!-- Refinement first but folded away, then the brief. The output used to
         sit above the five empty fields that shape it, which reads as "this is
         waiting for you" when in fact it is ready to copy. -->
    <details class="handoff-refine">
      <summary>Refine this brief (optional)</summary>
      ${renderHandoffForm(capsule)}
    </details>
    ${renderFreshStartPreview(capsule)}
    <div class="copy-row">
      <span class="label" style="align-self:center">Format for</span>
      ${Object.entries(HANDOFF_TARGET_LABELS).map(([item, label]) => `<button class="btn-quiet" aria-pressed="${item === target ? 'true' : 'false'}" onclick="regenerateHandoff('${esc(capsule.session_id)}','${item}', ${includePrompt}, ${isDemo ? 'true' : 'false'})">${esc(label)}</button>`).join('')}
    </div>
    <p class="tool-link-note">${esc(runtime.reason || 'Use the Fresh Start brief when the exact running chat cannot be reopened.')}</p>
    ${enrichment}
    <label class="prompt-opt-in">
      <input type="checkbox" ${includePrompt ? 'checked' : ''} onchange="regenerateHandoff('${esc(capsule.session_id)}','${target}', this.checked, ${isDemo ? 'true' : 'false'})">
      <span class="prompt-opt-in-label">Include prompt excerpt <span class="pill">Privacy opt-in</span></span>
      <span class="hint">Off by default: everything else in this brief is metadata (counts, hashes, file paths). This adds your actual prompt text from the costliest turn, so review it before pasting into another tool.</span>
    </label>
  </section>
  ${runtimeReturnPanel(runtime, capsule.source_path)}
  <section class="detail-section"><h3>Why start fresh now</h3>
    <ul class="insight-list">${(capsule.warnings || []).map(item => `<li>${esc(item)}</li>`).join('')}</ul>
  </section>
  <section class="detail-section"><h3>Brief that will be copied</h3>
    <textarea id="handoffBrief" class="brief-box">${esc(capsule.next_brief || '')}</textarea>
    <div class="copy-row"><button class="btn-quiet" onclick="copyFreshStartFromDrawer('${esc(capsule.session_id)}', false, ${isDemo ? 'true' : 'false'})">Copy brief</button></div>
    ${changedFiles.length ? `<details class="aiw-details"><summary>${esc(changedFiles.length)} changed file${changedFiles.length === 1 ? '' : 's'} to inspect</summary><div class="details-body"><div class="pill-row">${changedFiles.slice(0, 12).map(file => `<span class="pill">${esc(file)}</span>`).join('')}</div></div></details>` : ''}
  </section>`;
}
async function copyFreshStartFromDrawer(sessionId, openRuntime = false, isDemo = false) {
  const brief = document.getElementById('handoffBrief') ? document.getElementById('handoffBrief').value : '';
  const copied = await copyText(brief, 'Fresh Start brief copied');
  if (!copied) return;
  if (!isDemo) {
    await fetch('/api/handoff-decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        decision: 'copy_handoff',
        reason: 'Fresh Start brief copied from the session drawer.',
        action_channel: 'dashboard_session',
      })
    }).catch(() => {});
  }
  let message = isDemo
    ? 'Demo brief copied. In a live session AIWatcher would save a local Fresh Start receipt and watch for follow-up proof.'
    : 'Fresh Start receipt saved. Open a fresh chat in the same workspace and paste the brief.';
  if (openRuntime) {
    try {
      const returnRes = await requestRuntimeReturn(sessionId);
      const returned = await returnRes.json();
      if (returned && returned.ok) message = `${returned.message || 'Workspace opened.'} Paste the copied brief into a fresh chat.`;
    } catch (error) {}
  }
  const status = document.getElementById('handoffStatus');
  if (status) {
    const controls = isDemo
      ? '<button class="btn-primary" onclick="showView(\'sessions\'); closeDrawer()">Find real sessions</button><button class="btn-quiet" onclick="closeDrawer()">Done</button>'
      : '<button class="btn-primary" onclick="showView(\'receipts\'); closeDrawer()">View receipt</button><button class="btn-quiet" onclick="closeDrawer()">Done</button>';
    status.outerHTML = `<div id="handoffStatus">${freshStartReceiptWidget({
      reason: isDemo ? 'Demo Fresh Start brief copied.' : 'Fresh Start brief copied from the session drawer.',
      expected: isDemo ? 'sample context at risk' : 'proof pending',
      copy: message,
      controls,
    })}</div>`;
  }
}
async function regenerateHandoff(sessionId, target = 'generic', includePrompt = false, isDemo = false) {
  const options = handoffOptionsFromForm(isDemo ? 'product' : 'coding');
  if (isDemo) {
    await openDemoHandoff(target, includePrompt, options);
  } else {
    await openHandoff(sessionId, target, includePrompt, options);
  }
}
async function openDemoHandoff(target = 'generic', includePrompt = false, options = null) {
  openDrawer('Fresh Start');
  const node = document.getElementById('detailContent');
  setDrawerContent('<div class="loading">Building Fresh Start demo from sample context pressure...</div>');
  const demoOptions = options || {
    type: 'product',
    objective: 'Continue the work in a fresh session without losing decisions, constraints, or acceptance criteria.',
    sources: ['Current repo state', 'Strategy or spec document', 'Relevant PR or issue'],
    constraints: ['Do not assume access to the previous chat.', 'Do not broaden scope beyond the next checkpoint.', 'Preserve unrelated local changes and privacy boundaries.'],
    acceptance: ['First reply states what appears done, what remains uncertain, and the smallest next checkpoint.', 'The next session loads source-of-truth files before editing.', 'The result reports verification and remaining uncertainty.'],
  };
  const payload = handoffPayload('', target, includePrompt, demoOptions);
  const capsule = await postJson('/api/handoff-demo', payload);
  if (capsule.error) {
    setDrawerContent(`<div class="empty">${esc(capsule.error)}</div>`);
    return;
  }
  setDrawerContent(renderHandoff(capsule));
}
async function openHandoff(sessionId, target = 'generic', includePrompt = false, options = null) {
  openDrawer('Fresh Start');
  const node = document.getElementById('detailContent');
  setDrawerContent('<div class="loading">Finding the source session before building the Fresh Start brief...</div>');
  const handoffOptions = options || handoffOptionsFromForm();
  const payload = handoffPayload(sessionId, target, includePrompt, handoffOptions);
  const summaryPromise = fetch(`/api/session-summary?id=${encodeURIComponent(sessionId)}`)
    .then(res => res.json())
    .catch(() => null);
  const basicPromise = postJson('/api/handoff-basic', payload)
    .catch(() => null);
  const handoffPromise = postJson('/api/handoff', payload);
  const fastSummary = await summaryPromise;
  if (fastSummary && !fastSummary.error) {
    setDrawerContent(renderSessionSummary(fastSummary, 'Building Fresh Start brief...'));
  } else {
    setDrawerContent('<div class="loading">Building local Fresh Start brief...</div>');
  }
  const basicCapsule = await basicPromise;
  if (basicCapsule && !basicCapsule.error && !includePrompt) {
    setDrawerContent(renderHandoff(basicCapsule));
  }
  const capsule = await handoffPromise;
  if (capsule.error) {
    setDrawerContent(`<div class="empty">${esc(capsule.error)}</div>`);
    return;
  }
  setDrawerContent(renderHandoff(capsule));
}
function handoffDecisionBubble(sessionId) {
  const current = window.currentHandoffBubble || {};
  if (current.session_id === sessionId) return current;
  return {
    session_id: sessionId,
    reason: 'Fresh Start brief copied from the session review.',
    body: 'Fresh Start brief copied from the session review.',
    expected_saved_context_tokens: null,
  };
}
async function recordHandoffDecision(bubble, decision) {
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
        source_project_path: bubble.project_full || '',
        action_channel: 'dashboard',
      })
    });
  } catch (error) {
    // Decision receipts should never block the user's flow.
  }
}
async function startFreshFromBubble(sessionId) {
  const res = await fetch(`/api/handoff-basic?id=${encodeURIComponent(sessionId)}&target=generic`);
  const capsule = await res.json();
  if (capsule.error) {
    showToast(capsule.error, 'error');
    return;
  }
  const copied = await copyText(capsule.next_brief || '', 'Fresh Start brief copied');
  if (!copied) return;
  const bubble = handoffDecisionBubble(sessionId);
  await recordHandoffDecision(bubble, 'copy_handoff');
  const runtime = (bubble || {}).runtime_attachment || {};
  if (runtime.available) {
    try {
      const returnRes = await requestRuntimeReturn(sessionId);
      const returned = await returnRes.json();
      showToast(returned.message || 'Brief copied and return target opened.', returned.ok ? 'success' : 'error');
    } catch (error) {
      showToast('Brief copied. Open a fresh chat in the same workspace and paste it.', 'success');
    }
  } else {
    showToast('Brief copied. Open a fresh chat in the same workspace and paste it.', 'success');
  }
  renderHandoffCopied(bubble, sessionId);
}
async function continueFromSession(sessionId) {
  await recordHandoffDecision({
    session_id: sessionId,
    reason: 'User chose to keep working in the current session from the session drawer.',
    body: 'User chose to keep working in the current session from the session drawer.',
    expected_saved_context_tokens: null,
  }, 'continue_here');
  showToast('Fresh Start decision saved: continue here');
  closeDrawer();
  await load(false, true);
}
function renderHandoffCopied(bubble, sessionId) {
  const node = document.getElementById('handoffBubble');
  if (!node || !bubble) return;
  node.hidden = false;
  node.innerHTML = freshStartReceiptWidget({
    reason: bubble.reason || bubble.body || 'Fresh Start brief copied from local evidence.',
    expected: bubble.expected_saved_context_label ? '~' + bubble.expected_saved_context_label + ' expected context at risk' : 'proof pending',
    copy: 'Paste the copied brief into a fresh AI chat for the matching workspace.',
    controls: `
      <button class="btn-primary" onclick="showView('receipts')">View receipt</button>
      <button class="btn-quiet" data-session="${esc(sessionId)}" onclick="openHandoff(this.dataset.session)">Review brief</button>
      <button class="btn-quiet" onclick="document.getElementById('handoffBubble').hidden = true">Dismiss</button>
    `,
  });
}
function dateLabel(value) {
  if (!value) return 'unknown';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
// survival.py will not judge a change younger than this: 95% of lines from the
// last three days are still standing simply because nothing has had time to
// touch them, so a fresh commit would score as a pass it has not earned.
const SURVIVAL_MIN_AGE_DAYS = 7;

function tooYoungToJudge(row) {
  const at = Date.parse(row.committed_at || '');
  if (Number.isNaN(at)) return false;
  return (Date.now() - at) < SURVIVAL_MIN_AGE_DAYS * 86400000;
}

// Same Windows-path bug as projectName had: this matched forward slashes only,
// so a backslash path came back whole and the Changes ledger showed a full
// C:\ path beside shortened ones.
function repoLabel(row) {
  return projectName({ project_full: row.repo || row.project || '' });
}


/* A number column is read down, not across, so its unit and its precision have
   to be decided for the column rather than per cell. The Tokens column showed
   "331.2M" above "158.3k"; the $/line column showed "$1.14" above "$0.0030".
   Both are individually correct and neither can be compared with the row above
   it without re-reading the suffix. */

function tokenColumnFormatter(values) {
  const nums = values.map(Number).filter(Number.isFinite);
  const peak = nums.length ? Math.max(...nums) : 0;
  // One unit for the whole column, chosen by its largest value. A 158k row
  // reading 0.2M beside a 331.2M one is the comparison working, not precision
  // being lost -- next to a third of a billion, 158k *is* rounding error.
  const unit = peak >= 1e9 ? ['B', 1e9] : peak >= 1e6 ? ['M', 1e6] : peak >= 1e3 ? ['k', 1e3] : ['', 1];
  return value => {
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value ?? '');
    if (!unit[0]) return String(Math.round(n));
    return (n / unit[1]).toFixed(1) + unit[0];
  };
}

function currencyColumnFormatter(values) {
  const nums = values.map(Number).filter(v => Number.isFinite(v) && v > 0);
  const floor = nums.length ? Math.min(...nums) : 0;
  // Two decimals everywhere, and anything that would round to zero says so
  // instead. "$0.00" claims the work was free; "<$0.01" is the same fact
  // without the lie, and keeps every cell in the column to one decimal count.
  return moneyLabel;
}
// Two decimals, and a real cost that would round to zero says so instead:
// "$0.00" claims the work was free, "<$0.01" is the same fact without the lie.
function moneyLabel(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  if (n > 0 && n < 0.005) return '<$0.01';
  return '$' + n.toFixed(2);
}

function renderChangeRows(rows) {
  if (!rows.length) {
    return `<tr><td colspan="7" class="empty">No commits in this window, or git history could not be read.</td></tr>`;
  }
  const ordered = sortedRows(rows, changeSort);
  const perLine = currencyColumnFormatter(ordered.map(r => r.usd_per_line));
  const perSurviving = currencyColumnFormatter(ordered.map(r => r.usd_per_surviving_line));
  return ordered.map(row => `<tr>
    <td><code>${esc(row.short_sha)}</code> ${esc(row.subject)}
      <div class="session-meta">${esc(dateLabel(row.committed_at))}${row.tools.length ? ' &middot; ' + esc(row.tools.join(', ')) : ''}${row.event_count ? ' &middot; ' + esc(row.event_count) + ' model calls' : ''}${row.was_rewritten ? ' &middot; <span class="muted" title="Rebased or amended on ' + esc(dateLabel(row.rewritten_at)) + '. Cost is attributed by when the work was authored, not when it was rewritten.">rewritten</span>' : ''}</div></td>
    <td title="${esc(row.repo || row.project)}">${esc(repoLabel(row))}</td>
    <td class="num">${row.unattributed ? '<span class="muted">no spend observed</span>' : esc(row.cost_label)}</td>
    <td class="num">+${esc(row.lines_added)} / -${esc(row.lines_removed)}
      <div class="session-meta">${esc(row.files_changed)} file(s)</div></td>
    <td class="num">${row.unattributed ? '—' : esc(perLine(row.usd_per_line))}</td>
    <td class="num">${row.survival_pct === null || row.survival_pct === undefined
      ? `<span class="muted">${tooYoungToJudge(row) ? 'too new' : '—'}</span>`
      : esc(row.survival_label)}</td>
    <td class="num">${row.survival_pct === null || row.survival_pct === undefined
      ? '<span class="muted">—</span>'
      : esc(perSurviving(row.usd_per_surviving_line))}</td>
  </tr>`).join('');
}
function renderChangeTotals(rows, meta, unbanked) {
  if (!rows.length) return '';
  const foreign = (meta && meta.foreign_changes) || 0;
  const attributed = rows.filter(row => !row.unattributed);
  const cost = attributed.reduce((sum, row) => sum + row.cost_usd, 0);
  const lines = attributed.reduce((sum, row) => sum + row.lines_changed, 0);
  const measured = rows.filter(row => row.survival_pct !== null && row.survival_pct !== undefined).length;
  const tooYoung = rows.filter(row => (row.survival_pct === null || row.survival_pct === undefined) && tooYoungToJudge(row)).length;
  const survivalDetail = measured
    ? `${measured} of ${rows.length}`
    : tooYoung
      ? `none yet`
      : `0 of ${rows.length}`;
  const survivalNote = !measured && tooYoung
    ? `<span class="mini-note">every commit here is under ${SURVIVAL_MIN_AGE_DAYS} days old</span>`
    : '';
  const unbankedUsd = unbanked && unbanked.available ? Number(unbanked.unbanked_usd || 0) : 0;
  return `<div class="mini-grid" style="margin-bottom:12px">
    <div class="mini"><span class="label">Commits</span><strong>${esc(rows.length)}</strong>${foreign
      ? `<span class="mini-note">${esc(foreign)} more by other authors, excluded</span>` : ''}</div>
    <div class="mini"><span class="label">Attributed spend</span><strong>${esc(fmtMoney(cost))}</strong>${unbankedUsd > 0
      ? `<span class="mini-note">${esc(unbanked.unbanked_label)} has no commit to attach to</span>` : ''}</div>
    <div class="mini"><span class="label">Lines changed</span><strong>${esc(lines.toLocaleString())}</strong></div>
    <div class="mini"><span class="label">Survival measured</span><strong>${esc(survivalDetail)}</strong>${survivalNote}</div>
  </div>`;
}
function fmtMoney(value) {
  return '$' + (Math.round(value * 100) / 100).toFixed(2);
}
function renderOptimizeWorkspace(optimize) {
  if (!optimize) return '<div class="empty">Workspace optimization evidence is still building.</div>';
  const candidates = optimize.candidates || [];
  const checklist = optimize.checklist || '';
  if (!candidates.length) {
    return `<div class="empty">${esc(optimize.summary || 'No stale chats, worktrees, or runtime cleanup opportunities stood out.')}</div>`;
  }
  const topImpact = optimize.impact_label || 'No savings claim';
  return `<div id="optimizeReward" class="verdict-card" hidden></div>
    <div class="mini-grid" style="margin-bottom:12px">
      <div class="mini"><span class="label">Status</span><strong>${esc(optimize.title || 'Optimize')}</strong></div>
      <div class="mini"><span class="label">Impact signal</span><strong>${esc(topImpact)}</strong></div>
      <div class="mini"><span class="label">Evidence</span><strong>${esc(optimize.evidence_label || 'Observed')}</strong></div>
      <div class="mini"><span class="label">Items</span><strong>${esc(candidates.length)}</strong></div>
    </div>
    <p class="receipt-note" style="margin-bottom:12px">AIWatcher cannot archive or delete anything for you. Review one item, act only in the owning app, then mark it reviewed to quiet the nudge for 24 hours.</p>
    <div class="action-queue">${candidates.map(item => {
      const itemChecklist = item.checklist || checklist;
      return `<div class="action-row ${item.tokens_at_risk ? 'medium' : 'low'}">
      <div>
        <div class="action-title">${esc(item.title)} <span class="pill">${esc(item.evidence_label || 'Observed')}</span></div>
        <p>${esc(item.why_inactive || item.summary || '')}</p>
        <div class="action-meta"><span class="pill" title="${esc(item.project || '')}">${esc(item.project ? projectName({ project_full: item.project }) : 'Local machine')}</span>${item.impact_label ? `<span class="pill">${esc(item.impact_label)}</span>` : ''}<span class="pill">${esc(item.updated_label || '')}</span></div>
        <p class="receipt-note">${esc(item.evidence || '')}</p>
      </div>
      <div class="actions">
        ${item.view ? `<button class="btn-primary" onclick="showView('${esc(item.view)}')">${esc(item.action_label || 'Review')}</button>` : `<button class="btn-primary" onclick="copyText(${jsArg(itemChecklist)}, 'Project review steps copied')">${esc(item.action_label || 'Copy project steps')}</button>`}
        <button class="btn-quiet" data-project="${esc(item.project_full || '')}" data-impact="${esc(item.impact_label || '')}" onclick="recordOptimizeDecision('marked_done', this.dataset.project, this.dataset.impact, this)">Reviewed</button>
        <button class="btn-quiet" data-project="${esc(item.project_full || '')}" data-impact="${esc(item.impact_label || '')}" onclick="recordOptimizeDecision('skipped', this.dataset.project, this.dataset.impact, this)">Skip</button>
      </div>
    </div>`;
    }).join('')}</div>
    <div class="copy-row" style="margin-top:12px">
      <button class="btn-quiet" onclick="copyText(${jsArg(checklist)}, 'Global review queue copied')">Copy all review items</button>
    </div>`;
}
/* ---------------------------------------------------------------------------
   Chart core.

   Hand-rolled inline SVG, because the dashboard is one self-contained HTML
   string with no bundler and no CDN -- adding a chart library to draw a line
   would cost more than it returns. Everything below is shared by every chart on
   the page, so a new one is a data shape plus a call, not another forty lines
   of scale arithmetic.

   Conventions the whole page relies on:
   - colours come from CSS custom properties, never literals, so both themes work
   - `vector-effect="non-scaling-stroke"` keeps a 2px line 2px after the viewBox
     is scaled to the container
   - every chart ships a table view; nothing is encoded in colour alone
--------------------------------------------------------------------------- */
const SVG_NS = 'http://www.w3.org/2000/svg';
// Past this the projection is drawn no further and the caption says "N+". A
// straight line forty turns out is already a stretch; a hundred is a fiction.
const RUNWAY_MAX_PROJECTED_TURNS = 40;
function svgEl(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const key in attrs) node.setAttribute(key, attrs[key]);
  return node;
}
function chartToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function chartScale(domainMin, domainMax, rangeMin, rangeMax) {
  const span = (domainMax - domainMin) || 1;
  return value => rangeMin + ((value - domainMin) / span) * (rangeMax - rangeMin);
}
function chartText(parent, x, y, content, opts) {
  const options = opts || {};
  const node = svgEl('text', {
    x: x, y: y,
    'text-anchor': options.anchor || 'middle',
    fill: chartToken(options.fill || '--muted'),
    'font-family': options.mono === false
      ? "system-ui, -apple-system, 'Segoe UI', sans-serif"
      : 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    'font-size': options.size || 11,
    'font-weight': options.weight || 400,
  });
  node.textContent = content;
  parent.appendChild(node);
  return node;
}
function chartGrid(parent, plot, ticks, formatValue, yScale) {
  ticks.forEach(value => {
    const y = yScale(value);
    parent.appendChild(svgEl('line', {
      x1: plot.left, y1: y, x2: plot.right, y2: y,
      stroke: chartToken('--line'), 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke',
    }));
    chartText(parent, plot.left - 8, y + 4, formatValue(value), { anchor: 'end' });
  });
}
function chartPath(points) {
  return 'M' + points.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('L');
}
function chartLine(parent, points, token, opts) {
  const options = opts || {};
  parent.appendChild(svgEl('path', {
    d: chartPath(points),
    fill: 'none',
    stroke: chartToken(token),
    'stroke-width': options.width || 2,
    'stroke-linejoin': 'round',
    'stroke-linecap': 'round',
    'vector-effect': 'non-scaling-stroke',
    ...(options.dash ? { 'stroke-dasharray': options.dash } : {}),
    ...(options.opacity ? { opacity: options.opacity } : {}),
  }));
}

/* Runway: how many turns before this session reaches the action threshold.
   Two things this deliberately does NOT do. It never extrapolates across a
   context reset -- the projection uses growth since the last one, because a
   line drawn through a reset predicts a wall the session already stepped back
   from. And it draws the session peak, because severity fires on `latest > X
   OR peak > X`: a session that crossed before a reset still reads critical on
   the card while sitting well below the line now, and hiding that makes the
   card and the chart look like they disagree. */
/* Position and shape are two questions, and one chart answers them badly.
   Forcing a common y-axis across projects -- so the limit sits on one row and the
   column can be scanned -- flattened three of four series to under 5px of travel,
   because a project 690k deep barely moves in relative terms. So they split: the
   meter carries position on a scale every card shares, and the trend keeps its
   own scale, where the slope is legible. */
function drawMeter(node, chart) {
  if (!node || !chart) return;
  const critical = chart.critical_tokens_n || 0;
  const pressure = chart.pressure_tokens_n || 0;
  const latest = chart.latest_turn_tokens_n || 0;
  if (!critical) return;
  const W = 1000, H = 20, track = critical * 1.25;
  const at = value => Math.min(1, value / track) * W;
  const tone = latest >= critical ? 'var(--red)' : latest >= pressure ? 'var(--amber)' : 'var(--green)';
  const parts = [
    `<rect x="0" y="6" width="${W}" height="9" rx="4.5" fill="var(--surface)"/>`,
    `<rect x="0" y="6" width="${at(Math.min(latest, critical)).toFixed(1)}" height="9" rx="4.5" fill="${tone}"/>`,
    `<line x1="${at(pressure).toFixed(1)}" x2="${at(pressure).toFixed(1)}" y1="1" y2="19" stroke="var(--amber)" stroke-width="2"/>`,
    `<line x1="${at(critical).toFixed(1)}" x2="${at(critical).toFixed(1)}" y1="1" y2="19" stroke="var(--red)" stroke-width="2"/>`,
  ];
  node.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="meter-svg" role="img"
    aria-label="${esc(compactTokens(latest))} per turn against a ${esc(compactTokens(critical))} limit">${parts.join('')}</svg>`;
  node.setAttribute('data-over', latest > critical ? (latest / critical).toFixed(1) : '');
}

/* Context per turn against the thresholds that decide what it means.

   The old chart scaled y to the data's own min and max, which is the most
   natural thing to do and was badly wrong here. A session sitting at 819k
   against a 200k limit drew as a gentle 7% rise, because 752k-810k filled the
   canvas and the two thresholds were nowhere on it. The reader could not see
   the one fact that matters -- that this is four times over the line -- because
   the line was not drawn. Same defect as any true number answering a question
   it was not asked.

   So the y domain always contains both thresholds. A healthy session sits low
   in the frame with room above it; an overrun sits near the top with the limits
   compressed at the bottom, which is what being far past them looks like.

   Rendered 1:1 rather than through a stretched viewBox: the old one used a
   fixed 1000x60 box scaled to fit, so in a narrow card the whole plot collapsed
   into a 14px band floating in the middle of its container. */
const TREND_HEIGHT = 152;
const TREND_COMPACT_HEIGHT = 26;

/* Drawn 1:1, so it has to be redrawn when its width changes -- including the
   first time it becomes visible. A chart built while its view is still hidden
   measures zero, falls back to a default width, and is then stretched by CSS to
   whatever the container turns out to be: the old one rendered 152px tall as
   254px that way. One observer per host, redrawing only on a real width change
   so it cannot drive itself. */
const _trendObserved = new WeakSet();

function observeTrend(node) {
  if (_trendObserved.has(node) || typeof ResizeObserver === 'undefined') return;
  _trendObserved.add(node);
  const observer = new ResizeObserver(() => {
    const width = Math.round(node.getBoundingClientRect().width);
    if (!width || width === Number(node.dataset.drawnAt)) return;
    const chart = _trendCharts.get(node);
    if (chart) drawTrend(node, chart);
  });
  observer.observe(node);
}

const _trendCharts = new WeakMap();

function drawTrend(node, chart) {
  if (!node || !chart) return;
  const series = chart.turn_series || [];
  if (series.length < 2) return;
  _trendCharts.set(node, chart);
  observeTrend(node);

  const compact = node.classList.contains('health-row-trend');
  const measured = Math.round(node.getBoundingClientRect().width);
  // Hidden view: leave it for the observer rather than drawing at a guessed size.
  if (!measured && node.dataset.drawnAt) return;
  const W = Math.max(160, measured || 640);
  const H = compact ? TREND_COMPACT_HEIGHT : TREND_HEIGHT;

  const pressure = Number(chart.pressure_tokens_n) || 0;
  const critical = Number(chart.critical_tokens_n) || 0;

  // The scale stays the data's own, because shape is this chart's whole job --
  // the meter above it already carries position against the limit, on a scale
  // shared with every other card. Stretching y down to include a 200k threshold
  // when the session runs at 838k squeezes sixty turns of movement into six
  // percent of the frame, which is the flattening the meter/trend split was
  // made to avoid. What was missing was never the thresholds; it was any way to
  // read the magnitudes at all.
  const lo = Math.min(...series);
  const hi = Math.max(...series);
  const headroom = Math.max((hi - lo) * 0.18, hi * 0.02, 1);
  const top = hi + headroom;
  const base = Math.max(0, lo - headroom);
  const span = Math.max(1, top - base);

  const padL = compact ? 0 : 46;      // room for the y labels
  const padR = compact ? 0 : 8;
  const padT = compact ? 3 : 10;
  const padB = compact ? 3 : 18;      // room for the turn labels
  const plotW = Math.max(1, W - padL - padR);
  const plotH = Math.max(1, H - padT - padB);

  const x = i => padL + (i / (series.length - 1)) * plotW;
  const y = v => padT + (1 - (Math.min(Math.max(v, base), top) - base) / span) * plotH;

  const latest = Number(chart.latest_turn_tokens_n) || series[series.length - 1];
  const tone = critical && latest >= critical ? 'var(--red)'
    : pressure && latest >= pressure ? 'var(--amber)' : 'var(--green)';

  const points = series.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `${padL},${(padT + plotH).toFixed(1)} ${points} ${(padL + plotW).toFixed(1)},${(padT + plotH).toFixed(1)}`;
  const delta = series[series.length - 1] - series[0];

  // A threshold inside the drawn range gets a line. One outside it gets said in
  // words at the edge it fell off, because silently omitting it is how a reader
  // concludes the axis starts at zero and that they are comfortably under.
  const thresholds = [
    { value: pressure, colour: 'var(--amber)', name: 'pressure' },
    { value: critical, colour: 'var(--red)', name: 'act now' },
  ].filter(m => m.value > 0);
  const marks = compact ? [] : thresholds.filter(m => m.value >= base && m.value <= top);
  const below = compact ? [] : thresholds.filter(m => m.value < base);

  const gridline = m => `
    <line x1="${padL}" y1="${y(m.value).toFixed(1)}" x2="${(padL + plotW).toFixed(1)}" y2="${y(m.value).toFixed(1)}"
      stroke="${m.colour}" stroke-width="1" stroke-dasharray="3 4" opacity=".55"/>
    <text x="${padL - 6}" y="${(y(m.value) + 3.5).toFixed(1)}" text-anchor="end"
      class="trend-tick" fill="${m.colour}">${esc(compactTokens(m.value))}</text>`;

  node.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" class="trend-svg" role="img"
    aria-label="Context per turn across ${series.length} turns, ${delta >= 0 ? 'rising' : 'falling'} ${esc(compactTokens(Math.abs(delta)))}, latest ${esc(compactTokens(latest))} against a ${esc(compactTokens(critical))} limit">
    ${compact ? '' : `<line x1="${padL}" y1="${(padT + plotH).toFixed(1)}" x2="${(padL + plotW).toFixed(1)}" y2="${(padT + plotH).toFixed(1)}" stroke="var(--line)" stroke-width="1"/>`}
    ${marks.map(gridline).join('')}
    <polygon points="${area}" fill="${tone}" opacity=".10"/>
    <polyline points="${points}" fill="none" stroke="${tone}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(series.length - 1).toFixed(1)}" cy="${y(series[series.length - 1]).toFixed(1)}" r="4"
      fill="${tone}" stroke="var(--bg)" stroke-width="2"/>
    ${compact ? '' : `
      <text x="${padL - 6}" y="${(padT + 4).toFixed(1)}" text-anchor="end" class="trend-tick">${esc(compactTokens(Math.round(top)))}</text>
      <text x="${padL - 6}" y="${(padT + plotH).toFixed(1)}" text-anchor="end" class="trend-tick">${esc(compactTokens(Math.round(base)))}</text>
      ${below.length ? `<text x="${padL}" y="${(padT + plotH - 5).toFixed(1)}" class="trend-tick" fill="${below[below.length - 1].colour}">${esc(below.map(m => compactTokens(m.value)).join(' and '))} ${below.length > 1 ? 'limits are' : 'limit is'} below this range</text>` : ''}
      <text x="${padL}" y="${H - 5}" class="trend-tick" fill="var(--faint)">turn 1</text>
      <text x="${(padL + plotW).toFixed(1)}" y="${H - 5}" text-anchor="end" class="trend-tick" fill="var(--faint)">turn ${series.length}</text>
      <text x="${(padL + plotW).toFixed(1)}" y="${Math.max(12, y(series[series.length - 1]) - 9).toFixed(1)}" text-anchor="end"
        class="trend-value" fill="${tone}">${esc(compactTokens(latest))}</text>`}
    <g class="trend-hover" hidden>
      <line class="trend-crosshair" y1="${padT}" y2="${(padT + plotH).toFixed(1)}" stroke="var(--line-strong)" stroke-width="1"/>
      <circle class="trend-cursor" r="3.5" fill="var(--bg)" stroke="${tone}" stroke-width="2"/>
    </g>
    <rect x="${padL}" y="${padT}" width="${plotW}" height="${plotH}" fill="transparent" class="trend-capture"/>
  </svg>${compact ? '' : '<div class="trend-tip" hidden></div>'}`;

  node.dataset.drawnAt = String(W);
  if (compact) return;
  attachTrendHover(node, series, { padL, plotW, padT, plotH, x, y, top });
}

/* Crosshair and tooltip. A line chart that cannot be read point by point makes
   the reader guess at the values between its endpoints. */
function attachTrendHover(node, series, geom) {
  const svg = node.querySelector('svg');
  const capture = node.querySelector('.trend-capture');
  const group = node.querySelector('.trend-hover');
  const crosshair = node.querySelector('.trend-crosshair');
  const cursor = node.querySelector('.trend-cursor');
  const tip = node.querySelector('.trend-tip');
  if (!svg || !capture || !group || !tip) return;

  const show = event => {
    const box = svg.getBoundingClientRect();
    const scale = box.width / svg.viewBox.baseVal.width || 1;
    const local = (event.clientX - box.left) / scale;
    const ratio = (local - geom.padL) / geom.plotW;
    const index = Math.max(0, Math.min(series.length - 1, Math.round(ratio * (series.length - 1))));
    const px = geom.x(index);
    const py = geom.y(series[index]);
    group.removeAttribute('hidden');
    crosshair.setAttribute('x1', px.toFixed(1));
    crosshair.setAttribute('x2', px.toFixed(1));
    cursor.setAttribute('cx', px.toFixed(1));
    cursor.setAttribute('cy', py.toFixed(1));
    tip.hidden = false;
    tip.textContent = `turn ${index + 1} · ${compactTokens(series[index])}`;
    // Flip before the tooltip runs off the right edge rather than after.
    const tipW = tip.getBoundingClientRect().width || 90;
    const left = px * scale;
    tip.style.left = `${Math.max(0, Math.min(box.width - tipW, left - tipW / 2))}px`;
  };
  const hide = () => { group.setAttribute('hidden', ''); tip.hidden = true; };

  capture.addEventListener('mousemove', show);
  capture.addEventListener('mouseleave', hide);
}


/* The runway verdict as {headline, detail, severity}, so Home and the full card
   render the same judgement from one place. Two surfaces computing "how long
   have I got" independently is how they end up disagreeing. */
function runwayVerdict(chart) {
  if (!chart || (chart.turn_series || []).length < 3) return null;
  // turns_to_critical is null for two opposite reasons and they must not share a
  // sentence: a session already past the threshold is the worst case on the page,
  // and describing it as "not on a path to" the threshold reads as reassurance.
  if (chart.turns_to_critical === null || chart.turns_to_critical === undefined) {
    if (chart.latest_turn_tokens_n >= chart.critical_tokens_n) {
      return {
        severity: 'critical',
        headline: 'Already past the action threshold',
        detail: `${compactTokens(chart.latest_turn_tokens_n)} per turn against a ${compactTokens(chart.critical_tokens_n)} limit. There is no headroom left to project.`,
      };
    }
    return {
      severity: 'healthy',
      headline: 'Not growing right now',
      detail: 'Context is flat, so there is no threshold to project towards.',
    };
  }
  // The drawn projection is capped, so nothing may quote a number the chart does
  // not reach -- and past this range the honest reading is "plenty".
  if (chart.turns_to_critical > RUNWAY_MAX_PROJECTED_TURNS) {
    return {
      severity: 'healthy',
      headline: `${RUNWAY_MAX_PROJECTED_TURNS}+ turns of headroom`,
      detail: `At ${compactTokens(chart.growth_per_turn_n)}/turn. Far enough out that the exact number is noise.`,
    };
  }
  return {
    severity: chart.turns_to_critical <= 10 ? 'critical' : 'warning',
    headline: `≈${chart.turns_to_critical} turn${chart.turns_to_critical === 1 ? '' : 's'} of headroom`,
    detail: `At ${compactTokens(chart.growth_per_turn_n)}/turn, the growth since this session last shed context.`,
  };
}
/* Names every line on the runway chart. The caption used to carry "Amber is
   pressure, red is where action is needed" inside a conditional that dropped it
   on critical cards with no projection left -- so the explanation vanished from
   exactly the sessions in the worst state, which are the ones a reader most
   needs to be able to read. Blue was never named anywhere at all.

   Only lines that were actually drawn are listed: drawRunway omits the
   projection when there is no headroom left, and the peak line when the peak is
   the current turn, and a legend naming an absent line sends you hunting for it. */

/* Daily shape under a metric tile. Deliberately unlabelled: there are no axes,
   no ticks and no gridlines, because the tile already carries the number and
   the only question left is which way it has been going.

   Colour is inherited (currentColor) from the card's --metric, so the series
   never picks a token of its own -- see .metric-spark.

   A series may stop short of the right edge. Outcomes only exist for sessions
   somebody has judged, and drawing a line through the unjudged tail would state
   that recent work produced nothing when the truth is that nobody has looked at
   it yet. The tail is shaded and left empty instead, and the caveat says so in
   words rather than relying on the shading being noticed. */
function drawTileSpark(node, series, days) {
  if (!node || !series) return false;
  const values = series.values || [];
  if (values.length < 3) return false;
  const W = 240, H = 28, pad = 3;
  // Drawn only as far as there is a verdict; the rest of the axis is still laid
  // out, so the shaded gap is visibly a gap rather than a shorter chart.
  const through = Number.isInteger(series.judged_through) ? series.judged_through : values.length - 1;
  const drawn = values.slice(0, through + 1);
  if (drawn.length < 2) return false;
  const peak = Math.max(...drawn);
  if (!(peak > 0)) return false;

  const x = chartScale(0, values.length - 1, pad, W - pad);
  const y = chartScale(0, peak, H - pad, pad);
  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`, class: 'metric-spark-svg',
    preserveAspectRatio: 'none', role: 'img',
  });

  // A faint baseline, so the line has something to be high or low against.
  svg.appendChild(svgEl('line', {
    x1: pad, y1: H - pad, x2: W - pad, y2: H - pad,
    stroke: 'currentColor', 'stroke-width': 1, opacity: 0.25,
  }));

  if (through < values.length - 1) {
    svg.appendChild(svgEl('rect', {
      x: x(through), y: 0, width: (W - pad) - x(through), height: H,
      fill: chartToken('--line'), opacity: 0.35,
    }));
  }

  const points = drawn.map((value, index) => [x(index), y(value)]);
  const area = chartPath(points)
    + `L${x(through).toFixed(1)},${y(0).toFixed(1)}`
    + `L${x(0).toFixed(1)},${y(0).toFixed(1)}Z`;
  svg.appendChild(svgEl('path', { d: area, fill: 'currentColor', opacity: 0.16, stroke: 'none' }));
  svg.appendChild(svgEl('path', {
    d: chartPath(points), fill: 'none', stroke: 'currentColor',
    'stroke-width': 1.75, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    'vector-effect': 'non-scaling-stroke',
  }));
  svg.appendChild(svgEl('circle', {
    cx: points[points.length - 1][0], cy: points[points.length - 1][1], r: 2.4,
    fill: 'currentColor', stroke: chartToken('--surface'), 'stroke-width': 1.5,
  }));

  // The text alternative, so nothing here is carried by the drawing alone.
  const labels = series.labels || [];
  const peakIndex = drawn.indexOf(peak);
  const summary = `Peak ${labels[peakIndex] || peak} on ${(days || [])[peakIndex] || 'the busiest day'}`
    + `, ${labels[through] || drawn[drawn.length - 1]} on ${(days || [])[through] || 'the last day drawn'}.`;
  svg.setAttribute('aria-label', summary);
  const title = svgEl('title', {});
  title.textContent = summary;
  svg.appendChild(title);

  node.innerHTML = '';
  node.appendChild(svg);
  // Always emitted, empty when there is nothing to caveat: two of the four
  // tiles carry one and two do not, which ended those two cards 17px short of
  // their neighbours. The row is meant to be read across, so every card has to
  // end in the same place whatever it happens to have to say.
  const note = document.createElement('div');
  note.className = 'metric-spark-caveat';
  note.textContent = series.caveat || '';
  node.appendChild(note);
  node.hidden = false;
  return true;
}
// Cost per surviving line, as a tile rather than a sentence inside another one.
//
// Q: what did a line of code that is still there actually cost? Unit: dollars
// per surviving line, cumulative over survival's own fixed window. Compared
// against nothing -- there is no threshold for it here, so the rail is identity
// and never status. It discriminates: it moves with both spend and churn, and
// the commit receipt already reads a 30-day median off the same figure.
//
// Survival has its own fixed window (SURVIVAL_WINDOW_DAYS, 30) and ignores the
// date picker entirely -- a commit needs to age past MIN_AGE_DAYS before
// survival means anything, so the figure cannot follow a 24h view. It sat under
// the picker looking filtered and never moved. Naming its real window is the
// honest fix; calling it "all time" would be a second wrong label on the same
// number. The window now sits in the caveat slot, where the sparklines put
// theirs, which keeps the supporting line to one line like every other card.
function renderSurvivalTile(survival) {
  const value = document.getElementById('costPerSurviving');
  const sub = document.getElementById('costPerSurvivingSub');
  const meter = document.getElementById('survivalMeter');
  if (!value || !sub || !meter) return;
  if (!survival.available) {
    // Unmeasured is its own state, not a zero and not a dash presented as an
    // answer. The reason can be a whole sentence with an instruction in it,
    // which wrapped to three lines in a 235px column and pushed the row 31px
    // taller than its measured state -- a reflow the ambient surface is not
    // allowed. So the short form goes on the one supporting line every card
    // gets, and the full reason takes the slot the meter would have used,
    // which is already 45px of reserved height.
    value.textContent = 'not yet';
    value.classList.add('value-unmeasured');
    sub.textContent = 'Not measured yet';
    const reason = String(survival.reason || '').replace(/^Not measured yet\.\s*/, '');
    meter.innerHTML = '';
    const note = document.createElement('div');
    note.className = 'survival-reason';
    note.textContent = reason || 'Reopen the dashboard to compute it.';
    meter.appendChild(note);
    meter.hidden = false;
    return;
  }
  value.textContent = survival.cost_per_surviving_line_label;
  value.classList.remove('value-unmeasured');
  sub.textContent = `${survival.survival_pct}% of ${formatCount(survival.lines_touched)} lines still standing`;
  drawSurvivalMeter(meter, survival);
}
// A meter, not a sparkline: survival is a share of a whole on a fixed 0-100%
// scale, which is position against a shared limit. A trend line would have to
// be drawn over the date picker's window, and that is a different question from
// the one the number answers.
function drawSurvivalMeter(node, survival) {
  const pct = Math.max(0, Math.min(100, Number(survival.survival_pct) || 0));
  const svg = svgEl('svg', {
    viewBox: '0 0 240 28', class: 'metric-spark-svg survival-meter',
    preserveAspectRatio: 'none', role: 'img',
  });
  svg.appendChild(svgEl('rect', { x: 0, y: 10, width: 240, height: 8, rx: 4, fill: 'currentColor', opacity: 0.18 }));
  svg.appendChild(svgEl('rect', { x: 0, y: 10, width: 240 * pct / 100, height: 8, rx: 4, fill: 'currentColor' }));
  const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
  title.textContent = `${survival.survival_pct}% of the lines measured are still standing.`;
  svg.appendChild(title);
  node.innerHTML = '';
  node.appendChild(svg);
  const note = document.createElement('div');
  note.className = 'metric-spark-caveat';
  const window_ = survival.window_days ? `last ${survival.window_days} days` : 'a fixed window';
  note.textContent = `${window_} · not the selected range`;
  node.appendChild(note);
  node.hidden = false;
}
// An empty slot rather than no slot. The Useful outcomes tile has too few judged
// points to plot at short ranges, and hiding its sparkline made that one card a
// different height from its neighbours -- the row's alignment shifted when the
// reader changed the date range, which reads as a failed load.
function drawTileSparkPlaceholder(node) {
  if (!node) return;
  const svg = svgEl('svg', {
    viewBox: '0 0 240 28', class: 'metric-spark-svg metric-spark-empty',
    preserveAspectRatio: 'none', role: 'img',
  });
  svg.appendChild(svgEl('line', { x1: 3, y1: 25, x2: 237, y2: 25, stroke: 'currentColor', 'stroke-width': 1, opacity: 0.35 }));
  const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
  title.textContent = 'Not enough data yet for a trend.';
  svg.appendChild(title);
  node.innerHTML = '';
  node.appendChild(svg);
  const note = document.createElement('div');
  note.className = 'metric-spark-caveat';
  note.textContent = 'not enough data yet';
  node.appendChild(note);
  node.hidden = false;
}

function compactTokens(n) {
  if (!n) return '0';
  // Carries past thousands: the scatter's decade ticks reach hundreds of
  // millions, and "100000K" is not a number anyone reads.
  if (n >= 1e9) return +(n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + 'B';
  if (n >= 1e6) return +(n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
  // Lowercase k with one decimal, matching ui.py's compact_int. They disagreed:
  // Python rendered 564.2k and this rendered 564K, and both appeared in the same
  // Watch card. Four call sites had grown a  to paper over it.
  if (n >= 1000) return +(n / 1000).toFixed(1) + 'k';
  return String(Math.round(n));
}

/* One horizontal bar split by category, with the legend carrying exact values.
   Shared rather than local to the unbanked card because it is the same object
   any part-to-whole question needs. Segment colours come from the caller so
   identity stays with the entity, never with its rank in the list. */
/* Share of a whole, which the ranked bars beside it cannot show -- each of those
   is sized against the largest row, not against the total.

   Slices are separated by a stroke in the surface colour rather than a border,
   and only slices wide enough to hold one get a label inside; the rest rely on
   the legend, which carries every value in full. */
const COMPOSITION_COLOURS = ['--blue', '--cyan', '--green'];
function compositionColours(segments) {
  let index = 0;
  return segments.map(segment =>
    segment.kind === 'other' ? '--faint' : COMPOSITION_COLOURS[index++ % COMPOSITION_COLOURS.length]);
}
function drawPie(node, chart) {
  if (!node || !chart || !chart.segments || chart.segments.length < 2) return;
  const colours = compositionColours(chart.segments);
  const size = 132, r = 62, cx = size / 2, cy = size / 2;
  const svg = svgEl('svg', { viewBox: `0 0 ${size} ${size}`, class: 'pie-svg', 'aria-hidden': 'true' });
  let angle = -Math.PI / 2;

  chart.segments.forEach((segment, index) => {
    const sweep = (segment.pct / 100) * Math.PI * 2;
    const end = angle + sweep;
    const large = sweep > Math.PI ? 1 : 0;
    const x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
    const x2 = cx + r * Math.cos(end), y2 = cy + r * Math.sin(end);
    // A single slice covering the whole circle has identical start and end
    // points, which collapses the arc to nothing -- draw the circle instead.
    const path = sweep >= Math.PI * 2 - 0.0001
      ? svgEl('circle', { cx: cx, cy: cy, r: r, fill: chartToken(colours[index]) })
      : svgEl('path', {
          d: `M${cx},${cy}L${x1.toFixed(2)},${y1.toFixed(2)}A${r},${r} 0 ${large} 1 ${x2.toFixed(2)},${y2.toFixed(2)}Z`,
          fill: chartToken(colours[index]),
          stroke: chartToken('--surface'), 'stroke-width': 2,
        });
    svg.appendChild(path);
    if (sweep > 0.55) {
      const mid = angle + sweep / 2;
      chartText(svg, cx + r * 0.62 * Math.cos(mid), cy + r * 0.62 * Math.sin(mid) + 4,
        // 600 is --fw-med; 700 was the one weight left outside the three-step scale.
        Math.round(segment.pct) + '%', { fill: '--surface', weight: 600, size: 12 });
    }
    angle = end;
  });
  node.innerHTML = '';
  node.appendChild(svg);
}
function compositionLegend(chart) {
  if (!chart) return '';
  const colours = compositionColours(chart.segments);
  const rows = chart.segments.map((segment, index) => `<span class="bar-key"${segment.title ? ` title="${esc(segment.title)}"` : ''}>
      <span class="bar-swatch" style="background:var(${colours[index]})"></span>
      ${esc(segment.label)} <strong>${esc(segment.pct)}%</strong>
      <span class="bar-pct">${esc(segment.tokens_label)}</span>
    </span>`).join('');
  // Said rather than hidden: a chart that is one colour is a number, and the
  // reader should be told that instead of squinting at it.
  const caveat = chart.dominant
    ? `<p class="receipt-note">One slice is ${esc(chart.dominant_pct)}% of the total, so this is really a single figure rather than a breakdown.</p>`
    : '';
  return `<div class="bar-legend">${rows}</div>${caveat}`;
}

/* Markup first, SVG appended after -- the same two-step every chart here uses,
   because appending into an element that does not exist yet draws nothing. */
/* One stacked bar per tool, split by model. The colour map is built once from
   the shared legend and reused for every row, so a model keeps its colour down
   the whole chart -- the eye follows it across tools, which is the only reason
   to cross these two lists in the first place. */
/* Sessions as dots: tokens across, cost up, model by colour, landed by fill.

   Both axes are logarithmic because the data spans four orders of magnitude and
   would otherwise pile into one corner. The consequence worth knowing while
   reading it: price per token is a vertical offset here, not a slope. Two models
   at the same rate lie on the same diagonal; a dearer one sits above a cheaper
   one rather than climbing faster. */
function drawModelScatter(node, scatter) {
  if (!node || !scatter || !scatter.points || !scatter.points.length) return;
  const colours = compositionColours(scatter.legend);
  const colourFor = model => {
    const index = scatter.legend.findIndex(entry => entry.label === model);
    return colours[index < 0 ? scatter.legend.length - 1 : index];
  };

  const W = 720, H = 300, plot = { left: 54, right: 700, top: 18, bottom: 236 };
  const xs = scatter.points.map(p => Math.log10(Math.max(1, p.tokens)));
  const ys = scatter.points.map(p => Math.log10(Math.max(0.01, p.cost_usd)));
  const pad = 0.15;
  const x = chartScale(Math.min(...xs) - pad, Math.max(...xs) + pad, plot.left, plot.right);
  const y = chartScale(Math.min(...ys) - pad, Math.max(...ys) + pad, plot.bottom, plot.top);

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'scatter-svg', 'aria-hidden': 'true' });

  // Decade gridlines, so the log scale is visible rather than implied.
  for (let decade = Math.ceil(Math.min(...ys)); decade <= Math.floor(Math.max(...ys)); decade++) {
    const yy = y(decade);
    svg.appendChild(svgEl('line', {
      x1: plot.left, y1: yy, x2: plot.right, y2: yy,
      stroke: chartToken('--line'), 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke',
    }));
    chartText(svg, plot.left - 8, yy + 4, '$' + (decade < 0 ? Math.pow(10, decade).toFixed(2) : Math.pow(10, decade)), { anchor: 'end' });
  }
  for (let decade = Math.ceil(Math.min(...xs)); decade <= Math.floor(Math.max(...xs)); decade++) {
    const xx = x(decade);
    svg.appendChild(svgEl('line', {
      x1: xx, y1: plot.top, x2: xx, y2: plot.bottom,
      stroke: chartToken('--line'), 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke',
    }));
    chartText(svg, xx, plot.bottom + 18, compactTokens(Math.pow(10, decade)));
  }
  chartText(svg, (plot.left + plot.right) / 2, plot.bottom + 38, 'tokens in session');

  const placed = scatter.points.map(point => {
    const colour = chartToken(colourFor(point.model === 'other' ? 'other models' : point.model));
    const cx = x(Math.log10(Math.max(1, point.tokens)));
    const cy = y(Math.log10(Math.max(0.01, point.cost_usd)));
    // Filled means the work landed. Hollow is deliberately the same size, so
    // the eye reads outcome and not magnitude from the difference.
    svg.appendChild(svgEl('circle', {
      cx: cx, cy: cy, r: 5,
      fill: point.landed ? colour : 'none',
      stroke: colour, 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke',
      opacity: point.landed === null ? 0.35 : 1,
    }));
    return { point, cx, cy };
  });

  // Hit targets are drawn last so they sit above every dot, and are far larger
  // than the 5px mark -- a scatter you have to hit dead-centre is unusable.
  // Listeners are attached to nodes rather than written into an onclick string,
  // so a session id never has to be escaped into markup.
  placed.forEach(({ point, cx, cy }) => {
    const hit = svgEl('circle', { cx: cx, cy: cy, r: 12, class: 'scatter-hit' });
    const outcome = point.landed === null
      ? 'no evidence recorded yet'
      : (point.landed ? 'produced a commit still on the branch' : 'produced nothing that lasted');
    const title = document.createElementNS(SVG_NS, 'title');
    title.textContent =
      `${point.project} — ${point.model_label}\n${point.tokens_label} tokens · ${point.cost_label}\n${outcome}\nClick to open this session`;
    hit.appendChild(title);
    hit.addEventListener('click', () => selectSession(point.session_id));
    svg.appendChild(hit);
  });

  svg.appendChild(svgEl('line', {
    x1: plot.left, y1: plot.bottom, x2: plot.right, y2: plot.bottom,
    stroke: chartToken('--line-strong'), 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke',
  }));
  node.innerHTML = '';
  node.appendChild(svg);
}
function paintModelScatter(scatter) {
  const host = document.getElementById('modelScatter');
  if (!host) return;
  host.hidden = !scatter;
  if (!scatter) return;
  const colours = compositionColours(scatter.legend);
  // Three outcome states are drawn, so three are named. The faded key only
  // appears when something is actually faded -- a key for a mark that is not on
  // the chart invites the reader to hunt for one.
  const unexamined = scatter.unexamined > 0
    ? '<span class="bar-key"><span class="scatter-key unexamined"></span>not judged yet</span>'
    : '';
  document.getElementById('modelScatterLegend').innerHTML =
    scatter.legend.map((entry, index) =>
      `<span class="bar-key"><span class="bar-swatch" style="background:var(${colours[index]})"></span>${esc(entry.label)}</span>`).join('')
    + '<span class="bar-key"><span class="scatter-key filled"></span>work landed</span>'
    + '<span class="bar-key"><span class="scatter-key"></span>did not</span>'
    + unexamined;
  renderScatterWithheld(scatter.unpriced);
  drawModelScatter(host.querySelector('[data-scatter]'), scatter);
}

// Says what the chart is not showing, in the chart's own terms: how many
// sessions, whose, and how much work they did. Tokens rather than dollars
// because the dollars are exactly what is missing.
function renderScatterWithheld(unpriced) {
  const note = document.getElementById('modelScatterWithheld');
  if (!note) return;
  const count = unpriced && unpriced.sessions ? unpriced.sessions : 0;
  note.hidden = count === 0;
  if (!count) { note.textContent = ''; return; }
  // No esc(): this is written with textContent, which escapes for us. Passing
  // esc'd text through it would print the entities themselves.
  const tools = unpriced.tools || [];
  const whose = tools.length
    ? (tools.length === 1 ? tools[0] : tools.slice(0, -1).join(', ') + ' and ' + tools[tools.length - 1])
    : 'plan-based tools';
  note.textContent =
    `${count} ${count === 1 ? 'session' : 'sessions'} not drawn — ${unpriced.tokens_label} tokens on `
    + `${whose}, billed by a plan rather than per token. Local logs cannot price them, and a `
    + `logarithmic cost axis has no floor to put them on; drawn at the bottom they would read as `
    + `nearly free work rather than unpriced work.`;
}

function renderToolModels(breakdown) {
  if (!breakdown || !breakdown.tools || !breakdown.tools.length) return '';
  const colours = compositionColours(breakdown.legend);
  const key = breakdown.legend.map((entry, index) =>
    `<span class="bar-key"><span class="bar-swatch" style="background:var(${colours[index]})"></span>${esc(entry.label)}</span>`).join('');
  const rows = breakdown.tools.map((tool, index) => `<div class="tm-row">
      <div class="tm-head">
        <span class="tm-name">${esc(tool.tool)}</span>
        <span class="tm-total">${esc(tool.total_label)}</span>
      </div>
      <div class="tm-bar" data-tool-models="${index}"></div>
      <div class="tm-note">${tool.segments.filter(s => s.tokens > 0)
        .map(s => `${esc(s.label)} ${esc(s.pct)}%`).join(' &middot; ')}</div>
    </div>`).join('');
  return `<div class="bar-legend tm-legend">${key}</div>${rows}`;
}
function paintToolModels(breakdown) {
  const host = document.getElementById('toolModels');
  if (!host) return;
  host.hidden = !breakdown || !breakdown.tools || !breakdown.tools.length;
  host.innerHTML = renderToolModels(breakdown);
  if (host.hidden) return;
  const colours = compositionColours(breakdown.legend);
  breakdown.tools.forEach((tool, index) => {
    // Zero-token segments are dropped before drawing: a model a tool never ran
    // should contribute no sliver and no 2px gap.
    const segments = tool.segments.filter(segment => segment.tokens > 0);
    const segmentColours = segments.map(segment =>
      colours[breakdown.legend.findIndex(entry => entry.label === segment.label)]);
    const node = host.querySelector(`[data-tool-models="${index}"]`);
    if (segments.length === 1) {
      // drawStackedBar needs two segments to be a stack; one model is a solid
      // bar, which is the honest picture for a single-model tool.
      node.innerHTML = `<div class="tm-solid" style="background:var(${segmentColours[0]})"></div>`;
      return;
    }
    drawStackedBar(node, segments.map(s => ({ ...s, usd: s.tokens })), segmentColours);
  });
}

function paintComposition(name, chart) {
  const host = document.getElementById(name + 'Composition');
  if (!host) return;
  const withhold = !chart || (chart.dominant && chart.hide_when_dominant);
  host.hidden = withhold;
  if (withhold) return;
  document.getElementById(name + 'CompositionLegend').innerHTML = compositionLegend(chart);
  drawPie(host.querySelector('[data-pie]'), chart);
}

function drawStackedBar(node, segments, colours) {
  if (!node || !segments || segments.length < 2) return;
  const W = 720, H = 30, gap = 2, radius = 3;
  const total = segments.reduce((sum, item) => sum + item.usd, 0);
  if (total <= 0) return;

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'stacked-bar', preserveAspectRatio: 'none', 'aria-hidden': 'true' });
  let x = 0;
  segments.forEach((segment, index) => {
    const full = (segment.usd / total) * W;
    // A 2px surface gap separates segments; never a border, which would read as
    // part of the data at this height.
    const width = Math.max(0, full - (index < segments.length - 1 ? gap : 0));
    svg.appendChild(svgEl('rect', {
      x: x, y: 0, width: width, height: H, rx: radius,
      fill: chartToken(colours[index]),
    }));
    x += full;
  });
  node.innerHTML = '';
  node.appendChild(svg);
}
function stackedBarLegend(segments, colours) {
  return segments.map((segment, index) => `<span class="bar-key"${segment.title ? ` title="${esc(segment.title)}"` : ''}>
      <span class="bar-swatch" style="background:var(${colours[index]})"></span>
      ${esc(segment.label)} <strong>${esc(segment.label_usd)}</strong>
      <span class="bar-pct">${esc(segment.pct)}%</span>
    </span>`).join('');
}
/* Colour follows what a segment *is*, not where it landed in the list, so a
   quiet week that drops a repo cannot repaint the survivors. Repos take the
   three non-status hues in order; the tail is neutral because it is a leftover
   rather than a project; "outside any repo" takes amber because it is the one
   segment with a different fix -- a session started in the wrong directory,
   not exploration that went nowhere. */
const UNBANKED_REPO_COLOURS = ['--blue', '--cyan', '--green'];
function unbankedColours(segments) {
  let repo = 0;
  return segments.map(segment => {
    if (segment.kind === 'outside') return '--amber';
    if (segment.kind === 'other') return '--faint';
    return UNBANKED_REPO_COLOURS[repo++ % UNBANKED_REPO_COLOURS.length];
  });
}

// The last two path segments, always. The same project appeared three ways in
// four adjacent rows: "...s/tadan/Downloads/AgentWatch/aiwatcher-local-public",
// "...ers/tadan/Downloads/..." and an untouched Windows path. The third was
// this function failing to split a Windows path -- it matched forward slashes
// only, so a backslash path came back whole. Two segments rather than one
// because the leaf alone does not separate aiwatcher-local-public from
// aiwatcher-local-pr46 at a glance.
function projectName(row) {
  const full = String((row && (row.project_full || row.project)) || '');
  const parts = full.split(/[\\/]+/).filter(Boolean);
  if (!parts.length) return 'unknown';
  return parts.slice(-2).join('/');
}

function projectTitle(row) {
  return String((row && (row.project_full || row.project)) || 'unknown');
}

function healthProjectName(row) {
  return projectName(row);
}

function healthRank(row, waiting) {
  // "Which one needs you first" -- so a session that is literally waiting on
  // your input outranks a bigger one that is still making progress. Size was
  // the only key here, which answered "which is worst" instead: a session
  // blocked on a question you have not seen is the one that cannot continue
  // without you, whatever its per-turn number.
  //
  // Then the old order, unchanged: past the limit outranks below it, and within
  // each group the bigger per-turn number leads.
  const chart = row.chart || {};
  const critical = chart.critical_tokens_n || Infinity;
  const latest = chart.latest_turn_tokens_n || 0;
  return [waiting && waiting.has(row.session_id) ? 1 : 0, latest >= critical ? 1 : 0, latest];
}
/* Why this row sits where it does.
 *
 * The ranking is three keys deep and none of them were visible: a reader saw
 * an order and had to trust it. Each reason states the key that actually put
 * the row here, in the same precedence the sort uses, so the explanation
 * cannot drift from the ordering it explains.
 */
function healthReason(row, waitingById) {
  const entry = waitingById && waitingById.get(row.session_id);
  if (entry) {
    const mins = Math.round((entry.idle_seconds || 0) / 60);
    return mins >= 1
      ? `Waiting on you for ${mins}m.`
      : 'Waiting on you.';
  }
  const chart = row.chart || {};
  const critical = chart.critical_tokens_n || 0;
  const latest = chart.latest_turn_tokens_n || 0;
  if (critical && latest >= critical) {
    return `Past the ${compactTokens(critical)} limit at ${esc(row.latest_turn_tokens)} per turn.`;
  }
  if (latest) return `Highest per-turn here at ${esc(row.latest_turn_tokens)}.`;
  return '';
}

function headroomLabel(chart) {
  const turns = chart && chart.turns_to_critical;
  if (turns === null || turns === undefined) {
    return { big: 'No headroom', sub: 'already past the limit' };
  }
  return { big: `${turns} turn${turns === 1 ? '' : 's'}`, sub: 'of headroom at this rate' };
}

function healthFacts(row) {
  const chart = row.chart || {};
  const peakIsHistoric = chart.peak_turn_tokens_n > chart.latest_turn_tokens_n * 1.05;
  // Two scopes used to run together in one dot-separated list, so "68% replay"
  // (the charted session) and "10 sessions" (the whole project) read as one
  // measurement. The card already warns in prose that its buttons act on a
  // single session rather than all of them; the numbers now say so too.
  const session = [
    peakIsHistoric ? `peak ${row.peak_turn_tokens}` : '',
    row.bloat_measurable ? `${row.bloat_label} replay` : '',
    row.bloat_measurable ? `${row.replayed_cost_label} on replay` : '',
  ].filter(Boolean).join(' \u00b7 ');
  const project = [
    `${row.session_count} session${row.session_count === 1 ? '' : 's'}`,
    row.critical_sessions ? `${row.critical_sessions} critical` : '',
  ].filter(Boolean).join(' \u00b7 ');
  // Returned as groups rather than one joined string so the strip can only
  // ever break between the two scopes. Squeezed into a narrow column it broke
  // mid-phrase -- the defect that put "session . 1 critical" on a line of its
  // own -- and splitting the scopes is pointless if a wrap can still put half
  // of one underneath the other.
  return [
    session ? `this session: ${session}` : '',
    project ? `${session ? '\u2014 ' : ''}this project: ${project}` : '',
  ].filter(Boolean);
}

// A health-card button goes where its label says. The kind comes from
// _context_action alongside the label, so the two cannot be changed apart.
// Falls back to the review when a handoff is not available for this row,
// rather than offering "Start fresh" and doing nothing.
function healthActionButton(row, className, label, kind) {
  const effective = (kind === 'handoff' && !row.can_handoff) ? 'review' : kind;
  const text = effective === kind ? label : 'Review session';
  return `<button class="${className}" data-session="${esc(row.session_id)}" data-kind="${esc(effective)}" onclick="runHealthAction(this.dataset.kind, this.dataset.session)">${esc(text)}</button>`;
}

function runHealthAction(kind, sessionId) {
  return kind === 'handoff' ? openHandoff(sessionId) : selectSession(sessionId);
}

function healthLeadCard(row, waitingById) {
  const verdict = runwayVerdict(row.chart);
  const room = headroomLabel(row.chart);
  return `<div class="health-card lead" data-project-full="${esc(row.project_full || '')}">
    <div class="health-head">
      <div><h3 title="${esc(row.project_full || row.project)}">${esc(healthProjectName(row))}</h3><p>${row.session_count > 1
        ? `${esc(row.session_count)} sessions here \u00b7 charted below: <button class="link-inline" data-session="${esc(row.session_id)}" onclick="selectSession(this.dataset.session)">${row.charted_because_live
            ? 'the worst recently active one' : 'the one under most pressure'}</button> (${esc(row.tool)} \u00b7 last active ${esc(row.age_label)} ago)${row.bigger_idle_label
            ? ` \u2014 a larger one, ${esc(row.bigger_idle_label)}, has been quiet for ${esc(row.bigger_idle_age_label)}` : ''}`
        : `<button class="link-inline" data-session="${esc(row.session_id)}" onclick="selectSession(this.dataset.session)">${esc(row.tool)} session</button> \u00b7 last active ${esc(row.age_label)} ago`}</p></div>
      <span class="health-severity ${esc(row.severity)}">${esc(row.severity)}</span>
    </div>
    ${(() => { const why = healthReason(row, waitingById);
      return why ? `<p class="health-why"><b>First:</b> ${why}</p>` : ''; })()}
    ${verdict ? `<p class="health-verdict"><strong>${esc(verdict.headline)}</strong> \u2014 ${esc(verdict.detail)}</p>` : ''}
    <div class="identity-strip">
      <strong>${esc(row.identity_label || 'Historical log only')}</strong>
      <span>${esc(row.tool || 'unknown tool')} · ${esc(row.session_short || row.session_id || 'unknown session')}</span>
      <span>${esc(row.return_label || 'Exact chat unavailable')}</span>
    </div>
    <div class="health-evidence">
      <div class="label">Inferred intent</div>
      <p>${esc(row.intent_summary || row.action.reason || 'Review this source before carrying context forward.')}</p>
      <div class="label">Context AIWatcher will carry</div>
      <p>${esc(row.context_summary || row.recommendation || 'Fresh Start will use local session identity and workspace evidence.')}</p>
    </div>
    <div class="health-hero"><b>${esc(room.big)}</b><u>${esc(room.sub)}</u>
      <span class="health-now">${esc(row.latest_turn_tokens)} per turn</span></div>
    <div class="meter-host" data-meter="${esc(row.session_id)}"></div>
    <div class="trend-host" data-trend="${esc(row.session_id)}"></div>
    <p class="health-facts">${healthFacts(row).map(group => `<span>${esc(group)}</span>`).join('')}</p>
    <div class="health-actions">
      ${healthActionButton(row, 'btn-primary', row.action.label, row.action.kind)}
      ${row.can_handoff ? healthActionButton(row, 'btn-quiet', row.action.secondary_label, row.action.secondary_kind) : ''}
      ${row.can_handoff ? `<button class="btn-quiet" data-session="${esc(row.session_id)}" data-project="${esc(row.project_full || '')}" onclick="continueFreshStartProject(this.dataset.session, this.dataset.project)">Continue 48h</button>` : ''}
      ${row.can_handoff ? `<button class="btn-quiet" data-project="${esc(row.project_full || '')}" onclick="snoozeFreshStartProject(this.dataset.project, this)">Snooze 48h</button>` : ''}
      <button class="btn-quiet" data-compact="${esc(row.compact_prompt || '/compact')}" onclick="copyText(this.dataset.compact, 'Compact prompt copied')">Copy compact prompt</button>
    </div>
    <p class="receipt-note">${esc(row.action.reason)}${row.session_count > 1
      ? ' These act on that one session, not on all ' + esc(row.session_count) + ' in the project.'
      : ''}</p>
  </div>`;
}

function healthQuietRow(row, waitingById) {
  const room = headroomLabel(row.chart);
  return `<button class="health-row" data-session="${esc(row.session_id)}" data-project-full="${esc(row.project_full || '')}" onclick="selectSession(this.dataset.session)">
    <span class="health-row-name" title="${esc(row.project_full || row.project)}">${esc(healthProjectName(row))}
      ${(() => { const why = healthReason(row, waitingById);
        return why ? `<i class="health-row-why">${why}</i>` : ''; })()}</span>
    <span class="health-row-trend" data-trend="${esc(row.session_id)}"></span>
    <span class="health-row-now">${esc(row.latest_turn_tokens)}</span>
    <span class="health-row-room">${esc(room.big)}</span>
    <span class="health-severity ${esc(row.severity)}">${esc(row.severity)}</span>
  </button>`;
}

function renderContextHealth(rows, statusArg, presence) {
  const status = arguments.length > 1 ? arguments[1] : 'ready';
  // Keyed by session so the rank and the reason read the same source; a second
  // list here would be a second thing to keep in step with presence.
  const waitingById = new Map(
    (((presence || {}).sessions) || [])
      .filter(entry => entry.state === 'waiting')
      .map(entry => [entry.session_id, entry]));
  if (status === 'pending') return '<div class="loading">Checking context health and handoff opportunities...</div>';
  if (!rows.length) return '<div class="empty">No active context-health warnings. AIWatcher will surface bloat, stale sessions, and handoff opportunities here.</div>';
  // One project is worth acting on; the rest are worth knowing about. Four cards
  // of equal weight gave the eye nowhere to start.
  const ranked = rows.slice().sort((a, b) => {
    const ar = healthRank(a, new Set(waitingById.keys()));
    const br = healthRank(b, new Set(waitingById.keys()));
    return br[0] - ar[0] || br[1] - ar[1] || br[2] - ar[2];
  });
  const [lead, ...rest] = ranked;
  const snoozable = ranked.filter(row => row.can_handoff && (row.severity === 'critical' || row.severity === 'warning'));
  // Only when there is more than one to act on. The pre-redesign header also
  // carried a heading counting the projects and a "Review list" button; this
  // layout already shows the count and is itself the review list, so only the
  // control with no other home survives. Label kept verbatim from #83 -- it is
  // their copy, and its test is what proves the capability came across.
  const batch = snoozable.length > 1
    ? `<div class="health-batch"><button class="btn-quiet" onclick="snoozeVisibleFreshStartProjects(this)">Snooze all 48h</button></div>`
    : '';
  return `<div class="health-stack">${batch}${healthLeadCard(lead, waitingById)}${rest.map(row => healthQuietRow(row, waitingById)).join('')}</div>`;
}
function renderCoverage(rows) {
  if (!rows.length) return '<div class="empty">Coverage could not be determined on this machine.</div>';
  const modeCopy = {
    automatic: 'Protected automatically when the tool invokes its hook.',
    companion: 'Companion/manual protection. AIWatcher can help, but it is not intercepting this surface directly.',
    limited: 'Partial coverage. Treat findings as local evidence, not full control.',
    unverified: 'Not verified on this machine yet. Run the suggested check before trusting protection claims.',
    not_detected: 'Tool not detected. Install or open the tool, then refresh coverage.',
    unsupported: 'No direct hook known. Use Prompt Companion or history-only review.',
  };
  return rows.map(row => `<div class="coverage-card">
    <div class="coverage-head">
      <h3>${esc(row.label)}</h3>
      <span class="coverage-status ${esc(row.status)}">${esc(row.status_label)}</span>
    </div>
    <div class="coverage-detail">
      <div><strong>Protection:</strong> ${esc(modeCopy[row.status] || 'Local evidence only until verified.')}</div>
      <div><strong>Gate:</strong> ${esc(row.automatic_gate)}</div>
      <div><strong>History:</strong> ${esc(row.history)}</div>
      <div><strong>Sessions:</strong> ${esc(row.session_count)}</div>
      <div><strong>Next:</strong> ${esc(row.action)}</div>
      <div>${esc(row.detail)}</div>
    </div>
  </div>`).join('');
}
function renderSetup(rows) {
  if (!rows.length) return '<div class="empty">Setup checklist unavailable.</div>';
  return rows.map((row, index) => `<div class="coverage-card">
    <div class="coverage-head">
      <h3>${index + 1}. ${esc(row.title)}</h3>
      <span class="coverage-status ${esc(row.status)}">${esc(row.status)}</span>
    </div>
    <div class="coverage-detail">
      <div>${esc(row.why)}</div>
      <div><strong>Verify:</strong> run this, then refresh Settings. If no recent hook event appears, that surface is companion/history-only until proven otherwise.</div>
      <code>${esc(row.command)}</code>
      <button class="btn-quiet" data-command="${esc(row.command)}" onclick="copyText(this.dataset.command, 'Command copied')">Copy command</button>
    </div>
  </div>`).join('');
}
function costliestShare(event, session) {
  const total = Number(session.api_value_usd || 0);
  const part = Number(event.api_value_usd || 0);
  if (total <= 0 || part <= 0) return '';
  const pct = Math.round(part / total * 100);
  return pct >= 1 ? ` · ${pct}% of session cost` : '';
}
/* `weightKey` decides what the bar length means. Projects and models are asked
   about in money, so they stay on api_value_usd. Tools cannot be: a plan-based
   tool is priced at zero by design, so sizing its bar by dollars draws Codex and
   Cursor as empty stubs labelled $0.00 -- indistinguishable from a tool that was
   never opened, when the truth is that it was used and simply has no invoice.
   Tokens exist for every tool, so the tool bars are measured in those and show
   the dollar figure alongside rather than as the length. */
function bars(rows, valueKey = "api_value_label", kind = "project", weightKey = "api_value_usd") {
  if (!rows.length) return '<div class="empty">No local usage found for this window.</div>';
  const max = maxValue(rows, weightKey);
  return rows.map(row => {
    const weight = Number(row[weightKey] || 0);
    // Zero gets no bar at all. A 2% stub for something genuinely unused reads as
    // a small amount rather than as none.
    const width = weight > 0 ? Math.max(2, Math.round(weight / max * 100)) : 0;
    const id = encodeURIComponent(row.id || row.name);
    const click = kind === "project" ? `onclick="selectProject(decodeURIComponent(this.dataset.id))" data-id="${id}"` : "";
    // Both numbers, because neither works alone. A token count is not something
    // anyone can feel -- "354.7M" means nothing without an anchor -- so the
    // dollar figure leads wherever there is one. Where there is not, the tokens
    // lead and say why: plan-based, which is not the same as free or unused.
    const planBased = !row.detected_only && Number(row.tokens || 0) > 0 && Number(row.api_value_usd || 0) <= 0;
    let amount;
    if (row.detected_only) {
      amount = row.status_label || 'Detected';
    } else if (planBased) {
      amount = `${esc(row.tokens_label)}<span class="bar-note">plan-based</span>`;
    } else if (weightKey === "tokens" && row.tokens_label && row.api_value_label) {
      amount = `${esc(row.api_value_label)}<span class="bar-note">${esc(row.tokens_label)} tokens</span>`;
    } else {
      amount = esc(row[valueKey]);
    }
    // `amount` carries markup, so it is interpolated raw below -- every value
    // inside it is escaped individually above.
    return `<div class="bar-row ${kind === "project" ? "clickable" : ""}" title="${esc(row.name)}" ${click}>
      <div class="bar-label">${esc(kind === "project" ? projectName({ project_full: row.name }) : (row.short_name || row.name))}${kind === "project" && row.health ? ` ${healthPill(row.health)}` : ''}</div>
      <div class="bar-shell"><div class="bar" style="width:${width}%"></div></div>
      <div class="amount">${amount}</div>
    </div>`;
  }).join('');
}
function miniStats(totals) {
  return `<div class="mini-grid">
    <div class="mini"><span class="label">Sessions</span><strong>${esc(totals.sessions)}</strong></div>
    <div class="mini"><span class="label">API value</span><strong>${esc(totals.api_value)}</strong></div>
    <div class="mini"><span class="label">Tokens</span><strong>${esc(totals.tokens)}</strong></div>
    <div class="mini"><span class="label">Tool calls</span><strong>${esc(totals.tool_calls)}</strong></div>
  </div>`;
}
async function selectProject(project) {
  openDrawer('Project detail');
  setDrawerContent('<div class="loading">Loading project activity...</div>');
  const days = document.getElementById('days').value;
  const res = await fetch(`/api/project?days=${days}&project=${encodeURIComponent(project)}`);
  const data = await res.json();
  document.getElementById('drawerTitle').textContent = data.project_short || 'Project detail';
  setDrawerContent(`<section class="detail-section"><h2>${esc(data.project_short)}</h2>
    ${miniStats(data.totals)}
    <div class="verdict-card ${data.health && data.health.status === 'critical' ? 'high' : ''}">
      <h3>${healthPill(data.health)} ${esc(data.health ? data.health.reason : 'Review recent local sessions before optimizing.')}</h3>
      <p>The right next action is to review the sessions driving this project, then mark outcomes or create a handoff before continuing broad work.</p>
    </div>
    </section><section class="detail-section"><h3>Models used</h3>
    ${bars(data.models, "api_value_label", "model")}
    </section><section class="detail-section"><h3>Tools used</h3>
    ${bars(data.tools, "api_value_label", "tool")}
    </section><section class="detail-section"><h3>Recent sessions</h3>
    <div class="table-wrap"><table><thead><tr><th>Tool</th><th>Model</th><th>Status</th><th>Tokens</th><th></th></tr></thead>
      <tbody>${data.sessions.map(s => `<tr class="clickable" onclick="selectSession('${s.session_id}')">
        <td>${esc(s.tool)}</td><td>${esc(s.model)}</td><td>${sessionStatePill(s.state)} ${outcomeEvidencePill(s)}</td><td>${esc(s.tokens_label)}</td><td><button class="row-action">Review</button></td>
      </tr>`).join('')}</tbody></table></div></section>`);
}
function renderSessionHero(s) {
  const actions = s.actions || [];
  const action = actions.find(item => item.primary) || actions[0] || null;
  const runtime = s.runtime_attachment || {};
  const outcomeLabel = s.outcome ? `Outcome: ${s.outcome}` : 'Outcome not marked';
  const evidence = confidenceLabel(s);
  // The hero used to be a four-fact grid restating the next step, the return
  // target and more, each of which has its own section below it -- three
  // quarters of a screen of letterhead before the drawer said anything. What is
  // left is the identity and the pair of numbers that are read together: how
  // much this session used, and what that came to.
  return `<section class="session-hero">
    <h2 class="session-title">${esc(s.project_short || s.project || 'Session')}</h2>
    <p class="session-meta">${esc(s.tool || 'unknown tool')} · ${esc(s.model || 'unknown model')}</p>
    ${renderIdentityStrip(s, runtime, s.source_path)}
    <div class="session-hero-pressure">
      <span>Tokens</span><strong>${esc(s.tokens_label || '—')}</strong>
      <em>${esc(s.api_value || '—')} API-equivalent</em>
    </div>
    <div class="session-hero-status">${sessionStatePill(s.state)}<span class="pill">${esc(outcomeLabel)}</span><span class="confidence-chip ${esc(evidence.tone)}">${esc(evidence.label)}</span></div>
  </section>`;
}
function renderSessionSummary(s, label = 'Loading detailed evidence...') {
  const actions = s.actions || [];
  const action = actions.find(item => item.primary) || actions[0] || null;
  const actionButton = action
    ? action.id === 'handoff' && action.label !== 'Inspect evidence'
      ? `<button class="btn-primary" onclick="openHandoff('${esc(s.session_id)}')">${esc(action.label || 'Build Fresh Start brief')}</button>`
      : action.id === 'review_outcome'
        ? `<button class="btn-primary" disabled>${esc(action.label || 'Review outcome')}</button>`
        : `<button class="btn-primary" disabled>${esc(action.label || 'Review session')}</button>`
    : '';
  return `<div class="session-review-shell">${renderSessionHero(s)}
  <section class="detail-section recommended-action loading-action">
    <div class="section-title">
      <div><h3>${esc(action ? action.label : 'Review session')}</h3><p>${esc(action ? action.reason : 'AIWatcher is loading full local evidence for this session.')}</p></div>
      <span class="session-state recent">loading</span>
    </div>
    <div class="copy-row">${actionButton}</div>
    <div class="ai-loading-panel" aria-live="polite">
      <div class="ai-loading-mark">AI</div>
      <div>
        <strong>${esc(label)}</strong>
        <p>Timeline, outcome, git, and prompt evidence are indexing in the background. You can use the primary action while details finish loading.</p>
        <div class="ai-loading-bar" aria-hidden="true"></div>
      </div>
    </div>
  </section></div>`;
}
function renderSessionActions(s) {
  const actions = s.actions || [];
  const handoffAction = actions.find(action => action.id === 'handoff') || null;
  const hasHandoff = !!handoffAction;
  const needsOutcome = actions.some(action => action.id === 'review_outcome');
  const primaryId = (actions.find(action => action.primary) || {}).id || (needsOutcome ? 'review_outcome' : hasHandoff ? 'handoff' : 'optimize_next_prompt');
  const runtime = s.runtime_attachment || actions.find(action => action.id === 'open_tool') || {};
  const openAction = actions.find(action => action.id === 'open_tool') || {};
  const title = hasHandoff
    ? 'Recommended: Continue in a fresh session'
    : needsOutcome
      ? 'Recommended: mark the outcome'
      : 'Recommended: tighten the next prompt';
  const body = hasHandoff
    ? 'This session has enough context, model calls, or tool calls that a Fresh Start brief is safer than replaying the whole history.'
    : needsOutcome
      ? 'Mark whether this worked so AIWatcher can measure value per useful change instead of only tokens.'
      : 'Use this session as evidence, then preflight the next prompt before sending it.';
  const openToolNote = runtime.reason || openAction.reason || 'No safe return target is available for this session yet.';
  // When there is no live return there is no action, so nothing here may carry
  // button shape. It used to render a disabled button of exactly the same
  // height, border and padding as the three real ones beside it, with the
  // reason hidden in a title attribute where it could not be read.
  const openButton = openAction.available
    ? `<button class="btn-quiet" onclick="returnToRuntime('${esc(s.session_id)}')" title="${esc(openToolNote)}">${esc(openAction.label || runtime.action_label || 'Open workspace')}</button>`
    : `<span class="action-note">${esc(openAction.label || runtime.action_label || 'No live return')} — ${esc(openToolNote)}</span>`;
  const evidenceChips = [
    s.calls ? `${s.calls} model calls` : '',
    s.tool_calls ? `${s.tool_calls} tool calls` : '',
    runtime.exact_return_available ? 'Exact return available' : (runtime.available ? 'App focus only' : 'Log only'),
  ].filter(Boolean).slice(0, 5).map(item => `<span class="pill">${esc(item)}</span>`).join('');
  // A session already judged useful and verified is not making a demand of
  // anyone. It kept a critical-toned "Needs action" eyebrow directly under its
  // own "Outcome: useful" and "Verified outcome" chips, which is the UI arguing
  // with itself.
  // confidenceLabel() renders "Verified outcome" purely from s.outcome being
  // set -- a recorded judgement rather than an inferred one -- so that is the
  // same condition. There is no separate verified flag to read.
  const settled = s.outcome === 'useful';
  return `<section class="detail-section recommended-action action-composer${settled ? ' settled' : ''}">
    <div class="action-composer-head">
      <h3>${settled ? 'Optional next step' : 'Needs action'}</h3>
      <strong>${esc(title.replace(/^Recommended: /, ''))}</strong>
      <p>${esc(body)}</p>
    </div>
    <div class="action-evidence">${evidenceChips}</div>
    <!-- Two questions were being asked at once here -- "what next" and "grade
         what happened" -- across five same-sized controls wrapping onto two
         rows, with nothing ranking them. Two stay visible; the rest fold into
         More. "Mark outcome" is gone: it only scrolled to the outcome triad,
         which is its own step further down with its own question. The server's
         recommendation still decides which control is primary. -->
    <div class="action-buttons">
      ${hasHandoff
        ? `<button class="btn-primary" onclick="${handoffAction.label === 'Inspect evidence' ? "document.getElementById('evidencePanel').scrollIntoView({ behavior: 'smooth', block: 'center' })" : `openHandoff('${esc(s.session_id)}')`}">${esc(handoffAction.label === 'Inspect evidence' ? 'Inspect evidence' : 'Start fresh')}</button>
           <button class="btn-quiet" onclick="continueFromSession('${esc(s.session_id)}')">Continue here</button>`
        : `<button class="${primaryId === 'optimize_next_prompt' ? 'btn-primary' : 'btn-quiet'}" onclick="showView('prompt'); closeDrawer(); document.getElementById('promptInput').focus(); showToast('Paste the next prompt here to optimize it before sending')">Optimize next prompt</button>`}
      <details class="action-more">
        <summary>More</summary>
        <div class="action-more-body">
          ${hasHandoff ? `<button class="btn-quiet" onclick="showView('prompt'); closeDrawer(); document.getElementById('promptInput').focus(); showToast('Paste the next prompt here to optimize it before sending')">Optimize next prompt</button>` : ''}
          ${openButton}
        </div>
      </details>
    </div>
    <p class="tool-link-note">${esc(openToolNote)}</p>
  </section>`;
}
// A session that has just started may not be in the index yet, so a miss is
// retried -- but each attempt is a full round trip, so three is the ceiling.
const SESSION_LOOKUP_ATTEMPTS = 3;
let sessionSelectToken = 0;
async function selectSession(sessionId, attempt = 0, token = null) {
  // Opening a second session while the first was still loading -- or still
  // retrying -- left whichever finished last on screen, including a retry for a
  // session you had already navigated away from. Each selection claims a token
  // and stale continuations stop writing.
  if (token === null) token = ++sessionSelectToken;
  const isCurrent = () => token === sessionSelectToken;
  openDrawer('Session review');
  document.getElementById('drawerTitle').textContent = 'Session review';
  const node = document.getElementById('detailContent');
  setDrawerContent(attempt
    ? `<div class="loading">Still looking for this session (attempt ${attempt + 1} of ${SESSION_LOOKUP_ATTEMPTS})...</div>`
    : `<div class="loading">Loading session identity for ${esc(sessionId)}...</div>`);
  const summaryPromise = fetch(`/api/session-summary?id=${encodeURIComponent(sessionId)}`)
    .then(res => res.json())
    .catch(() => null);
  const detailPromise = fetch(`/api/session?id=${encodeURIComponent(sessionId)}`);
  const fastSummary = await summaryPromise;
  if (!isCurrent()) return;
  if (fastSummary && !fastSummary.error) {
    setDrawerContent(renderSessionSummary(fastSummary));
  } else {
    setDrawerContent(`<div class="loading">Loading session details for ${esc(sessionId)}...</div>`);
  }
  const res = await detailPromise;
  const s = await res.json();
  if (!isCurrent()) return;
  if (s.error) {
    // Retried because a session that started moments ago may not be indexed yet.
    // Each attempt costs a full round trip, so this is deliberately short: past
    // that, "not found" is the answer rather than a delay.
    if (attempt + 1 < SESSION_LOOKUP_ATTEMPTS) {
      window.setTimeout(() => {
        if (isCurrent()) selectSession(sessionId, attempt + 1, token);
      }, 1400);
      return;
    }
    setDrawerContent(`<div class="empty">${esc(s.error)}</div>`);
    return;
  }
  if (s.detail_pending) {
    if (!fastSummary || fastSummary.error) setDrawerContent(renderSessionSummary(s));
    const pending = document.createElement('div');
    pending.className = 'loading';
    pending.textContent = s.detail_message || 'Timeline and evidence are still indexing in the background.';
    node.appendChild(pending);
    if (attempt < 8) window.setTimeout(() => selectSession(sessionId, attempt + 1), 1400);
    return;
  }
  document.getElementById('drawerTitle').textContent = 'Session review';
  const summary = s.timeline_summary || {};
  const costliest = summary.costliest;
  const costliestCallout = costliest
    ? `<div class="costliest-step">
        <div class="costliest-head">Costliest step<span class="costliest-share">${costliestShare(costliest, s)}</span></div>
        <div class="costliest-body">${esc(eventTypeLabel(costliest.event_type))} · ${esc(costliest.model)} · ${esc(costliest.tokens_label)} tokens · ${esc(costliest.api_value)}</div>
      </div>`
    : '';
  const costRows = (summary.cost_by_type || []).filter(r => r.api_value_usd > 0)
    .map(r => ({ ...r, name: eventTypeLabel(r.event_type), short_name: eventTypeLabel(r.event_type) }));
  const costBreakdown = costRows.length
    ? `<section class="detail-section"><details class="aiw-details"><summary>Cost by event type</summary>
        <div class="details-body"><p>Where this session's API-equivalent value actually went.</p>
        ${bars(costRows, "label", "type")}</div>
      </details></section>`
    : '';
  const repeats = summary.repeats || {};
  const wasteNote = repeats.duplicate_events > 0
    ? `<div class="waste-note">Possible rework: ${esc(repeats.duplicate_events)} event(s) repeated identical content${repeats.max_repeat > 2 ? ` (one appeared ${esc(repeats.max_repeat)}x)` : ''}. This often signals a retry loop or the agent re-doing work.</div>`
    : '';
  const turnPrompts = s.turn_prompts || {};
  const meaningfulEvents = (s.events || []).filter(e => Number(e.tokens || 0) > 0 || Number(e.api_value_usd || 0) > 0);
  const hiddenMetadata = Math.max(0, (s.events || []).length - meaningfulEvents.length);
  const shownEvents = meaningfulEvents.slice(0, 80);
  const truncated = meaningfulEvents.length > shownEvents.length
    ? `<p class="timeline-note">Showing first ${esc(shownEvents.length)} meaningful events of ${esc(meaningfulEvents.length)}. ${esc(hiddenMetadata)} zero-value metadata events hidden.</p>`
    : hiddenMetadata
      ? `<p class="timeline-note">${esc(hiddenMetadata)} zero-value metadata events hidden.</p>`
      : '';
  const timeline = meaningfulEvents.length
    ? `<section class="detail-section"><details class="aiw-details"><summary>Advanced timeline (${esc(meaningfulEvents.length)} meaningful events)</summary><div class="details-body">
        <p>${esc(summary.events)} total events · ${esc(summary.tokens)} tokens · ${esc(summary.api_value)} API-equivalent value</p>
        ${costliestCallout}
        ${wasteNote}
        ${truncated}
        <div class="table-wrap"><table><thead><tr><th>Turn</th><th>Event</th><th>Model</th><th>Tokens</th><th>API value</th></tr></thead>
          <tbody>${shownEvents.map(e => `<tr title="${esc(e.turn && turnPrompts[e.turn] ? 'Turn #' + e.turn + ': ' + compactText(turnPrompts[e.turn], 220) : (e.content_hash || ''))}">
            <td class="evt-turn">${e.turn ? '#' + esc(e.turn) : '—'}</td><td>${esc(eventTypeLabel(e.event_type))}</td><td>${esc(e.model)}</td><td>${esc(e.tokens_label)}</td><td>${esc(e.api_value)}</td>
          </tr>`).join('')}</tbody></table></div>
      </div></details></section>`
    : `<section class="detail-section"><details class="aiw-details"><summary>Advanced timeline</summary><div class="details-body"><p>No meaningful token/cost events are available for this tool yet.</p>${hiddenMetadata ? `<p>${esc(hiddenMetadata)} zero-value metadata events hidden.</p>` : ''}</div></details></section>`;
  const outcomeButtons = `<div class="outcome-options">
      <button data-testid="outcome-useful" class="outcome-button useful ${s.outcome === 'useful' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','useful')">${s.outcome === 'useful' ? '✓ ' : ''}Useful</button>
      <button data-testid="outcome-rework" class="outcome-button rework ${s.outcome === 'rework' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','rework')">${s.outcome === 'rework' ? '✓ ' : ''}Needs rework</button>
      <button data-testid="outcome-abandoned" class="outcome-button abandoned ${s.outcome === 'abandoned' ? 'selected' : ''}" onclick="markOutcome('${esc(s.session_id)}','abandoned')">${s.outcome === 'abandoned' ? '✓ ' : ''}Abandoned</button>
    </div>`;
  const outcomeActions = s.outcome
    ? `<details id="outcomePanel" class="aiw-details outcome-control"><summary>Outcome marked: ${esc(s.outcome)} · change if needed</summary><div class="details-body">
        <p>Changing this updates local value metrics and future cost-per-useful-outcome calculations.</p>
        <div class="outcome-help">
          <div><strong>Useful</strong> means this session moved the work forward.</div>
          <div><strong>Needs rework</strong> means the output helped but required correction or another pass.</div>
          <div><strong>Abandoned</strong> means the session did not produce useful progress.</div>
        </div>
        ${outcomeButtons}
      </div></details>`
    : `<div id="outcomePanel" class="outcome-control"><h3>Was this work useful?</h3>
        <p>Mark the result so AIWatcher can measure value instead of tokens alone.</p>
        <div class="outcome-help">
          <div><strong>Useful</strong> means this session moved the work forward.</div>
          <div><strong>Needs rework</strong> means the output helped but required correction or another pass.</div>
          <div><strong>Abandoned</strong> means the session did not produce useful progress.</div>
        </div>
        ${outcomeButtons}
      </div>`;
  // These are threshold trips phrased as guidance ("High API-equivalent value:
  // $61.16"), sitting one section away from the coaching block that names the
  // actual turn and says what to change. Kept, because occasionally one is not a
  // restatement, but collapsed so it stops competing with the real advice.
  const insights = s.insights && s.insights.length
    ? `<section class="detail-section"><details class="aiw-details"><summary>What to check next (${esc(s.insights.length)})</summary>
        <div class="details-body"><ul class="insight-list">${s.insights.map(i => `<li>${esc(i)}</li>`).join('')}</ul></div>
      </details></section>`
    : '';
  const pa = s.prompt_analysis;
  let promptReview = '';
  if (pa) {
    const opener = `<details class="aiw-details"><summary>Session opening prompt</summary><div class="details-body"><p class="prompt-opener">${esc(pa.opening_prompt)}</p></div></details>`;
    const asksRows = (pa.expensive_asks || []).map(a => `<tr>
        <td class="ask-turn">#${esc(a.turn)}</td>
        <td class="ask-prompt" title="${esc(a.prompt)}">${esc(a.prompt.length > 110 ? a.prompt.slice(0, 110) + '…' : a.prompt)}</td>
        <td class="ask-tools">${esc(a.tool_calls)}</td>
        <td class="ask-cost">${esc(a.api_value)}<span class="ask-share">${esc(a.share_pct)}%</span></td>
      </tr>`).join('');
    const expensiveAsks = (pa.expensive_asks && pa.expensive_asks.length)
      ? `<section class="detail-section"><details class="aiw-details"><summary>Expensive asks (${esc((pa.expensive_asks || []).length)} turns)</summary><div class="details-body">
          <p>Which prompts drove the cost, by turn. Cost is cumulative — later turns re-send the whole conversation, so a short prompt late in a long session can still be expensive.</p>
          <div class="table-wrap"><table class="asks-table"><thead><tr><th>Turn</th><th>Prompt</th><th>Tools</th><th>Cost</th></tr></thead>
            <tbody>${asksRows}</tbody></table></div>
        </div></details></section>`
      : '';
    const c = pa.coaching;
    const coaching = c
      ? `<section class="detail-section"><h3>Prompt worth tightening <span class="risk-tag risk-${esc(c.risk)}">${esc(c.risk)} risk</span></h3>
          <p>Turn #${esc(c.turn)} (${esc(c.api_value)}) — the costliest ask with something to tighten.</p>
          <div class="prompt-text">${esc(compactText(c.prompt, 700))}</div>
          ${String(c.prompt || '').length > 700 ? `<details class="aiw-details"><summary>Show full prompt</summary><div class="details-body"><div class="prompt-text">${esc(c.prompt)}</div></div></details>` : ''}
          ${c.findings && c.findings.length ? `<h4>Findings</h4><ul class="insight-list">${c.findings.map(f => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}
          ${c.suggestions && c.suggestions.length ? `<h4>Suggestions</h4><ul class="insight-list">${c.suggestions.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
          <h4>Tighter prompt for next time</h4>
          <div class="prompt-suggested">${esc(compactText(c.suggested_prompt, 900))}</div>
          ${String(c.suggested_prompt || '').length > 900 ? `<details class="aiw-details"><summary>Show full tighter prompt</summary><div class="details-body"><div class="prompt-suggested">${esc(c.suggested_prompt)}</div></div></details>` : ''}
        </section>`
      : `<section class="detail-section"><h3>Prompt worth tightening</h3>
          <p>No single prompt stood out as under-specified — cost accumulated across ${esc(pa.turns)} turns. For work this long, checkpoint or start a fresh session between chunks to keep context (and cost) from compounding.</p>
        </section>`;
    // Coaching first: it names the turn, quotes the prompt and says what to
    // change. The asks table is the evidence for that conclusion, so it follows.
    promptReview = `${coaching}${expensiveAsks}<section class="detail-section"><h3>Prompt context</h3>${opener}</section>`;
  }
  setDrawerContent(`<div class="session-review-shell">${renderSessionHero(s)}
    ${renderSessionActions(s)}
    ${renderVerdict(s)}
    ${outcomeActions}
    ${promptReview}
    <div id="evidencePanel">${renderEvidence(s.outcome_evidence)}</div>
    ${renderEvidenceRail(s, costliest, meaningfulEvents)}
    ${insights}
    ${costBreakdown}
    ${runtimeReturnPanel(s.runtime_attachment, s.source_path)}
    <section class="detail-section"><details class="aiw-details"><summary>Session metadata</summary><div class="details-body">
      <table><tbody>
        <tr><th>Started</th><td>${esc(dateLabel(s.started_at))}</td></tr>
        <tr><th>Updated</th><td>${esc(dateLabel(s.updated_at))}</td></tr>
        <tr><th>Source</th><td>${esc(s.source_path || 'unknown')}</td></tr>
        <tr><th>Privacy</th><td>${esc(s.privacy)}</td></tr>
      </tbody></table>
    </div></details></section>
    ${timeline}</div>`);
}
async function markOutcome(sessionId, outcome) {
  const buttons = document.querySelectorAll('.outcome-button');
  buttons.forEach(button => { button.disabled = true; });
  try {
    const res = await fetch('/api/outcome', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, outcome })
    });
    if (!res.ok) {
      const error = await res.json().catch(() => ({ error: 'Could not save outcome' }));
      showToast(error.error || 'Could not save outcome', 'error');
      return;
    }
    await Promise.all([selectSession(sessionId), load(false)]);
    const labels = { useful: 'Useful', rework: 'Needs rework', abandoned: 'Abandoned' };
    showToast(`Outcome saved: ${labels[outcome]}. Continue only if there is a clear next checkpoint.`);
  } catch (error) {
    showToast('Could not reach the local AIWatcher server.', 'error');
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}
function renderTodayDigest(digest) {
  if (!digest) return '<div class="empty">Not enough local history yet to build a weekly digest.</div>';
  const o = digest.outcomes;
  const tally = [
    o.useful ? `${o.useful} useful` : '',
    o.rework ? `${o.rework} rework` : '',
    o.abandoned ? `${o.abandoned} abandoned` : '',
  ].filter(Boolean).join(', ');
  const survival = digest.survival && digest.survival.available
    ? `<span class="pill">${esc(digest.survival.cost_per_surviving_line_label)} per surviving line &middot; ${esc(digest.survival.survival_pct)}% still standing</span>`
    : '';
  return `<div class="insight"><strong>${esc(digest.recommendation)}</strong></div>
    <div class="pill-row">
      ${tally ? `<span class="pill">${esc(tally)}</span>` : ''}
      ${digest.command_gate.commands_blocked ? `<span class="pill">${esc(digest.command_gate.commands_blocked)} dangerous commands blocked</span>` : ''}
      ${digest.prompt_gate.modified ? `<span class="pill">${esc(digest.prompt_gate.modified)} risky prompts modified</span>` : ''}
      ${survival}
    </div>`;
}
function renderReport(report) {
  const digest = report.digest;
  if (!digest) {
    return `<p>${esc(report.title)}</p>
      <div class="pill-row">${report.summary.map(item => `<span class="pill">${esc(item)}</span>`).join('')}</div>
      ${report.highlights.map(item => `<div class="insight"><strong>${esc(item)}</strong></div>`).join('')}
      <p>${esc(report.next_checks.join(' '))}</p>`;
  }
  const sections = [];
  const o = digest.outcomes;
  const outcomeTotal = o.useful + o.rework + o.abandoned + o.inferred_useful + o.inferred_churned;
  if (outcomeTotal > 0) {
    sections.push(`<div class="detail-section">
      <h2>Outcomes</h2>
      <div class="mini-grid">
        ${o.useful ? `<div class="mini"><span class="label">Useful</span><strong>${esc(o.useful)}</strong></div>` : ''}
        ${o.rework ? `<div class="mini"><span class="label">Rework</span><strong>${esc(o.rework)}</strong></div>` : ''}
        ${o.abandoned ? `<div class="mini"><span class="label">Abandoned</span><strong>${esc(o.abandoned)}</strong></div>` : ''}
        ${o.inferred_useful ? `<div class="mini"><span class="label">Inferred useful</span><strong>${esc(o.inferred_useful)}</strong></div>` : ''}
        ${o.inferred_churned ? `<div class="mini"><span class="label">Inferred churned</span><strong>${esc(o.inferred_churned)}</strong></div>` : ''}
      </div>
    </div>`);
  }
  if (digest.highest_cost_useful_session) {
    const h = digest.highest_cost_useful_session;
    sections.push(`<div class="detail-section">
      <h2>Highest-cost useful session</h2>
      <p>${esc(h.project)} &middot; ${esc(h.tool)} &middot; ${esc(h.model)} &mdash; <span class="mono">${esc(h.api_value_label)}</span></p>
    </div>`);
  }
  if (digest.top_sessions && digest.top_sessions.length) {
    // The share is what turns a ranking into a decision: a top five worth most of
    // the window means reviewing five sessions is the whole job, and a top five
    // worth a tenth of it means the money is spread out and this list is the wrong
    // place to look. Same five rows either way, so the number has to be on screen.
    const share = digest.top_sessions_share_pct;
    const cover = share === null || share === undefined
      ? ''
      : `<p class="muted">These ${digest.top_sessions.length} cover ${share}% of the ${esc(digest.top_sessions_window_total_label)} spent this window.</p>`;
    sections.push(`<div class="detail-section">
      <h2>Costliest sessions</h2>
      ${cover}
      ${digest.top_sessions.map(s => `<div class="digest-row${s.session_id ? ' clickable' : ''}" ${s.session_id ? `onclick="selectSession('${esc(s.session_id)}')"` : ''}>
        <span class="digest-row-label">${esc(s.project)} &middot; ${esc(s.tool)} &middot; ${esc(s.model)}</span>
        <span class="mono">${esc(s.api_value_label)}${s.share_pct === null || s.share_pct === undefined ? '' : ` &middot; ${s.share_pct}%`}</span>
        ${s.outcome ? `<span class="outcome-pill ${esc(s.outcome)}">${esc(s.outcome)}</span>` : '<span class="pill">unreviewed</span>'}
      </div>`).join('')}
    </div>`);
  }
  const candidates = [
    ...digest.loop_candidates.map(c => ({ ...c, kind: 'Loop' })),
    ...digest.velocity_candidates.map(c => ({ ...c, kind: 'Runaway pace' })),
  ];
  if (candidates.length) {
    sections.push(`<div class="detail-section">
      <h2>Loop &amp; runaway signals</h2>
      ${candidates.map(c => `<div class="insight"><strong>${esc(c.kind)} &middot; ${esc(c.project)} (${esc(c.tool)})</strong><p>${esc(c.diagnosis || c.ratio_label)}</p></div>`).join('')}
    </div>`);
  }
  if (digest.command_gate.gates_fired > 0 || digest.prompt_gate.flagged > 0) {
    sections.push(`<div class="detail-section">
      <h2>Guardrails this window</h2>
      <div class="pill-row">
        ${digest.command_gate.gates_fired ? `<span class="pill">${esc(digest.command_gate.commands_blocked)} of ${esc(digest.command_gate.gates_fired)} dangerous commands blocked</span>` : ''}
        ${digest.prompt_gate.flagged ? `<span class="pill">${esc(digest.prompt_gate.modified)} of ${esc(digest.prompt_gate.flagged)} risky prompts modified</span>` : ''}
      </div>
    </div>`);
  }
  if (digest.survival && digest.survival.available) {
    const s = digest.survival;
    sections.push(`<div class="detail-section">
      <h2>Cost per surviving line</h2>
      <div class="mini-grid">
        <div class="mini"><span class="label">Still standing</span><strong>${esc(s.survival_pct)}%</strong></div>
        <div class="mini"><span class="label">$/line written</span><strong>${esc(s.cost_per_line_label)}</strong></div>
        <div class="mini"><span class="label">$/surviving line</span><strong>${esc(s.cost_per_surviving_line_label)}</strong></div>
        <div class="mini"><span class="label">Changes measured</span><strong>${esc(s.changes_measured)}</strong></div>
      </div>
      <p class="receipt-note">${esc(s.lines_intact)} of ${esc(s.lines_touched)} lines still in the code, across
        ${esc(s.cost_coverage_pct)}% of spend in the last ${esc(s.window_days)} days.
        ${s.changes_too_recent ? `${esc(s.changes_too_recent)} newer change(s) worth ${esc(s.too_recent_label)} are not old enough to judge yet.` : ''}
        A floor, not a verdict: reformatting and refactoring move attribution away from the original change.</p>
    </div>`);
  }
  return `<div class="verdict-card"><h3>${esc(digest.recommendation)}</h3></div>
    <div class="pill-row" style="margin-top:14px">${report.summary.map(item => `<span class="pill">${esc(item)}</span>`).join('')}</div>
    ${sections.join('')}
    <p class="receipt-note">API-equivalent value, not invoice spend. Outcomes are inferred from local signals, not guaranteed truth. Based on local logs only, not live provider quota.</p>`;
}
/* How far this repo is from its last commit.
 *
 * Present tense, which is what Home asks for, and deliberately not a verdict.
 * The tempting figure here is "how much of today's spend is unbanked", and it
 * cannot honestly exist: the ledger banks an event against the first commit at
 * or after it, so everything inside the 12-hour lookback is provisionally
 * unbanked and flips the moment you commit. A tile built on that would fire on
 * every developer every afternoon for the ordinary state of having uncommitted
 * work, and a signal that fires for everyone sorts nothing.
 *
 * Distance is answerable now. It states a fact and leaves the judgment to the
 * reader -- except where there is a real baseline to compare against, and that
 * baseline is the developer's own median spend between commits, never a round
 * number someone picked. No status rail either way: being far from a commit is
 * a normal part of a working afternoon.
 */
function renderCheckpoint(card) {
  const host = document.getElementById('checkpoint');
  if (!host) return;
  // No live session to measure from is not a state worth a permanent empty row
  // on the ambient surface; an unmeasurable-but-live one is, because then the
  // reader is looking at a session and the absence needs a reason.
  if (!card || (!card.available && !card.reason)) { host.hidden = true; return; }
  if (!card.available) {
    if (/no live session/i.test(card.reason || '')) { host.hidden = true; return; }
    host.hidden = false;
    host.innerHTML = `<div class="section-title"><div><h2>Since your last commit</h2></div></div>
      <div class="empty">${esc(card.reason)}</div>`;
    return;
  }

  const baseline = card.baseline || {};
  const comparison = baseline.available && baseline.ratio
    ? ` &middot; <b>${esc(baseline.ratio)}×</b> your usual ${esc(baseline.median_label)} between commits`
    : '';
  const caveat = baseline.available ? '' : `<p class="receipt-note">${esc(baseline.reason || '')}</p>`;

  host.hidden = false;
  host.innerHTML = `<div class="section-title">
      <div><h2>Since your last commit</h2><p>${esc(card.last_commit_sha)} &middot; ${esc(card.last_commit_subject)}</p></div>
      <span class="note-chip local">Local only</span>
    </div>
    <p class="ambient-say"><b>${esc(card.elapsed_label)}</b> and <b>${esc(card.spend_label)}</b>
      across ${esc(card.events_since)} call${card.events_since === 1 ? '' : 's'}${comparison}.</p>
    ${caveat}`;
}
/* Prove's claim, and the coverage behind it.
 *
 * The surface was two tables of receipts: a log, which answers "what happened",
 * and not an argument, which is what "was any of this worth it" needs. The
 * argument was already written and rendering nowhere near here -- the server
 * builds `unbanked.headline` and its caption in _unbanked_card, and the caption
 * is careful about the thing that matters: uncommitted work in progress looks
 * exactly like exploration that went nowhere, and this cannot tell them apart.
 *
 * Coverage leads rather than trails. On a surface whose job is to be believed,
 * "80% of spend is measured and the rest is too recent to judge" is not a
 * hedge to bury under the tables -- it is the reason to trust the figures above
 * it. A proving surface that only reports its wins is the one nobody believes.
 */
function renderProveClaim(unbanked, survival) {
  const parts = [];

  if (unbanked && unbanked.available && unbanked.headline) {
    // The sentence carries the figure, so it is not also set beside it as a
    // headline number: the server's headline already opens with the amount,
    // and a figure next to it printed "$64.52 -- $64.52 of the last 7 days
    // has no commit behind it". One labelled statement of a number.
    //
    // No status rail. There is no baseline for how much unbanked spend is too
    // much -- exploration that goes nowhere is how the work gets done -- and
    // colouring it would assert a judgment nothing here can support.
    parts.push(`<div class="verdict-card">
      <h3>${esc(unbanked.headline)}</h3>
      <p>${esc(unbanked.caption || '')}</p>
    </div>`);
  } else {
    // Unmeasurable is its own state, with the reason, never a zero.
    parts.push(`<div class="empty">${esc((unbanked && unbanked.reason)
      || 'Spend cannot be attributed to commits in this window yet.')}</div>`);
  }

  if (survival && survival.available) {
    const tooRecent = survival.changes_too_recent
      ? ` ${esc(survival.changes_too_recent)} newer `
        + (survival.changes_too_recent === 1 ? 'change is' : 'changes are')
        + ` not old enough to judge yet, worth ${esc(survival.too_recent_label)}.`
      : '';
    parts.push(`<p class="receipt-note"><strong>What this is based on.</strong>
      ${esc(survival.cost_coverage_pct)}% of spend in the last ${esc(survival.window_days)} days is
      measured, across ${esc(survival.changes_measured)} changes old enough to judge.${tooRecent}
      Survival is a floor, not a verdict: reformatting and refactoring move attribution away from
      the original change, so the true figure is at least this good and never worse.</p>`);
  } else if (survival && survival.reason) {
    parts.push(`<p class="receipt-note"><strong>What this is based on.</strong>
      ${esc(survival.reason)}</p>`);
  }

  return parts.join('');
}
function renderInsightHeadline(totals) {
  const split = totals.replayed_tokens_label && totals.replayed_share_pct
    ? `<span class="pill">${esc(totals.new_tokens_label)} new &middot; ${esc(totals.replayed_tokens_label)} replayed (${esc(totals.replayed_share_pct)}%)</span>`
    : `<span class="pill">${esc(totals.tokens_label)} tokens</span>`;
  return `<div class="headline">
    <span class="headline-figure">${esc(totals.api_value_label)}</span>
    <span class="headline-sub">${esc(totals.window_label)} &middot; ${esc(totals.sessions)} sessions</span>
    ${split}
  </div>`;
}
/* Replay compounding: cost per turn, split into what bought new work and what
   re-sent history you had already paid for. Stacked rather than two free lines,
   because the height of the stack is the turn's real cost -- the reader needs
   the total and the composition, and a stack gives both.

   Priced at the cache-read rate upstream, which is why the band stays modest in
   dollars even when it is most of the tokens. */
/* Daily spend against a trailing band of the user's own recent days.

   The band's edges are drawn soft, and that is not decoration. Resampling this
   distribution moves an edge by roughly half the band's own width even with a
   month of history -- daily AI spend is erratic enough that no realistic amount
   of data pins a quartile down. A crisp boundary would claim a precision that
   does not exist and invite reading "just above the line" as a finding.

   For the same reason nothing is marked for merely clearing the edge. Only days
   at least spike_multiple past it are flagged, which is the point where the
   verdict survives resampling 95%+ of the time. Nothing is ever marked for
   falling *below* the band: a low day may be a quiet day, a day off, or simply
   a day that is not over yet.

   Bars rather than a line, because a line has to pass through days with no
   activity at some height, and there is no honest height for a day that did not
   happen. A missing bar is the only mark that means "nothing here". */
function drawDailySpend(node, chart) {
  if (!node || !chart) return;
  const values = chart.values || [];
  if (values.length < 3) return;
  const W = 640, H = 190, padL = 52, padR = 12, padT = 12, padB = 26;
  const plot = { left: padL, right: W - padR, top: padT, bottom: H - padB };

  const ceiling = Math.max(...values, ...chart.band_high.filter(v => v != null)) * 1.08 || 1;
  const x = chartScale(0, values.length, plot.left, plot.right);
  const y = chartScale(0, ceiling, plot.bottom, plot.top);
  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`, class: 'runway-svg', role: 'img',
  });

  chartGrid(svg, plot, [0, ceiling / 2, ceiling], v => '$' + Math.round(v), y);

  // The band, as a soft ribbon. Segments are broken wherever the baseline is
  // unavailable, so the earliest days of a window simply have no backdrop
  // rather than borrowing a later one.
  let run = [];
  const flushBand = () => {
    if (run.length > 1) {
      const top = run.map(i => [x(i + 0.5), y(chart.band_high[i])]);
      const bottom = run.map(i => [x(i + 0.5), y(chart.band_low[i])]).reverse();
      svg.appendChild(svgEl('path', {
        d: chartPath(top) + 'L' + bottom.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('L') + 'Z',
        fill: chartToken('--line'), opacity: 0.55, stroke: 'none',
      }));
      chartLine(svg, run.map(i => [x(i + 0.5), y(chart.band_mid[i])]),'--line-strong', { width: 1, dash: '3 3' });
    }
    run = [];
  };
  values.forEach((_, index) => {
    if (chart.band_high[index] == null) flushBand();
    else run.push(index);
  });
  flushBand();

  const barWidth = Math.max(2, (x(1) - x(0)) * 0.62);
  values.forEach((value, index) => {
    if (!chart.active[index]) return;
    const height = Math.max(1, y(0) - y(value));
    svg.appendChild(svgEl('rect', {
      x: x(index + 0.5) - barWidth / 2, y: y(value), width: barWidth, height: height,
      fill: chartToken(chart.spikes[index] ? '--cyan' : '--blue'),
      opacity: index === chart.partial_index ? 0.55 : 1,
      rx: 1,
    }));
    if (chart.spikes[index]) {
      svg.appendChild(svgEl('circle', {
        cx: x(index + 0.5), cy: y(value) - 6, r: 2.2, fill: chartToken('--cyan'),
      }));
    }
  });

  // A full-height column per day, transparent, added last so it takes the
  // pointer. Same reasoning as the scatter's oversized hit circles: a 6px bar
  // you must hit dead-centre is not a target, and a day with no bar at all has
  // nothing to aim at otherwise -- which is exactly the day whose tooltip has
  // something worth saying.
  values.forEach((value, index) => {
    const hit = svgEl('rect', {
      x: x(index), y: plot.top, width: x(1) - x(0), height: plot.bottom - plot.top,
      class: 'spend-hit',
    });
    const lines = [chart.day_labels[index]];
    if (!chart.active[index]) {
      lines.push('No recorded activity');
    } else if (index === chart.partial_index) {
      lines.push(`${chart.labels[index]} so far — today is still in progress`);
    } else {
      lines.push(chart.labels[index]);
    }
    lines.push(chart.band_labels[index]
      ? `Usual for you then: ${chart.band_labels[index]}`
      : 'Not enough history yet to say what was usual');
    if (chart.spikes[index]) {
      lines.push(`At least ${chart.spike_multiple}x past the top of that`);
    }
    const title = document.createElementNS(SVG_NS, 'title');
    title.textContent = lines.join('\n');
    hit.appendChild(title);
    svg.appendChild(hit);
  });

  // Ends only. A tick under every day turns into a smear at thirty.
  const first = (chart.days[0] || '').slice(5);
  const last = (chart.days[chart.days.length - 1] || '').slice(5);
  chartText(svg, x(0.5), plot.bottom + 16, first, { anchor: 'start' });
  chartText(svg, x(values.length - 0.5), plot.bottom + 16, last, { anchor: 'end' });

  const summary = `Daily spend over ${values.length} days against a typical range of `
    + `${chart.band_label}. ${chart.spike_count} day${chart.spike_count === 1 ? '' : 's'} `
    + `at least ${chart.spike_multiple}x past the top of it.`;
  svg.setAttribute('aria-label', summary);
  const title = svgEl('title', {});
  title.textContent = summary;
  svg.appendChild(title);

  node.innerHTML = '';
  node.appendChild(svg);
}
function dailySpendCaption(chart) {
  if (!chart) return '';
  const parts = [];
  if (chart.spike_count) {
    parts.push(`<strong>${chart.spike_count}</strong> day${chart.spike_count === 1 ? '' : 's'} ran at least ${chart.spike_multiple}x past the top of your usual range`);
  }
  if (chart.quiet_days) {
    parts.push(`${chart.quiet_days} day${chart.quiet_days === 1 ? '' : 's'} had no recorded activity, drawn as a gap rather than a zero`);
  }
  if (chart.partial_index != null) parts.push('today is still in progress');
  // One span for the whole sentence: .feed-chart-note is a flex row, so loose
  // text either side of a <strong> would wrap as separate blocks.
  return `<p class="feed-chart-note"><span class="swatch-blue"></span>Daily spend
    <span class="swatch-cyan"></span>Well past your usual
    <span class="swatch-line"></span>Your middle half
    <span class="feed-chart-sentence">${esc(chart.band_label)} a day, around ${esc(chart.median_label)}${parts.length ? ' — ' + parts.join('; ') : ''}. The band describes your habits, not a budget.</span></p>`;
}

const REPLAY_MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
/* Turn times arrive as one start plus a seconds offset each, so they are
   assembled here rather than shipped as nine hundred formatted strings. */
function replayTurnTime(chart, index) {
  if (!chart || !chart.started_at) return '';
  const offset = (chart.second_offsets || [])[index] || 0;
  const at = new Date(new Date(chart.started_at).getTime() + offset * 1000);
  if (isNaN(at.getTime())) return '';
  const pad = value => String(value).padStart(2, '0');
  return `${at.getDate()} ${REPLAY_MONTHS[at.getMonth()]} ${pad(at.getHours())}:${pad(at.getMinutes())}`;
}
/* Three decimals. Turns here sit around forty cents and differ by fractions of
   one, so money()'s two would print a turn's total and its replayed part as the
   same number. */
function turnMoney(value) {
  return '$' + (value || 0).toFixed(3);
}
function drawReplaySplit(node, chart) {
  if (!node || !chart) return;
  const fresh = chart.fresh_usd || [], replayed = chart.replayed_usd || [];
  if (fresh.length < 3) return;

  const W = 640, H = 150, plot = { left: 52, right: 596, top: 12, bottom: 112 };
  // A cache-write turn is billed at a premium and can cost several times an
  // ordinary one. Scaling to the maximum lets two such turns flatten every other
  // turn into an unreadable strip along the axis, hiding the thing the chart
  // exists to show. So the axis is clipped near the top of the ordinary range and
  // the overflow is stated in the caption -- clipped, never silently truncated.
  const written = chart.written_usd || fresh.map(() => 0);
  const totals = fresh.map((v, i) => v + written[i] + replayed[i]);
  const ranked = totals.slice().sort((a, b) => a - b);
  const percentile = ranked[Math.floor(ranked.length * 0.9)] || ranked[ranked.length - 1];
  const ceiling = Math.max(percentile * 1.25, 0.01);
  const clipped = totals.filter(v => v > ceiling).length;
  const x = chartScale(0, fresh.length - 1, plot.left, plot.right);
  const y = chartScale(0, ceiling, plot.bottom, plot.top);
  const clamp = value => Math.max(plot.top, y(value));

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'runway-svg', 'aria-hidden': 'true' });
  chartGrid(svg, plot, [0, ceiling / 2, ceiling], v => '$' + v.toFixed(2), y);

  const freshTop = fresh.map((v, i) => [x(i), clamp(v)]);
  const writeTop = fresh.map((v, i) => [x(i), clamp(v + written[i])]);
  const stackTop = fresh.map((v, i) => [x(i), clamp(v + written[i] + replayed[i])]);
  // Mark every turn that runs off the top, so a clipped peak reads as clipped
  // rather than as a turn that happened to touch the ceiling.
  totals.forEach((value, i) => {
    if (value <= ceiling) return;
    svg.appendChild(svgEl('circle', {
      cx: x(i), cy: plot.top, r: 2.5, fill: chartToken('--muted'),
    }));
  });
  node.dataset.clipped = String(clipped);
  const baseline = fresh.map((v, i) => [x(i), plot.bottom]);
  const area = (top, bottom) =>
    chartPath(top) + 'L' + bottom.slice().reverse().map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('L') + 'Z';

  // Three bands now, bottom to top: work actually done, the conversation being
  // written to cache, and the conversation being read back. Only the first is
  // new -- the other two are the same history paid for twice over, which is the
  // claim this card makes and could not previously show.
  svg.appendChild(svgEl('path', { d: area(stackTop, writeTop), fill: chartToken('--amber'), opacity: 0.22 }));
  svg.appendChild(svgEl('path', { d: area(writeTop, freshTop), fill: chartToken('--cyan'), opacity: 0.22 }));
  svg.appendChild(svgEl('path', { d: area(freshTop, baseline), fill: chartToken('--blue'), opacity: 0.22 }));
  // A 2px gap in the surface colour separates the bands -- never a border.
  chartLine(svg, freshTop, '--surface', { width: 4 });
  chartLine(svg, freshTop, '--blue');
  chartLine(svg, writeTop, '--surface', { width: 4 });
  chartLine(svg, writeTop, '--cyan');
  chartLine(svg, stackTop, '--amber');

  svg.appendChild(svgEl('line', {
    x1: plot.left, y1: plot.bottom, x2: plot.right, y2: plot.bottom,
    stroke: chartToken('--line-strong'), 'stroke-width': 1, 'vector-effect': 'non-scaling-stroke',
  }));
  // Time, not turn numbers. A session this long runs across days, and "turn 400"
  // locates nothing a person remembers; "15 Aug 09:12" does.
  chartText(svg, plot.left, plot.bottom + 18, replayTurnTime(chart, 0), { anchor: 'start' });
  chartText(svg, plot.right, plot.bottom + 18, replayTurnTime(chart, fresh.length - 1), { anchor: 'end' });

  // Hover in two parts, because at nine hundred turns one column per turn is
  // two thirds of a pixel wide -- unhittable, and not worth hitting either,
  // since turn 437 and turn 438 have nothing to tell apart.
  //
  // The trend is sampled at a readable width instead: each column reports the
  // real turn nearest its centre, so the numbers are exact rather than averaged.
  // Averaging was the alternative and it would have flattened the one thing
  // worth stopping on -- a cache write is a single turn costing twenty times its
  // neighbours, and a mean across fifteen turns turns it into a bump.
  const replayAhead = [];
  let ahead = 0;
  for (let i = replayed.length - 1; i >= 0; i--) { replayAhead[i] = ahead; ahead += replayed[i]; }

  const addTip = (node, index, writeTokens) => {
    const lines = [`Turn ${(chart.first_turn_no || 1) + index} · ${replayTurnTime(chart, index)}`];
    const total = fresh[index] + written[index] + replayed[index];
    if (writeTokens) {
      lines.push(`${turnMoney(total)} — writing ${compactTokens(writeTokens)} tokens to cache`);
      lines.push('Storing the conversation so later turns read it back cheaply, not new work being done');
    } else {
      lines.push(`${turnMoney(total)}, of which ${turnMoney(replayed[index])} re-sent history`);
      lines.push(`${compactTokens((chart.resent_tokens || [])[index] || 0)} tokens of conversation re-sent`);
    }
    if (index < fresh.length - 1) {
      lines.push(`${'$' + replayAhead[index].toFixed(2)} more on replay over the ${fresh.length - 1 - index} turns after this`);
    }
    const title = document.createElementNS(SVG_NS, 'title');
    title.textContent = lines.join(String.fromCharCode(10));
    node.appendChild(title);
  };

  const span = plot.right - plot.left;
  const columns = Math.max(2, Math.floor(span / 18));
  for (let c = 0; c < columns; c++) {
    const left = plot.left + (span * c) / columns;
    const width = span / columns;
    const index = Math.min(fresh.length - 1, Math.max(0, Math.round(
      ((left + width / 2 - plot.left) / span) * (fresh.length - 1))));
    const hit = svgEl('rect', {
      x: left, y: plot.top, width: width, height: plot.bottom - plot.top, class: 'spend-hit',
    });
    addTip(hit, index, 0);
    svg.appendChild(hit);
  }

  // Cache writes get their own mark and their own target, added last so they win
  // the pointer. About one turn in seventy, and the only ones whose story the
  // shape of the line does not already tell.
  (chart.write_turns || []).forEach(write => {
    const top = clamp(fresh[write.i] + written[write.i] + replayed[write.i]);
    svg.appendChild(svgEl('circle', {
      cx: x(write.i), cy: Math.max(plot.top + 2, top - 5), r: 2.6,
      fill: chartToken('--cyan'), stroke: chartToken('--surface'), 'stroke-width': 1.5,
    }));
    const hit = svgEl('rect', {
      x: x(write.i) - 6, y: plot.top, width: 12, height: plot.bottom - plot.top, class: 'spend-hit',
    });
    addTip(hit, write.i, write.tokens);
    svg.appendChild(hit);
  });

  node.innerHTML = '';
  node.appendChild(svg);
}
function feedChartCaption(chart) {
  if (!chart) return '';
  return chart.kind === 'daily_spend' ? dailySpendCaption(chart) : replaySplitCaption(chart);
}
function replaySplitCaption(chart) {
  if (!chart) return '';
  // The share of the session's cost that was replay, which is the claim the card
  // above makes -- stated here so the chart and the sentence cannot drift.
  const share = chart.session_total_usd > 0
    ? Math.round(100 * chart.replayed_total_usd / chart.session_total_usd) : 0;
  const written = chart.session_total_usd > 0
    ? Math.round(100 * (chart.written_total_usd || 0) / chart.session_total_usd) : 0;
  // Writes are stated separately rather than folded into the replay figure. Both
  // are the same conversation being paid for again, but one is storing it and
  // one is reading it back, and they behave differently: writes are a few large
  // spikes, reads are every single turn.
  const writeNote = written >= 1
    ? ` A further <strong>${written}%</strong> went on writing it to cache.` : '';
  return `<p class="feed-chart-note"><span class="feed-chart-legend"><span class="swatch-blue"></span>New context
    <span class="swatch-cyan"></span>Written to cache
    <span class="swatch-amber"></span>Read back</span>
    <span class="feed-chart-sentence"><strong>${share}%</strong> of what this session cost across ${chart.turns} turns
    went on re-sent history.${writeNote} ${chart.session_turns > chart.turns
      ? `Showing the last ${chart.turns} of this session's ${chart.session_turns} turns.` : ''}
    <span data-clip-note></span></span></p>`;
}
/* Clipping is decided while drawing, so the note is filled in afterwards rather
   than guessed at caption time. */
function annotateClipping(node) {
  if (!node) return;
  const note = node.parentElement && node.parentElement.querySelector('[data-clip-note]');
  const clipped = Number(node.dataset.clipped || 0);
  if (!note) return;
  note.textContent = clipped
    ? `${clipped} cache-write turn${clipped === 1 ? ' runs' : 's run'} past the top of the axis.`
    : '';
}

/* One recommendation, and the rest on request.
 *
 * The screen is called Improve and its job is "what is the one behaviour I
 * should change next". Seven findings ranked by money is a ranking, not an
 * answer: a list of everything is advice about nothing, and the reader has to
 * do the choosing the ranking was supposed to do for them.
 *
 * _insight_feed already orders by impact_usd, so the first card is the one the
 * money says matters. Nothing is dropped -- the remainder folds, with its count
 * in the summary, because a fold with a bare title hides whether there is
 * anything behind it. The threshold for reading further should be a click, not
 * a scroll past six things you did not ask about.
 */
function renderInsightFeed(insights) {
  if (!insights || !insights.length) {
    return '<div class="empty">No notable local signals yet. Keep using AI tools and check back after a few sessions.</div>';
  }
  const [first, ...rest] = insights;
  const lead = renderInsightRows([first]);
  if (!rest.length) return lead;
  return lead + `<details class="aiw-details">
    <summary><span>${rest.length} more signal${rest.length === 1 ? '' : 's'}, ranked below this one</span></summary>
    <div class="details-body">${renderInsightRows(rest)}</div>
  </details>`;
}
function renderInsightRows(insights) {
  return insights.map(card => `<div class="feed-row ${esc(card.severity || 'info')}${card.session_id ? ' clickable' : ''}"
      ${card.session_id ? `onclick="selectSession('${esc(card.session_id)}')"` : ''}>
      <div class="feed-main${card.chart ? ' has-evidence' : ''}">
        <div class="feed-says">
          <strong>${esc(card.title)}</strong>
          <p>${esc(card.body)}</p>
          ${card.session_id && card.session_label ? `<p class="feed-session">Charted: <button class="link-inline" data-session="${esc(card.session_id)}" onclick="event.stopPropagation(); selectSession(this.dataset.session)">${esc(card.session_label)}</button></p>` : ''}
        </div>
        ${card.chart ? `<div class="feed-shows"><div class="feed-chart" data-feed-chart="${esc(card.id)}"></div>${feedChartCaption(card.chart)}</div>` : ''}
      </div>
      ${card.impact_label ? `<span class="feed-impact mono">${esc(card.impact_label)}</span>` : ''}
    </div>`).join('');
}
let sessionsLoadedForDays = null;
let reportLoadedForDays = null;
let reportLoading = false;
let freshStartReceiptsMarkedViewed = false;
let sessionRowsCache = [];
let changeRowsCache = [];
let sessionSort = { key: 'updated_at', dir: 'desc' };
// The server returns rows already ordered -- by relevance when there is a search
// term, by recency otherwise. Re-sorting on arrival threw the relevance away, so
// a project search still looked unranked. The client only takes over once the
// reader picks a column.
let sessionSortChosen = false;
let changeSort = { key: 'cost_usd', dir: 'desc' };
async function markFreshStartReceiptsViewed() {
  if (freshStartReceiptsMarkedViewed) return;
  freshStartReceiptsMarkedViewed = true;
  try {
    await fetch('/api/handoff-receipts-viewed', { method: 'POST' });
  } catch (error) {
    // Receipt review acknowledgments should never block reading Evidence.
  }
}
async function quietFreshStartReminders() {
  try {
    await fetch('/api/companion-skip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: 'proof_pending' }),
    });
    freshStartReceiptsMarkedViewed = true;
    // A toggle that does not show its own state leaves the reader pressing it
    // again to find out whether the first press worked.
    const button = pressedButton();
    if (button) {
      button.setAttribute('aria-pressed', 'true');
      button.textContent = 'Reminders quieted';
      button.disabled = true;
    }
    showToast('Fresh Start reminders quieted. Receipts stay available here.');
  } catch (error) {
    showToast('Could not quiet Fresh Start reminders yet.', 'error');
  }
}
async function snoozeFreshStartProjects(projects, message) {
  const clean = (projects || []).filter(Boolean);
  if (!clean.length) {
    showToast('No reliable project path found to snooze.', 'error');
    return false;
  }
  try {
    const response = await fetch('/api/companion-skip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: 'control_recommended_group', projects: clean }),
    });
    if (!response.ok) throw new Error('snooze failed');
    showToast(message || `Fresh Start snoozed for ${clean.length} project${clean.length === 1 ? '' : 's'} for 48h.`);
    await load(true, true);
    return true;
  } catch (error) {
    showToast('Could not snooze Fresh Start projects yet.', 'error');
    return false;
  }
}
async function snoozeFreshStartProject(project, button) {
  const ok = await snoozeFreshStartProjects([project], 'Fresh Start snoozed for this project for 48h.');
  if (ok && button) {
    const card = button.closest('.health-card');
    if (card) card.classList.add('muted-card');
  }
}
async function snoozeVisibleFreshStartProjects(button) {
  // Any element carrying a project, not only .health-card. This screen shows
  // one lead card and the rest as compact rows, so matching on the card class
  // would have snoozed a single project while the button said it snoozed all.
  const cards = Array.from(document.querySelectorAll('#sessionContextHealth [data-project-full]'));
  const projects = [...new Set(cards.map(card => card.dataset.projectFull || '').filter(Boolean))];
  const ok = await snoozeFreshStartProjects(projects, `Fresh Start snoozed for ${projects.length} project${projects.length === 1 ? '' : 's'} for 48h.`);
  if (ok && button) button.disabled = true;
}
async function continueFreshStartProject(sessionId, project) {
  await recordHandoffDecision({
    session_id: sessionId,
    project_full: project,
    reason: 'User chose to continue this project from the context review list.',
    body: 'User chose to continue this project from the context review list.',
    expected_saved_context_tokens: null,
  }, 'continue_here');
  showToast('Fresh Start snoozed for this project for 48h.');
  await load(true, true);
}
function showView(view) {
  document.querySelectorAll('.view').forEach(node => {
    node.hidden = node.id !== `view-${view}`;
  });
  // Views swap in place while the window keeps its scroll offset, so arriving
  // from a card partway down one page dropped you the same distance into the
  // next one -- "Open Watch" sits well down Home and landed 774px past the
  // Context health section it was pointing at. Every caller here is a
  // navigation, and a navigation starts at the top of where it went.
  // Instant rather than smooth: this is a page change, not a scroll, and
  // animating a thousand pixels of a page the user never asked to see is worse
  // than simply being there.
  window.scrollTo(0, 0);
  // Every view has its own nav entry, so each highlights itself. Coverage used
  // to borrow this one -- it was a separate page duplicating what Settings
  // already showed, and it is gone.
  const activeView = view;
  document.querySelectorAll('.nav-tab').forEach(node => {
    node.classList.toggle('active', node.dataset.view === activeView);
  });
  const days = document.getElementById('days').value;
  if (view === 'sessions' && sessionsLoadedForDays !== days) loadSessions();
  if (view === 'insights' && reportLoadedForDays !== days) loadReport();
  if (view === 'receipts') markFreshStartReceiptsViewed();
}
function changeWindow() {
  sessionsLoadedForDays = null;
  reportLoadedForDays = null;
  load();
}
let sessionSearchTimer = null;
function debounceSessionSearch() {
  clearTimeout(sessionSearchTimer);
  sessionSearchTimer = setTimeout(loadSessions, 250);
}
function clearSessionFilters() {
  document.getElementById('sessionSearch').value = '';
  document.getElementById('sessionOutcomeFilter').value = '';
  document.getElementById('sessionStateFilter').value = '';
  loadSessions();
}
function compareValues(a, b, key) {
  const av = a && a[key] !== undefined && a[key] !== null ? a[key] : '';
  const bv = b && b[key] !== undefined && b[key] !== null ? b[key] : '';
  if (typeof av === 'number' || typeof bv === 'number') return Number(av || 0) - Number(bv || 0);
  const at = Date.parse(av);
  const bt = Date.parse(bv);
  if (!Number.isNaN(at) || !Number.isNaN(bt)) return (Number.isNaN(at) ? 0 : at) - (Number.isNaN(bt) ? 0 : bt);
  return String(av).localeCompare(String(bv), undefined, { sensitivity: 'base' });
}
function sortedRows(rows, sort) {
  return [...(rows || [])].sort((a, b) => {
    const result = compareValues(a, b, sort.key);
    return sort.dir === 'asc' ? result : -result;
  });
}
function updateSortIndicators(prefix, sort, keys) {
  keys.forEach(key => {
    const node = document.getElementById(`sort-${prefix}-${key}`);
    if (node) node.textContent = sort.key === key ? (sort.dir === 'asc' ? '▲' : '▼') : '';
  });
}
function setSessionSort(key) {
  sessionSortChosen = true;
  sessionSort = { key, dir: sessionSort.key === key && sessionSort.dir === 'desc' ? 'asc' : 'desc' };
  renderSessionRows(sessionRowsCache, Boolean(
    document.getElementById('sessionSearch').value.trim()
    || document.getElementById('sessionOutcomeFilter').value
    || document.getElementById('sessionStateFilter').value
  ));
}
function setChangeSort(key) {
  changeSort = { key, dir: changeSort.key === key && changeSort.dir === 'desc' ? 'asc' : 'desc' };
  document.getElementById('changeRows').innerHTML = renderChangeRows(changeRowsCache);
  updateSortIndicators('change', changeSort, ['committed_at', 'project', 'cost_usd', 'lines_changed', 'usd_per_line', 'survival_pct', 'usd_per_surviving_line']);
}
function renderSessionRows(rows, filtered) {
  updateSortIndicators('session', sessionSortChosen ? sessionSort : { key: null, dir: 'desc' },
    ['tool', 'project', 'model', 'tokens_value']);
  const ordered = sessionSortChosen ? sortedRows(rows, sessionSort) : rows;
  const tokens = tokenColumnFormatter(ordered.map(s => s.tokens_value));
  document.getElementById('sessionRows').innerHTML = rows.length
    ? ordered.map(s => `<tr class="clickable" onclick="selectSession('${esc(s.session_id)}')">
        <td>${esc(s.tool)}</td>
        <td title="${esc(projectTitle(s))}">${esc(projectName(s))}${s.match_field ? `<span class="match-note">matched on ${esc(s.match_field)}</span>` : ''}<br>${sessionStatePill(s.state)} ${s.outcome ? outcomePill(s.outcome) : outcomeEvidencePill(s)}</td>
        <td>${esc(s.model)}</td>
        <td class="mono num">${esc(s.tokens_value === null || s.tokens_value === undefined ? s.tokens : tokens(s.tokens_value))}</td>
        <td><button class="row-action">Review</button></td>
      </tr>`).join('')
    : `<tr><td colspan="5"><div class="empty">${filtered
        ? 'No sessions match those filters. Try clearing the search or choosing a different session state.'
        : 'No local sessions found for this window.'}</div></td></tr>`;
}
let sessionSearchToken = 0;
async function loadSessions() {
  const days = document.getElementById('days').value;
  const search = document.getElementById('sessionSearch').value.trim();
  const outcome = document.getElementById('sessionOutcomeFilter').value;
  const state = document.getElementById('sessionStateFilter').value;
  const params = new URLSearchParams({ days });
  if (search) params.set('search', search);
  if (outcome) params.set('outcome', outcome);
  if (state) params.set('state', state);
  // A search that doesn't field-match every session in the window falls back
  // to an uncached per-session git evidence lookup (filter_sessions()'s rough
  // topic match) -- that can take several seconds, so show a visible pending
  // state, and drop this response if a newer search has since been fired.
  const token = ++sessionSearchToken;
  document.getElementById('sessionResultsNote').textContent = 'Searching local sessions...';
  const res = await fetch(`/api/sessions?${params.toString()}`);
  const data = await res.json();
  if (token !== sessionSearchToken) return;
  const filtered = Boolean(search || outcome || state);
  document.getElementById('sessionResultsNote').textContent = filtered
    ? `${data.total_matched} matching session${data.total_matched === 1 ? '' : 's'} of ${data.total_scanned} in this window.`
    : `${data.total_scanned} session${data.total_scanned === 1 ? '' : 's'} in this window.`;
  sessionRowsCache = data.sessions || [];
  renderSessionRows(sessionRowsCache, filtered);
  sessionsLoadedForDays = days;
}
async function loadReport() {
  const days = document.getElementById('days').value;
  if (reportLoading || reportLoadedForDays === days) return;
  reportLoading = true;
  document.getElementById('report').innerHTML = '<div class="loading">Building local spend and outcome evidence...</div>';
  try {
    const reportRes = await fetch(`/api/report?days=${days}`);
    const report = await reportRes.json();
    if (document.getElementById('days').value !== days) return;
    const todayDigest = document.getElementById('todayDigest');
    if (todayDigest) todayDigest.innerHTML = renderTodayDigest(report.digest);
    document.getElementById('report').innerHTML = renderReport(report);
    reportLoadedForDays = days;
  } catch (error) {
    document.getElementById('report').innerHTML = '<div class="empty">Spend evidence is still building. Try again in a moment.</div>';
  } finally {
    reportLoading = false;
  }
}
// ---------------------------------------------------------------------------
// The ambient surface.
//
// Five slots in a fixed order: hero number, meter, one sentence, one action, a
// fact row. There are two states -- a session is running, or nothing is -- and
// they deliberately use the same five slots. A surface that relaid itself out
// every time you started or stopped coding would catch your eye every time, and
// catching your eye is the one thing an always-open tool must not do.
//
// Every number here is server-computed. The thresholds in particular come from
// chart.pressure_tokens_n / chart.critical_tokens_n rather than being repeated
// as constants, so this surface cannot disagree with the runway chart below it.
// ---------------------------------------------------------------------------
let ambientMarkup = null;

function meterSvg(segments, marks, trackMax) {
  // One track, drawn to a caller-supplied maximum. The maximum has to be dynamic:
  // a session well past the critical threshold (334k against a 200k limit) would
  // otherwise push its own fill and peak marker off the end of the track.
  const W = 1000, H = 34, y = 8, h = 14;
  const at = value => Math.max(0, Math.min(1, value / trackMax)) * W;
  const parts = ['<rect x="0" y="' + y + '" width="' + W + '" height="' + h + '" rx="7" fill="var(--surface)"/>'];
  let cursor = 0;
  segments.forEach(seg => {
    const width = at(seg.value);
    if (width > 2) {
      // 2px surface gap between touching fills, rather than a stroke around them.
      parts.push('<rect x="' + cursor.toFixed(1) + '" y="' + y + '" width="' + (width - 2).toFixed(1)
        + '" height="' + h + '" rx="7" fill="' + seg.colour + '"/>');
    }
    cursor += width;
  });
  marks.forEach(mark => {
    const x = at(mark.value);
    if (mark.dot) {
      parts.push('<circle cx="' + x.toFixed(1) + '" cy="' + (y + h / 2) + '" r="5" fill="' + mark.colour
        + '" stroke="var(--bg)" stroke-width="2"/>');
    } else {
      parts.push('<line x1="' + x.toFixed(1) + '" x2="' + x.toFixed(1) + '" y1="2" y2="' + (H - 2)
        + '" stroke="' + mark.colour + '" stroke-width="2"/>');
    }
  });
  return '<svg class="ambient-meter" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none"'
    + ' role="img" aria-hidden="true">' + parts.join('') + '</svg>';
}

function ambientScaleLabels(items) {
  return '<div class="ambient-scale">'
    + items.map(item => '<span class="' + item.tone + '">' + esc(item.label) + '</span>').join('')
    + '</div>';
}

// `presence` is passed in so the panel can admit it is talking about one
// session out of several. It picked the worst reachable one and said nothing
// about the rest, which reads as "this is what is happening" when it is
// really "this is the worst of what is happening".
function ambientRunning(card, presence) {
  const chart = card.chart || {};
  const liveCount = Number(presence && presence.live) || 0;
  const latest = chart.latest_turn_tokens_n || 0;
  const peak = chart.peak_turn_tokens_n || latest;
  const pressure = chart.pressure_tokens_n || 0;
  const critical = chart.critical_tokens_n || 0;
  const trackMax = Math.max(critical, peak, latest) * 1.08 || 1;
  const severity = card.severity === 'critical' ? 'critical'
    : (latest >= pressure ? 'warning' : 'healthy');
  const tone = { critical: 'var(--red)', warning: 'var(--amber)', healthy: 'var(--green)' }[severity];

  const marks = [];
  if (pressure) marks.push({ value: pressure, colour: 'var(--amber)' });
  if (critical) marks.push({ value: critical, colour: 'var(--red)' });
  if (peak > latest) marks.push({ value: peak, colour: 'var(--red)', dot: true });

  // compactTokens returns "200K"; the server's turn labels are "350.2k". Match the
  // hero rather than the other chart, so this component is internally consistent.
  const scaleLabel = value => compactTokens(value);
  const scale = [];
  if (pressure) scale.push({ label: scaleLabel(pressure) + ' pressure', tone: 'amber' });
  if (critical) scale.push({ label: scaleLabel(critical) + ' act now', tone: 'red' });
  if (peak > latest) scale.push({ label: 'peaked ' + card.peak_turn_tokens, tone: 'muted' });

  // Runway wording follows the data: turns_to_critical is null once a session is
  // already past the threshold, and claiming headroom there would be a lie.
  const runway = chart.turns_to_critical === null || chart.turns_to_critical === undefined
    ? (latest >= critical && critical
        ? 'It is already past the ' + compactTokens(critical) + ' threshold, so there is no headroom left to project.'
        : '')
    : 'About <b>' + chart.turns_to_critical + ' turns</b> of headroom at the current rate.';
  // Scope. These two figures are not the same scope and the old wording put
  // them under one unscoped "it": bloat_label is ONE session's ratio -- ui.py
  // builds it from health.bloat_ratio, the representative session -- while
  // replayed_cost_label is a project total, summed across the whole group in
  // _context_health_card. Sitting directly under a hero that is also one
  // session, "86% of what it has cost ... $791.49 so far" read as a single
  // measurement of a single thing; on live data the percentage covered one
  // session and the dollars covered thirteen.
  //
  // The percentage stays and says whose it is. The project-wide dollar moves
  // out rather than being relabelled: it is already in the facts row below as
  // "on replay", and one sentence cannot honestly carry both scopes without
  // getting long enough to wrap to a third line, which the equal-height
  // contract does not have room for.
  const bloat = card.bloat_measurable && card.bloat_label
    ? " <b>" + esc(card.bloat_label) + "</b> of this session's spend went on re-sending history."
    : '';

  return {
    state: severity,
    hero: esc(card.latest_turn_tokens || ''),
    heroUnit: 'tokens / turn',
    // This row still does not say *which* session the hero belongs to. The
    // original of this commit added "worst active of N here" from
    // card.session_count, which answers that; it is deliberately not taken
    // here, because the row is one line and already carries a count.
    //
    // The two counts are different scopes -- liveCount is sessions running
    // right now, card.session_count is sessions in this project -- so printing
    // both would sit two denominators side by side with nothing saying which
    // is which. That is the defect this very commit exists to fix, so it is
    // not worth introducing a second instance of it to close the first.
    //
    // Which of the two this row should carry is a product decision, tracked
    // with the surface restructure rather than settled by whoever merged last.
    context: esc(card.project || '') + (card.tool ? ' &middot; <b>' + esc(card.tool) + '</b>' : '')
      + (liveCount > 1 ? ' &middot; 1 of ' + liveCount + ' live' : ''),
    meter: meterSvg([{ value: latest, colour: tone }], marks, trackMax) + ambientScaleLabels(scale),
    sentence: runway + bloat,
    actions: (card.can_handoff
      ? '<button class="btn-primary" onclick="startFreshFromBubble(\'' + esc(card.session_id) + '\')">Copy Fresh Start brief</button>'
      : '')
      + '<button class="btn-quiet" onclick="selectSession(\'' + esc(card.session_id) + '\')">Inspect session</button>',
    facts: [
      peak > latest && card.peak_turn_tokens ? ['peak', card.peak_turn_tokens] : null,
      // turns_since_reset, not the session total: after a /compact the two
      // diverge and a bare "turns" overstates the context actually in play.
      // Kept short: longer labels wrapped this row to two lines at 880px and
      // broke the equal-height contract between the two ambient states.
      chart.turns_since_reset ? ['context turns', String(chart.turns_since_reset)] : null,
      // The only project-scoped figure in this row. "sessions here" left the
      // reader to guess whether "here" meant this session or this project.
      card.session_count ? ['project sessions', String(card.session_count)] : null,
      card.efficiency_label ? ['new context', card.efficiency_label] : null,
      card.replayed_cost_label ? ['on replay', card.replayed_cost_label] : null,
    ],
  };
}

function ambientQuiet(data) {
  const totals = data.totals || {};
  const liveCount = Number(data.presence && data.presence.live) || 0;
  // Spend-weighted, because the sentence below says "of what they cost". The
  // token-weighted figure reads ~98% for every window and would make that
  // sentence wrong by about thirty points.
  const replayed = Number(totals.replayed_spend_share_pct);
  const hasSplit = !Number.isNaN(replayed) && replayed > 0;
  const needsReview = Number(totals.needs_review_outcomes) || 0;

  const segments = hasSplit
    ? [{ value: replayed, colour: 'var(--amber)' }, { value: 100 - replayed, colour: 'var(--green)' }]
    : [];
  const scale = hasSplit
    ? [{ label: Math.round(replayed) + '% replayed', tone: 'amber' },
       { label: (100 - Math.round(replayed)) + '% new', tone: 'green' }]
    : [];

  // The Sessions observed tile sits about 150px below this and states the
  // count with a label on it. Saying it here too was the third copy of one
  // number on one screen; the sentence leads with what the tile cannot say.
  const sessions = Number(totals.sessions) || 0;
  let sentence = sessions ? '' : 'No local sessions in this window yet.';
  if (hasSplit) {
    sentence += (sentence ? ' ' : '')
      + '<b>' + Math.round(replayed) + '%</b> of what this window cost went on re-sending history.';
  }
  if (needsReview) {
    sentence += ' <b>' + needsReview + '</b> ' + (needsReview === 1 ? 'is' : 'are')
      + ' still waiting on you to say whether the work was useful.';
  }

  const last = (data.recent_sessions || [])[0] || null;
  return {
    state: 'idle',
    hero: last ? esc(last.tokens) : esc(totals.tokens_label || '-'),
    heroUnit: last ? 'tokens in the last session' : 'tokens this window',
    // "no session running" was asserted from the absence of a *chartable*
    // session, not from the absence of a session. A Codex thread exposes a
    // running total and no per-turn numbers, so it is live and unplottable at
    // once -- and this line called it idle. Say what is true instead.
    context: liveCount
      ? liveCount + (liveCount === 1 ? ' session running' : ' sessions running')
        + ' · no per-turn numbers to show'
      : last
        ? 'no session running · last was ' + esc(last.tool || 'a local tool')
        : 'no session running',
    meter: segments.length ? meterSvg(segments, [], 100) + ambientScaleLabels(scale) : '',
    sentence: sentence,
    actions: (needsReview
      ? '<button class="btn-primary" onclick="showView(\'sessions\')">Review ' + needsReview + ' outcome'
        + (needsReview === 1 ? '' : 's') + '</button>'
      : '')
      + '<button class="btn-quiet" onclick="showView(\'insights\')">Open Improve</button>',
    facts: [
      // No 'sessions' entry: the sentence directly above already says
      // "N sessions in this window", and the Sessions observed tile below says
      // it a third time. One labelled statement of a number per screen.
      // The hero is the last session's tokens; this is every session in the
      // window. Two token totals 200px apart, and only one of them was scoped.
      totals.tokens_label ? ['tokens this window', totals.tokens_label] : null,
      totals.useful_outcomes ? ['useful', String(totals.useful_outcomes)] : null,
      // Frozen across every range because it is a rate, not a window total.
      totals.projected_month_label ? ['projected month', totals.projected_month_label] : null,
    ],
  };
}

function renderAmbient(data) {
  const node = document.getElementById('ambient');
  if (!node) return;
  const live = liveHealthCard(data);
  const model = live ? ambientRunning(live, data.presence || null) : ambientQuiet(data);
  const facts = (model.facts || []).filter(Boolean)
    .map(pair => '<span>' + esc(pair[0]) + ' <b>' + esc(pair[1]) + '</b></span>').join('');

  const markup = '<div class="ambient-top"><span>' + model.context + '</span>'
      + '<span id="ambientFreshness"></span></div>'
    + '<div class="ambient-hero">' + model.hero
      + '<u>' + model.heroUnit + '</u></div>'
    + (model.meter ? '<div class="ambient-meter-wrap">' + model.meter + '</div>' : '')
    + (model.sentence ? '<p class="ambient-say">' + model.sentence + '</p>' : '')
    + '<div class="ambient-acts">' + model.actions + '</div>'
    + (facts ? '<div class="ambient-facts">' + facts + '</div>' : '');

  // Only touch the DOM when something actually changed. Rewriting every ten
  // seconds would blow away focus on the buttons and repaint for no reason.
  if (markup === ambientMarkup) return;
  ambientMarkup = markup;
  node.dataset.state = model.state;
  node.innerHTML = markup;
  node.hidden = false;
}

// ---------------------------------------------------------------------------
// Live state.
//
// The dashboard is meant to sit open in a tab while you code, which means it
// spends most of its life as a favicon and a truncated title behind other tabs.
// So the tab is updated first and the page second.
//
// Context per turn only moves when a turn completes -- every thirty seconds to
// several minutes in real work -- so polling faster than that just re-renders an
// identical number. Browsers also throttle background timers to roughly one wake
// a minute, and the background case is the case that matters here, so 60s is
// chosen rather than inherited. Switching to the tab refreshes immediately:
// without that, the first thing you see after switching is always up to a minute
// old, which is exactly when being wrong is most expensive.
// ---------------------------------------------------------------------------
const REFRESH_VISIBLE_MS = 10000;
const REFRESH_HIDDEN_MS = 60000;
const REFRESH_CATCHUP_MS = 1800;   // first poll after the watcher starts rebuilding
const REFRESH_CATCHUP_FACTOR = 1.5;

const TAB_COLOURS = { critical: '#f2778f', warning: '#f2bf6b', healthy: '#43d9a3', idle: '#78869a' };

let refreshTimer = null;
let freshnessTimer = null;
let loadInFlight = false;
let lastLoadedAt = null;
let catchupDelay = REFRESH_CATCHUP_MS;

function scheduleRefresh(ms) {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(refreshTick, ms);
}

function refreshTick() {
  // A scheduled tick never stacks on a load that is still running -- it waits and
  // tries again. User-initiated loads are not gated by this.
  if (loadInFlight) { scheduleRefresh(REFRESH_CATCHUP_MS); return; }
  load(false, false);
}

function nextRefreshDelay(data, forceRefresh) {
  const idle = document.hidden ? REFRESH_HIDDEN_MS : REFRESH_VISIBLE_MS;
  if (data && data.cache && data.cache.refreshing && !forceRefresh) {
    // Poll quickly at first so a rebuild that finishes in a second or two shows
    // up immediately, then back off to the idle cadence. A flat 1.8s here meant
    // a long rebuild fired a request every 1.8s for as long as it ran.
    const delay = Math.min(catchupDelay, idle);
    catchupDelay = Math.min(catchupDelay * REFRESH_CATCHUP_FACTOR, idle);
    return delay;
  }
  catchupDelay = REFRESH_CATCHUP_MS;
  return idle;
}

function faviconFor(state) {
  const colour = TAB_COLOURS[state] || TAB_COLOURS.idle;
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    + "<rect width='32' height='32' rx='7' fill='#070b11'/>"
    + "<circle cx='16' cy='16' r='7' fill='" + colour + "'/></svg>";
  return 'data:image/svg+xml,' + encodeURIComponent(svg);
}

function liveHealthCard(data) {
  const cards = data.context_health || [];
  return cards.find(card => card.charted_because_live) || null;
}

function waitingSessions(data) {
  return ((data.presence && data.presence.sessions) || [])
    .filter(row => row.state === 'waiting')
    .sort((a, b) => (b.idle_seconds || 0) - (a.idle_seconds || 0));
}

function tabStateFor(data) {
  // A session waiting on you outranks context pressure. Pressure is a cost you
  // are choosing; a blocked session is a stop, and this tab is behind an editor
  // most of the day -- if anything is going to interrupt from there, it is this.
  if (waitingSessions(data).length) return 'critical';
  // Severity comes from the handoff bubble when there is one, because that is the
  // same judgement the page itself leads with -- the tab must never disagree with
  // the surface behind it.
  const live = liveHealthCard(data);
  const severity = (data.handoff_bubble && data.handoff_bubble.severity)
    || (live && live.severity) || null;
  if (severity === 'critical') return 'critical';
  if (severity === 'warning' || severity === 'warn') return 'warning';
  return live ? 'healthy' : 'idle';
}

function renderTabState(data) {
  const state = tabStateFor(data);
  const live = liveHealthCard(data);
  const totals = data.totals || {};
  const blocked = waitingSessions(data);
  let title;
  if (blocked.length) {
    // Front-loaded: a browser tab shows perhaps twenty characters, and the
    // whole point is that this reads without switching to it.
    const wait = String(blocked[0].label || 'waiting').replace('waiting ', '');
    title = blocked.length === 1
      ? `Waiting ${wait} · AIWatcher`
      : `${blocked.length} waiting · AIWatcher`;
  } else if (live && live.latest_turn_tokens) {
    const mark = state === 'critical' ? '⚠ ' : '';
    title = mark + live.latest_turn_tokens + '/turn · AIWatcher';
  } else if (totals.api_value_label) {
    title = 'AIWatcher · ' + totals.api_value_label + ' ' + (totals.window_label || '');
  } else {
    title = 'AIWatcher Local';
  }
  document.title = title.trim();
  const icon = document.getElementById('favicon');
  if (icon) icon.setAttribute('href', faviconFor(state));
}

function freshnessLabel(millis) {
  // Coarse buckets on purpose. A counter that ticks every second is motion in the
  // corner of your eye, which is the thing an always-open tool must not be.
  const seconds = Math.round(millis / 1000);
  if (seconds < 10) return 'updated just now';
  if (seconds < 60) return 'updated ' + (Math.floor(seconds / 10) * 10) + 's ago';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return 'updated ' + minutes + 'm ago';
  return 'updated ' + Math.floor(minutes / 60) + 'h ago';
}

function renderFreshness() {
  const label = lastLoadedAt ? freshnessLabel(Date.now() - lastLoadedAt) : '';
  ['freshness', 'ambientFreshness'].forEach(id => {
    const node = document.getElementById(id);
    if (node) node.textContent = label;
  });
}

function startLiveRefresh() {
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { scheduleRefresh(REFRESH_HIDDEN_MS); return; }
    refreshTick();
  });
  window.clearInterval(freshnessTimer);
  freshnessTimer = window.setInterval(renderFreshness, 5000);
}

// The button label, the in-flight flag and the next scheduled poll are restored
// by load()'s finally, never in here. Everything below this line is allowed to
// throw: one bad field used to leave the button stuck on "Updating...",
// loadInFlight pinned true, and no refresh ever scheduled again -- an ambient
// dashboard silently frozen on stale data with no visible error.
async function loadOnce(resetDetail, forceRefresh) {
  const days = document.getElementById('days').value;
  let data;
  try {
    const summaryRes = await fetch(`/api/summary?days=${days}${forceRefresh ? '&refresh=1' : ''}`);
    data = await summaryRes.json();
  } catch (error) {
    // Keep trying on the normal cadence: a dashboard that gives up after one
    // failed poll looks identical to one showing current data.
    showToast('Could not load local AIWatcher data.', 'error');
    return null;
  }
  renderWatcher(data.watcher || null);
  setDefaultPromptTool(data);
  renderCacheStatus(data.cache || null);
  renderPresence(data.presence || null);
  const totals = data.totals;
  document.getElementById('windowLabel').textContent = totals.window_label;
  document.getElementById('apiValue').textContent = totals.api_value_label;
  document.getElementById('sessions').textContent = totals.sessions;
  document.getElementById('usefulOutcomes').textContent = totals.useful_outcomes;
  // "value", not "cost": this is API-equivalent, so for anyone on a
  // subscription no money moved -- the same distinction the metric-neutral rail
  // on the API-equivalent tile exists to protect. Shortened from "Value per
  // useful change: X" so it holds one line at a 235px column, which is what
  // lets four tiles sit across a 1280px window instead of stacking 2x2 and
  // costing the page 193px of height.
  document.getElementById('costPerUseful').textContent =
    `${totals.cost_per_useful_change} value each${totals.inferred_useful_outcomes ? ` · ${totals.inferred_useful_outcomes} to confirm` : ''}`;
  renderSurvivalTile(data.survival || {});
  renderCheckpoint(data.checkpoint);
  // Prove's claim reads the same two objects the Changes ledger and the
  // survival tile already read: no new payload, just the argument they were
  // always evidence for, stated where it is asked.
  document.getElementById('proveClaim').innerHTML =
    renderProveClaim(data.unbanked, data.survival);
  document.getElementById('preflightDecisions').textContent = totals.preflight_decisions;
  // Same two-step contract as the runway charts: the tiles' numbers are set
  // first, then SVG is appended into nodes collected by attribute. Absent on
  // the fast shell payload, so every tile is reset rather than left showing the
  // previous window's shape while the full refresh is still running.
  const tileTrends = data.tile_trends || null;
  // A fast shell payload carries no tile_trends, and a forced refresh always
  // returns the shell -- so clearing on "no data present" meant pressing
  // "Refresh data" emptied every sparkline and left the tiles collapsed until
  // some later poll happened to bring them back. The row visibly changed shape
  // on a button whose whole promise is that nothing changes but the numbers.
  //
  // The original worry is still handled: showing the previous window's shape
  // would be worse than showing none, so a window change still clears. What no
  // longer clears is a payload that simply has not finished computing.
  const sparkDays = String(document.getElementById('days').value);
  const windowChanged = sparkDays !== tileSparkWindow;
  if (tileTrends || data.summary_complete || windowChanged) {
    tileSparkWindow = sparkDays;
    document.querySelectorAll('[data-tile-spark]').forEach(node => {
      node.innerHTML = '';
      node.hidden = true;
      const series = tileTrends && tileTrends.series
        ? tileTrends.series[node.getAttribute('data-tile-spark')]
        : null;
      if (!series || !drawTileSpark(node, series, tileTrends.days)) {
        drawTileSparkPlaceholder(node);
      }
    });
  }
  receiptCache = data.intervention_receipts || [];
  const handoffDecisions = data.handoff_decisions || [];
  document.getElementById('receiptRows').innerHTML = renderReceiptRows(receiptCache);
  document.getElementById('handoffDecisionRows').innerHTML = renderHandoffDecisionRows(handoffDecisions);
  document.getElementById('sessionContextHealth').innerHTML =
    renderContextHealth(data.context_health || [], data.context_health_status || 'ready', data.presence);
  document.getElementById('optimizeWorkspaceBody').innerHTML = renderOptimizeWorkspace(data.optimize || null);
  // The summary has to carry the count, or a collapsed card hides the fact that
  // there is anything in it at all.
  const optimizeSummary = document.getElementById('optimizeWorkspaceSummary');
  if (optimizeSummary) {
    const pending = ((data.optimize || {}).candidates || []).length;
    optimizeSummary.textContent = pending
      ? `Optimize workspace (${pending} to review)`
      : 'Optimize workspace';
  }
  // SVG is built after the markup lands: the cards are assembled as an HTML
  // string, and appending nodes into elements that do not exist yet silently
  // draws nothing. Nodes are collected by attribute rather than looked up by a
  // selector built from the session id, which is scanner-supplied and would need
  // escaping to be safe inside one. Runs after both context-health renderers so
  // it finds the placeholders wherever they were emitted.
  const meterNodes = {};
  document.querySelectorAll('[data-meter]').forEach(node => {
    meterNodes[node.getAttribute('data-meter')] = node;
  });
  const trendNodes = {};
  document.querySelectorAll('[data-trend]').forEach(node => {
    trendNodes[node.getAttribute('data-trend')] = node;
  });
  (data.context_health || []).forEach(row => {
    drawMeter(meterNodes[row.session_id], row.chart);
    drawTrend(trendNodes[row.session_id], row.chart);
  });
  changeRowsCache = data.changes || [];
  document.getElementById('changeRows').innerHTML = renderChangeRows(changeRowsCache);
  document.getElementById('changeTotals').innerHTML = renderChangeTotals(changeRowsCache, data.changes_meta, data.unbanked);
  updateSortIndicators('change', changeSort, ['committed_at', 'project', 'cost_usd', 'lines_changed', 'usd_per_line', 'survival_pct', 'usd_per_surviving_line']);
  const coverage = data.coverage || [];
  document.getElementById('coverageRowsSettings').innerHTML = renderCoverage(coverage);
  // Counts on both headings. Setup steps is still folded, and a folded section
  // with a bare title hides whether there is anything inside. Coverage is not
  // folded any more -- it sits under the privacy card, whose promise it
  // qualifies -- but the count earns its place there for a different reason:
  // "2 of 4 gated automatically" is the honest headline, and a bare "Surface
  // coverage" would let a reader assume the four are covered.
  const gated = coverage.filter(row => row.status === 'automatic').length;
  const coverageSummary = document.getElementById('coverageSummary');
  if (coverageSummary) {
    coverageSummary.textContent = coverage.length
      ? `Surface coverage (${gated} of ${coverage.length} gated automatically)`
      : 'Surface coverage';
  }
  const setup = data.setup || [];
  document.getElementById('setupRows').innerHTML = renderSetup(setup);
  const recommended = setup.filter(step => step.status === 'recommended').length;
  const setupSummary = document.getElementById('setupSummary');
  if (setupSummary) {
    setupSummary.textContent = recommended
      ? `Setup steps (${recommended} recommended, ${setup.length - recommended} optional)`
      : 'Setup steps';
  }
  paintComposition('tools', data.tools_composition);
  paintToolModels(data.tool_models);
  paintModelScatter(data.model_scatter);
  document.getElementById('models').innerHTML = bars(data.models, "api_value_label", "model");
  document.getElementById('tools').innerHTML = bars(data.tools, "tokens_label", "tool", "tokens");
  document.getElementById('privacy').innerHTML = data.privacy.map(p => `<div class="privacy-item"><span class="privacy-check">&#10003;</span><span>${esc(p)}</span></div>`).join('');
  document.getElementById('projectWindow').textContent = totals.window_label;
  document.getElementById('projectRows').innerHTML = data.projects.length
    ? data.projects.map(p => `<tr class="clickable" data-id="${encodeURIComponent(p.id)}">
        <td title="${esc(p.name || '')}"><button class="row-open" onclick="selectProject(decodeURIComponent(this.closest('tr').dataset.id))">${esc(projectName({ project_full: p.name || p.short_name }))}</button></td>
        <td>${healthPill(p.health)}</td>
        <td class="mono">${esc(p.sessions)}</td>
        <td class="mono">${esc(p.tokens_label)}</td>
        <td class="mono">${esc(p.calls)}</td>
        <td class="mono">${esc(p.api_value_label)}</td>
      </tr>`).join('')
    : '<tr><td colspan="6"><div class="empty">No local project usage found for this window.</div></td></tr>';
  document.getElementById('insightHeadline').innerHTML = renderInsightHeadline(data.totals);
  // Always rendered, including at zero. A line that only appears once it has
  // something to admit is one nobody believes when it does appear.
  const overhead = data.analyst_overhead || null;
  document.getElementById('analystOverhead').textContent = overhead
    ? `${overhead.label} — ${overhead.detail}`
    : '';
  document.getElementById('insightFeed').innerHTML = renderInsightFeed(data.insights);
  // Same two-step as the runway charts: markup first, SVG appended after, and
  // nodes collected by attribute rather than by a selector built from data.
  const feedChartNodes = {};
  document.querySelectorAll('[data-feed-chart]').forEach(node => {
    feedChartNodes[node.getAttribute('data-feed-chart')] = node;
  });
  (data.insights || []).forEach(card => {
    if (!card.chart) return;
    // Dispatch on the chart's own kind. The replay chart predates the field and
    // carries none, so an absent kind still means that one.
    if (card.chart.kind === 'daily_spend') {
      drawDailySpend(feedChartNodes[card.id], card.chart);
      return;
    }
    drawReplaySplit(feedChartNodes[card.id], card.chart);
    annotateClipping(feedChartNodes[card.id]);
  });
  if (reportLoadedForDays !== days) {
    const todayDigest = document.getElementById('todayDigest');
    if (todayDigest) todayDigest.innerHTML = '<div class="empty">Open Improve for the evidence-backed weekly digest.</div>';
  }
  if (resetDetail && document.getElementById('detailDrawer').classList.contains('open')) closeDrawer();
  renderAmbient(data);
  renderTabState(data);
  lastLoadedAt = Date.now();
  renderFreshness();
  return data;
}

async function load(resetDetail = true, forceRefresh = false) {
  const refreshButton = document.getElementById('refreshButton');
  if (refreshButton) {
    refreshButton.disabled = true;
    refreshButton.textContent = forceRefresh ? 'Refreshing...' : 'Updating...';
  }
  loadInFlight = true;
  let data = null;
  try {
    data = await loadOnce(resetDetail, forceRefresh);
  } catch (error) {
    // A render fault must not read as fresh data. Say the screen is stale, and
    // put the reason somewhere a developer can find it.
    console.error('AIWatcher: could not render the dashboard', error);
    showToast('Refresh failed. Showing the data already on screen.', 'error');
  } finally {
    if (refreshButton) {
      refreshButton.disabled = false;
      // Restored to a literal, never to whatever the label happened to say. The
      // 10s poll also drives this button: capturing the current text meant a
      // tick landing mid-refresh saved "Updating..." and restored that forever.
      refreshButton.textContent = 'Refresh data';
    }
    loadInFlight = false;
    scheduleRefresh(nextRefreshDelay(data, forceRefresh));
  }
}
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeDrawer(); });
(async () => {
  startLiveRefresh();
  await load();
  const requestedView = new URLSearchParams(location.search).get('view');
  // Every view id, or a ?view= deep link at one of them silently does nothing.
  // test_deep_link_allowlist_covers_every_view pins this against the markup so
  // adding a section cannot quietly leave it unreachable by link.
  if (requestedView && ['today','prompt','watch','sessions','control','projects','changes','receipts','insights','setup'].includes(requestedView)) {
    showView(requestedView);
    if (requestedView === 'prompt') {
      document.getElementById('promptInput').focus();
    }
  }
  // Deep links carry a #target as well as ?view=: the Companion's nudges point
  // at the card that explains the nudge, not merely the page holding it.
  //
  // Native anchor scrolling cannot do this. Every view is hidden at load and
  // only revealed once showView runs, so the browser looks for the target,
  // finds nothing, and has stopped caring by the time it exists. #contextHealth
  // was emitted by seven links and never worked for exactly that reason -- and
  // for a second one, since no element carried that id at all.
  //
  // The owning view is derived from the element rather than kept in a mapping
  // beside it: a second list is a second thing to forget when a card moves, and
  // cards have moved. This also replaces the hand-written #optimizeWorkspace
  // branch, which was the only target that ever worked because it was the only
  // one someone had written a special case for.
  const hashTarget = location.hash ? document.getElementById(location.hash.slice(1)) : null;
  if (hashTarget) {
    const owner = hashTarget.closest('.view');
    if (owner) showView(owner.id.replace(/^view-/, ''));
    // After the view is visible, or there is nothing laid out to scroll to.
    window.setTimeout(() => hashTarget.scrollIntoView({ block: 'start' }), 50);
  }
  if (new URLSearchParams(location.search).get('ask') === '1') {
    openAskPanel();
  }
  // Deep link from `aiwatcher watch --notify` (issue #31): ?session=<id>
  // opens straight to that session's review instead of the overview.
  const deepLinkSession = new URLSearchParams(location.search).get('session');
  if (deepLinkSession) selectSession(deepLinkSession);
})();
