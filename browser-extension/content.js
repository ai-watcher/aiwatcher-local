/**
 * AIWatcher Local — browser extension content script.
 *
 * Intercepts prompt submission on claude.ai, calls the local preflight API,
 * and either passes through (low risk) or shows a decision modal (medium/high).
 * Fails open silently when the local server is not running.
 */

const STORAGE_KEY   = "aiwatcher_enabled";

// When we re-fire a submit event after a modal decision, we must skip our own
// interception for exactly that one event.
let skipNext    = false;
let intercepting = false;
let activeModal  = null;

// ── Editor detection ────────────────────────────────────────────────────────

function getEditor() {
  const active = document.activeElement;
  if (active && active.offsetParent !== null && (
    active.matches?.('textarea') || active.getAttribute?.('contenteditable') === 'true'
  )) return active;
  const selectors = [
    'div[contenteditable="true"][data-testid]',
    'div[contenteditable="true"].ProseMirror',
    'div[contenteditable="true"]',
    'textarea[data-testid]',
    'textarea',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el && el.offsetParent !== null) return el;
  }
  return null;
}

function getPromptText(editor) {
  if (!editor) return "";
  return (editor.value ?? editor.innerText ?? editor.textContent ?? "").trim();
}

/**
 * Set text in a ProseMirror contenteditable or textarea.
 * execCommand is the only reliable way to update ProseMirror state from outside.
 */
function setEditorText(editor, text) {
  if (!editor) return;
  editor.focus();
  if ("value" in editor) {
    // Plain textarea
    editor.value = text;
    editor.dispatchEvent(new Event("input", { bubbles: true }));
  } else {
    // ProseMirror contenteditable — use execCommand so ProseMirror sees the change
    document.execCommand("selectAll", false, undefined);
    document.execCommand("insertText", false, text);
  }
  // Move cursor to end
  try {
    const sel = window.getSelection();
    if (sel) {
      const range = document.createRange();
      range.selectNodeContents(editor);
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
    }
  } catch { /* ignore selection errors */ }
}

// ── Send-button detection ───────────────────────────────────────────────────

function isSendKey(e) {
  return e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey;
}

function isSendButton(el) {
  if (!el) return false;
  const btn = el.closest?.("button") ?? (el.tagName?.toLowerCase() === "button" ? el : null);
  if (!btn) return false;
  const label  = (btn.getAttribute("aria-label") ?? "").toLowerCase();
  const title  = (btn.getAttribute("title")       ?? "").toLowerCase();
  const testId = (btn.getAttribute("data-testid") ?? "").toLowerCase();
  return label.includes("send") || title.includes("send") || testId.includes("send");
}

// ── Preflight API ───────────────────────────────────────────────────────────

async function callPreflight(prompt) {
  const response = await chrome.runtime.sendMessage({
    type: "preflight",
    prompt,
    tool: "claude-web",
  });
  if (!response?.ok) throw new Error(response?.error ?? "AIWatcher Local is unavailable");
  return response.result;
}

// ── Resubmit helpers ────────────────────────────────────────────────────────

/**
 * Re-fire the original keyboard submit, bypassing our interception exactly once.
 */
function resubmitKey(editor) {
  const sendButton = [...document.querySelectorAll("button")].find(isSendButton);
  if (sendButton && !sendButton.disabled) {
    skipNext = true;
    sendButton.click();
    return;
  }
  const form = editor.closest?.("form");
  if (form?.requestSubmit) {
    form.requestSubmit();
    return;
  }
  skipNext = true;
  editor.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, cancelable: true })
  );
}

/**
 * Re-fire the original button click, bypassing our interception exactly once.
 */
function resubmitClick(btn) {
  skipNext = true;
  btn.click();
}

// ── Modal ────────────────────────────────────────────────────────────────────

function removeModal() {
  if (activeModal) { activeModal.remove(); activeModal = null; }
}

function showCoverageNotice(message) {
  const existing = document.getElementById("aiwatcher-coverage-notice");
  if (existing) existing.remove();
  const host = document.createElement("div");
  host.id = "aiwatcher-coverage-notice";
  const shadow = host.attachShadow({ mode: "closed" });
  const notice = document.createElement("div");
  notice.textContent = message;
  notice.style.cssText = [
    "position:fixed", "right:20px", "bottom:20px", "z-index:2147483647",
    "max-width:360px", "padding:12px 14px", "border-radius:8px",
    "background:#111827", "border:1px solid #f59e0b", "color:#f8fafc",
    "font:12px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
    "box-shadow:0 14px 38px rgba(0,0,0,.4)",
  ].join(";");
  shadow.appendChild(notice);
  document.body.appendChild(host);
  setTimeout(() => host.remove(), 5000);
}

function riskColor(risk) {
  return risk === "high" ? "#ef4444" : risk === "medium" ? "#f59e0b" : "#10b981";
}
function riskBg(risk) {
  return risk === "high" ? "rgba(239,68,68,0.12)" : risk === "medium" ? "rgba(245,158,11,0.12)" : "rgba(16,185,129,0.12)";
}
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function showModal(result, editor, resubmit) {
  removeModal();

  const risk          = String(result.risk ?? "low");
  const score         = Number(result.score ?? 0);
  const findings      = Array.isArray(result.findings)    ? result.findings    : [];
  const suggestedPrompt = String(result.suggested_prompt ?? "");

  const host   = document.createElement("div");
  host.id      = "aiwatcher-modal-host";
  const shadow = host.attachShadow({ mode: "open" });

  shadow.innerHTML = `
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  .overlay {
    position: fixed; inset: 0; z-index: 2147483647;
    background: rgba(0,0,0,0.65);
    display: flex; align-items: center; justify-content: center; padding: 16px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  }
  .modal {
    background: #0f172a; border: 1px solid rgba(255,255,255,0.1);
    border-radius: 12px; width: 100%; max-width: 540px; max-height: 90vh;
    overflow-y: auto; box-shadow: 0 24px 64px rgba(0,0,0,0.6);
  }
  .header {
    display: flex; align-items: center; gap: 10px;
    padding: 16px 20px; border-bottom: 1px solid rgba(255,255,255,0.07);
  }
  .logo {
    width: 28px; height: 28px; border-radius: 6px; background: #4f46e5;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; color: #fff; font-weight: 700; flex-shrink: 0;
  }
  .header-text { flex: 1; }
  .header-title { font-size: 13px; font-weight: 700; color: #e2e8f0; }
  .header-sub   { font-size: 11px; color: #475569; margin-top: 1px; }
  .risk-badge {
    padding: 3px 10px; border-radius: 6px;
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
    color: ${riskColor(risk)}; background: ${riskBg(risk)};
    border: 1px solid ${riskColor(risk)}44;
  }
  .score { font-size: 10px; color: #475569; margin-top: 2px; text-align: right; }
  .body { padding: 16px 20px; }
  .section-label {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: #475569; margin-bottom: 6px;
  }
  .findings { list-style: none; margin-bottom: 14px; }
  .findings li {
    display: flex; gap: 7px; align-items: flex-start;
    font-size: 12px; color: #94a3b8; line-height: 1.5; padding: 3px 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .findings li:last-child { border-bottom: none; }
  .findings li::before { content: "·"; color: ${riskColor(risk)}; font-size: 16px; line-height: 1.1; flex-shrink: 0; }
  .brief-box {
    background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.07);
    border-radius: 8px; padding: 10px 12px; margin-bottom: 16px;
  }
  .brief-text { font-size: 11.5px; color: #cbd5e1; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
  textarea.brief-edit {
    width: 100%; background: rgba(255,255,255,0.05);
    border: 1px solid rgba(99,102,241,0.4); border-radius: 6px;
    padding: 8px 10px; font-size: 11.5px; color: #e2e8f0; line-height: 1.6;
    font-family: inherit; resize: vertical; min-height: 120px; outline: none; display: none;
  }
  textarea.brief-edit:focus { border-color: rgba(99,102,241,0.7); }
  .actions {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
    padding: 14px 20px 16px; border-top: 1px solid rgba(255,255,255,0.07);
  }
  button {
    padding: 9px 14px; border-radius: 8px; font-size: 12px; font-weight: 600;
    cursor: pointer; border: none; transition: opacity .15s; font-family: inherit;
  }
  button:hover { opacity: .85; }
  .btn-primary  { background: #4f46e5; color: #fff; grid-column: 1 / -1; }
  .btn-edit     { background: rgba(255,255,255,0.08); color: #e2e8f0; }
  .btn-original { background: rgba(255,255,255,0.05); color: #94a3b8; }
  .btn-cancel   { background: rgba(239,68,68,0.1); color: #f87171; }
  .privacy { text-align: center; font-size: 10px; color: #334155; padding: 0 20px 12px; }
</style>

<div class="overlay" id="overlay">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="header">
      <div class="logo">A</div>
      <div class="header-text">
        <div class="header-title">AIWatcher Preflight</div>
        <div class="header-sub">Reviewed locally · no prompt left your machine</div>
      </div>
      <div>
        <div class="risk-badge">${esc(risk)} risk</div>
        <div class="score">Risk score ${score}</div>
      </div>
    </div>

    <div class="body">
      ${findings.length ? `
        <div class="section-label">Findings</div>
        <ul class="findings">${findings.map(f => `<li>${esc(f)}</li>`).join("")}</ul>
      ` : ""}
      ${suggestedPrompt ? `
        <div class="section-label">Scoped execution brief</div>
        <div class="brief-box">
          <div class="brief-text" id="briefText">${esc(suggestedPrompt)}</div>
          <textarea class="brief-edit" id="briefEdit">${esc(suggestedPrompt)}</textarea>
        </div>
      ` : ""}
    </div>

    <div class="actions">
      ${suggestedPrompt ? `<button class="btn-primary" id="btnUseBrief">Use scoped brief</button>` : ""}
      ${suggestedPrompt ? `<button class="btn-edit" id="btnEdit">Edit brief</button>` : ""}
      <button class="btn-original" id="btnOriginal">Run original</button>
      <button class="btn-cancel"   id="btnCancel">Cancel</button>
    </div>

    <div class="privacy">Analysis ran on your machine · nothing uploaded</div>
  </div>
</div>`;

  const q = id => shadow.getElementById(id);

  // Use scoped brief — replace editor content then resubmit
  const useBrief = (text) => {
    removeModal();
    setEditorText(editor, text);
    setTimeout(resubmit, 120);
  };

  const btnUseBrief = q("btnUseBrief");
  if (btnUseBrief) btnUseBrief.addEventListener("click", () => useBrief(suggestedPrompt));

  // Edit brief — first click toggles to textarea; second click sends
  const btnEdit  = q("btnEdit");
  const briefTxt = q("briefText");
  const briefEd  = q("briefEdit");
  if (btnEdit) {
    let editMode = false;
    btnEdit.addEventListener("click", () => {
      if (!editMode) {
        editMode = true;
        if (briefTxt) briefTxt.style.display = "none";
        if (briefEd)  { briefEd.style.display = "block"; briefEd.focus(); }
        btnEdit.textContent = "Send edited brief";
      } else {
        useBrief((briefEd?.value ?? suggestedPrompt).trim() || suggestedPrompt);
      }
    });
  }

  // Run original — let the prompt through unchanged
  q("btnOriginal").addEventListener("click", () => {
    removeModal();
    setTimeout(resubmit, 80);
  });

  // Cancel — close modal, return focus to editor
  q("btnCancel").addEventListener("click", () => {
    removeModal();
    editor?.focus();
  });

  // Click outside modal
  q("overlay").addEventListener("click", e => {
    if (e.target === q("overlay")) { removeModal(); editor?.focus(); }
  });

  // Escape key
  const onEsc = e => {
    if (e.key === "Escape") { removeModal(); editor?.focus(); document.removeEventListener("keydown", onEsc, true); }
  };
  document.addEventListener("keydown", onEsc, true);

  document.body.appendChild(host);
  activeModal = host;
}

// ── Core interception ────────────────────────────────────────────────────────

async function intercept(e, editor, resubmit) {
  if (intercepting) return;
  const prompt = getPromptText(editor);
  if (!prompt) return;

  e.preventDefault();
  e.stopImmediatePropagation();
  intercepting = true;

  try {
    const result = await callPreflight(prompt);
    if (String(result.risk ?? "low") === "low") {
      resubmit();
    } else {
      showModal(result, editor, resubmit);
    }
  } catch {
    // Fail open, but make the coverage gap visible to the developer.
    resubmit();
    showCoverageNotice("AIWatcher Local was unavailable. This prompt was sent without preflight.");
  } finally {
    intercepting = false;
  }
}

async function isEnabled() {
  return new Promise(resolve => {
    if (typeof chrome === "undefined" || !chrome.storage) { resolve(true); return; }
    chrome.storage.local.get([STORAGE_KEY], r => resolve(r[STORAGE_KEY] !== false));
  });
}

// ── Event listeners ──────────────────────────────────────────────────────────

// Keyboard: Enter in the editor
document.addEventListener("keydown", async e => {
  if (!isSendKey(e)) return;
  if (activeModal) return;

  // Let our own re-dispatched event through exactly once
  if (skipNext) { skipNext = false; return; }

  const editor = getEditor();
  if (!editor) return;
  if (!editor.contains(e.target) && e.target !== editor) return;
  if (!(await isEnabled())) return;

  await intercept(e, editor, () => resubmitKey(editor));
}, true);

// Mouse: Send button click
document.addEventListener("click", async e => {
  if (activeModal) return;
  if (skipNext) { skipNext = false; return; }

  const btn = e.target?.closest?.("button");
  if (!isSendButton(btn)) return;
  const editor = getEditor();
  if (!editor) return;
  if (!(await isEnabled())) return;

  await intercept(e, editor, () => resubmitClick(btn));
}, true);
