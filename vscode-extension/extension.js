// @ts-check
"use strict";

const vscode = require("vscode");
const https  = require("https");
const http   = require("http");
const crypto = require("crypto");

// ── Preflight API client ────────────────────────────────────────────────────

/**
 * Call the local AIWatcher preflight API.
 * @param {string} prompt
 * @param {string} serverUrl
 * @returns {Promise<object>}
 */
function requestJson(url, options = {}, body = "") {
  return new Promise((resolve, reject) => {
    const lib      = url.protocol === "https:" ? https : http;
    const requestOptions = {
      hostname: url.hostname,
      port:     url.port || (url.protocol === "https:" ? 443 : 80),
      path:     url.pathname,
      method:   options.method || "GET",
      headers:  options.headers || {},
    };
    const req = lib.request(requestOptions, (res) => {
      let data = "";
      res.on("data", (chunk) => { data += chunk; });
      res.on("end",  () => {
        if (!res.statusCode || res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error(`AIWatcher returned HTTP ${res.statusCode ?? "unknown"}`));
          return;
        }
        try { resolve(JSON.parse(data)); }
        catch { reject(new Error("Invalid JSON from preflight server")); }
      });
    });
    req.on("error", reject);
    req.setTimeout(3000, () => { req.destroy(new Error("Preflight request timed out")); });
    if (body) req.write(body);
    req.end();
  });
}

async function discoverServer(configuredUrl) {
  const candidates = configuredUrl && configuredUrl !== "auto"
    ? [configuredUrl]
    : Array.from({ length: 20 }, (_, index) => `http://127.0.0.1:${8765 + index}`);
  for (const candidate of candidates) {
    try {
      const healthUrl = new URL("/api/health", candidate);
      const health = await requestJson(healthUrl);
      if (health.service === "aiwatcher-local") return candidate;
    } catch {
      // Try the next configured fallback port.
    }
  }
  throw new Error("AIWatcher Local is not running");
}

async function callPreflight(prompt, configuredUrl) {
  const serverUrl = await discoverServer(configuredUrl);
  const body = JSON.stringify({ prompt, tool: "vscode", cwd: null });
  return requestJson(new URL("/api/preflight", serverUrl), {
    method: "POST",
    headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
  }, body);
}

// ── Result webview panel ────────────────────────────────────────────────────

/** @type {vscode.WebviewPanel | undefined} */
let panel;

function riskColor(risk) {
  if (risk === "high")   return "#ef4444";
  if (risk === "medium") return "#f59e0b";
  return "#10b981";
}

function riskBg(risk) {
  if (risk === "high")   return "rgba(239,68,68,0.12)";
  if (risk === "medium") return "rgba(245,158,11,0.12)";
  return "rgba(16,185,129,0.12)";
}

function esc(/** @type {string} */ str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function buildPanelHtml(result, prompt) {
  const risk       = String(result.risk ?? "low");
  const score      = Number(result.score ?? 0);
  const findings   = Array.isArray(result.findings)   ? result.findings   : [];
  const suggestions = Array.isArray(result.suggestions) ? result.suggestions : [];
  const brief      = String(result.suggested_prompt ?? "");
  const nonce      = crypto.randomBytes(16).toString("base64");

  return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--vscode-font-family);
      font-size: var(--vscode-font-size);
      color: var(--vscode-foreground);
      background: var(--vscode-editor-background);
      padding: 20px;
      line-height: 1.5;
    }
    h2  { font-size: 15px; font-weight: 700; margin-bottom: 4px; }
    .sub { font-size: 11px; color: var(--vscode-descriptionForeground); margin-bottom: 16px; }

    .risk-badge {
      display: inline-block;
      padding: 3px 10px; border-radius: 6px;
      font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
      color: ${riskColor(risk)};
      background: ${riskBg(risk)};
      margin-bottom: 14px;
    }

    .score { font-size: 11px; color: var(--vscode-descriptionForeground); margin-left: 8px; }

    .label {
      font-size: 10px; font-weight: 700; text-transform: uppercase;
      letter-spacing: .08em; color: var(--vscode-descriptionForeground);
      margin-bottom: 6px; margin-top: 16px;
    }

    ul { list-style: none; }
    li {
      display: flex; gap: 7px; align-items: flex-start;
      font-size: 12px; padding: 4px 0;
      border-bottom: 1px solid var(--vscode-widget-border, rgba(255,255,255,0.06));
    }
    li:last-child { border-bottom: none; }
    li::before { content: "·"; color: ${riskColor(risk)}; font-size: 16px; line-height: 1.1; flex-shrink: 0; }

    .brief {
      background: var(--vscode-editor-inactiveSelectionBackground, rgba(255,255,255,0.04));
      border: 1px solid var(--vscode-widget-border, rgba(255,255,255,0.08));
      border-radius: 6px;
      padding: 12px 14px;
      font-size: 12px;
      white-space: pre-wrap;
      word-break: break-word;
      color: var(--vscode-foreground);
    }

    .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 18px; }
    button {
      padding: 6px 14px; border-radius: 6px; font-size: 12px;
      font-weight: 600; cursor: pointer; border: none;
      font-family: var(--vscode-font-family);
    }
    .btn-primary { background: #4f46e5; color: #fff; }
    .btn-secondary {
      background: var(--vscode-button-secondaryBackground, rgba(255,255,255,0.08));
      color: var(--vscode-button-secondaryForeground, #ccc);
    }
    .btn-primary:hover  { background: #4338ca; }
    .btn-secondary:hover { opacity: .8; }

    .privacy { font-size: 10px; color: var(--vscode-descriptionForeground); margin-top: 20px; opacity: .6; }

    .original-prompt {
      font-size: 11px; color: var(--vscode-descriptionForeground);
      border-left: 2px solid var(--vscode-widget-border, rgba(255,255,255,0.1));
      padding-left: 10px; margin-bottom: 16px;
      white-space: pre-wrap; word-break: break-word;
    }
  </style>
</head>
<body>
  <h2>AIWatcher Preflight</h2>
  <div class="sub">Prompt reviewed locally · nothing uploaded</div>

  <div>
    <span class="risk-badge">${esc(risk)} risk</span>
    <span class="score">Risk score: ${score}</span>
  </div>

  ${prompt ? `
    <div class="label">Original prompt</div>
    <div class="original-prompt">${esc(prompt.slice(0, 300))}${prompt.length > 300 ? "…" : ""}</div>
  ` : ""}

  ${findings.length ? `
    <div class="label">Findings</div>
    <ul>${findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
  ` : ""}

  ${suggestions.length ? `
    <div class="label">Suggestions</div>
    <ul>${suggestions.map((s) => `<li>${esc(s)}</li>`).join("")}</ul>
  ` : ""}

  ${brief ? `
    <div class="label">Scoped execution brief</div>
    <div class="brief" id="briefText">${esc(brief)}</div>
    <div class="actions">
      <button class="btn-primary" id="copyBrief">Copy brief to clipboard</button>
      <button class="btn-secondary" id="copyOriginal">Copy original prompt</button>
    </div>
  ` : `
    <div class="actions">
      <button class="btn-secondary" id="copyOriginal">Copy original prompt</button>
    </div>
  `}

  <div class="privacy">Analysis ran on your machine via loopback HTTP · no data sent anywhere</div>

  <script nonce="${nonce}">
    const vscode  = acquireVsCodeApi();
    const brief   = ${JSON.stringify(brief)};
    const original = ${JSON.stringify(prompt ?? "")};

    document.getElementById("copyBrief")?.addEventListener("click", () => {
      vscode.postMessage({ type: "copy", text: brief });
    });
    document.getElementById("copyOriginal")?.addEventListener("click", () => {
      vscode.postMessage({ type: "copy", text: original });
    });
  </script>
</body>
</html>`;
}

// ── Preflight runner ────────────────────────────────────────────────────────

async function runPreflight(prompt, context) {
  const config    = vscode.workspace.getConfiguration("aiwatcher");
  const serverUrl = config.get("serverUrl", "auto");

  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "AIWatcher: running preflight…", cancellable: false },
    async () => {
      let result;
      try {
        result = await callPreflight(prompt, serverUrl);
      } catch (err) {
        vscode.window.showWarningMessage(
          "AIWatcher: local server not reachable. Run `aiwatcher ui` first."
        );
        return;
      }

      const risk = String(result.risk ?? "low");

      // Show quick notification for low risk
      if (risk === "low") {
        vscode.window.showInformationMessage(
          `AIWatcher: low risk (score ${result.score ?? 0}) - prompt looks fine.`
        );
        return;
      }

      // Show full panel for medium/high
      if (!panel) {
        panel = vscode.window.createWebviewPanel(
          "aiwatcher.preflight",
          "AIWatcher Preflight",
          vscode.ViewColumn.Beside,
          { enableScripts: true, retainContextWhenHidden: true }
        );
        panel.onDidDispose(() => { panel = undefined; }, null, context.subscriptions);
        panel.webview.onDidReceiveMessage(async (msg) => {
          if (msg.type === "copy") {
            await vscode.env.clipboard.writeText(msg.text);
            vscode.window.showInformationMessage("AIWatcher: copied to clipboard.");
          }
        }, null, context.subscriptions);
      } else {
        panel.reveal(vscode.ViewColumn.Beside);
      }

      panel.webview.html = buildPanelHtml(result, prompt);
    }
  );
}

// ── Commands ────────────────────────────────────────────────────────────────

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
  // Preflight selected text
  context.subscriptions.push(
    vscode.commands.registerCommand("aiwatcher.preflightSelection", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showWarningMessage("AIWatcher: open a file and select text to preflight.");
        return;
      }
      const selection = editor.document.getText(editor.selection);
      if (!selection.trim()) {
        vscode.window.showWarningMessage("AIWatcher: no text selected.");
        return;
      }
      await runPreflight(selection.trim(), context);
    })
  );

  // Preflight typed prompt
  context.subscriptions.push(
    vscode.commands.registerCommand("aiwatcher.preflightInput", async () => {
      const prompt = await vscode.window.showInputBox({
        prompt: "Enter the AI prompt to preflight",
        placeHolder: "e.g. Refactor auth and delete old credentials",
        ignoreFocusOut: true,
      });
      if (!prompt?.trim()) return;
      await runPreflight(prompt.trim(), context);
    })
  );

  // Preflight clipboard contents
  context.subscriptions.push(
    vscode.commands.registerCommand("aiwatcher.preflightClipboard", async () => {
      const text = await vscode.env.clipboard.readText();
      if (!text?.trim()) {
        vscode.window.showWarningMessage("AIWatcher: clipboard is empty.");
        return;
      }
      await runPreflight(text.trim(), context);
    })
  );
}

function deactivate() {
  if (panel) { panel.dispose(); panel = undefined; }
}

module.exports = { activate, deactivate };
