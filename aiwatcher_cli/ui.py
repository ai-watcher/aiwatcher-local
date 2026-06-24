"""Local-only dashboard for AIWatcher Local."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .pricing import is_subscription_model
from .scanner import LocalSession, discover_tools, scan_all


def money(value: float) -> str:
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def compact_int(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def short_path(path: str | None, max_len: int = 54) -> str:
    if not path:
        return "unknown"
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]


def in_window(session: LocalSession, since: datetime) -> bool:
    stamp = session.updated_at or session.started_at
    return bool(stamp and stamp.astimezone() >= since)


def summarize(rows: list[LocalSession]) -> dict[str, float | int]:
    return {
        "sessions": len(rows),
        "tokens": sum(row.tokens_in + row.tokens_out for row in rows),
        "tokens_in": sum(row.tokens_in for row in rows),
        "tokens_out": sum(row.tokens_out for row in rows),
        "api_value_usd": sum(row.cost_usd for row in rows),
        "calls": sum(row.agent_calls for row in rows),
        "tool_calls": sum(row.tool_calls for row in rows),
    }


def token_split(rows: list[LocalSession]) -> dict[str, int]:
    api_priced = sum(
        row.tokens_in + row.tokens_out
        for row in rows
        if row.cost_usd > 0 and not is_subscription_model(row.model)
    )
    total = sum(row.tokens_in + row.tokens_out for row in rows)
    return {"api_priced": api_priced, "plan_limited": max(0, total - api_priced)}


def group_rows(rows: list[LocalSession], key_fn) -> list[dict[str, object]]:
    grouped: dict[str, list[LocalSession]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)

    result = []
    for key, items in grouped.items():
        stats = summarize(items)
        result.append({
            "name": key,
            "id": key,
            "short_name": short_path(key),
            "sessions": stats["sessions"],
            "tokens": stats["tokens"],
            "tokens_label": compact_int(int(stats["tokens"])),
            "api_value_usd": round(float(stats["api_value_usd"]), 6),
            "api_value_label": money(float(stats["api_value_usd"])),
            "calls": stats["calls"],
            "tool_calls": stats["tool_calls"],
        })
    result.sort(key=lambda item: (float(item["api_value_usd"]), int(item["tokens"])), reverse=True)
    return result


def rows_for_window(days: int) -> list[LocalSession]:
    since = datetime.now().astimezone() - timedelta(days=days)
    return [row for row in scan_all() if in_window(row, since)]


def session_json(row: LocalSession) -> dict[str, object]:
    started = row.started_at.isoformat() if row.started_at else None
    updated = row.updated_at.isoformat() if row.updated_at else None
    return {
        "session_id": row.session_id,
        "tool": row.tool,
        "project": row.project_path or "unknown",
        "project_short": short_path(row.project_path),
        "model": row.model or "unknown",
        "tokens": row.tokens_in + row.tokens_out,
        "tokens_label": compact_int(row.tokens_in + row.tokens_out),
        "tokens_in_label": compact_int(row.tokens_in),
        "tokens_out_label": compact_int(row.tokens_out),
        "api_value_usd": round(row.cost_usd, 6),
        "api_value": money(row.cost_usd),
        "calls": row.agent_calls,
        "tool_calls": row.tool_calls,
        "started_at": started,
        "updated_at": updated,
        "source_path": row.source_path,
        "notes": row.notes,
    }


def build_project_detail(project: str, days: int = 7) -> dict[str, object]:
    rows = [row for row in rows_for_window(days) if (row.project_path or "unknown") == project]
    stats = summarize(rows)
    sessions = sorted(rows, key=lambda row: row.updated_at or row.started_at or datetime.min.astimezone(), reverse=True)
    return {
        "project": project,
        "project_short": short_path(project, 72),
        "totals": {
            "sessions": stats["sessions"],
            "api_value": money(float(stats["api_value_usd"])),
            "tokens": compact_int(int(stats["tokens"])),
            "calls": stats["calls"],
            "tool_calls": stats["tool_calls"],
        },
        "models": group_rows(rows, lambda row: row.model or "unknown"),
        "tools": group_rows(rows, lambda row: row.tool),
        "sessions": [session_json(row) for row in sessions[:20]],
    }


def build_session_detail(session_id: str, days: int = 30) -> dict[str, object]:
    rows = [row for row in rows_for_window(days) if row.session_id == session_id]
    if not rows:
        return {"error": "session not found"}
    row = rows[0]
    return {
        **session_json(row),
        "privacy": "Prompt/source content is not shown. Use event export for hashes.",
    }


def build_report(days: int = 7) -> dict[str, object]:
    rows = rows_for_window(days)
    stats = summarize(rows)
    projects = group_rows(rows, lambda row: row.project_path or "unknown")
    tools = group_rows(rows, lambda row: row.tool)
    models = group_rows(rows, lambda row: row.model or "unknown")
    return {
        "title": f"Your AI coding week" if days == 7 else f"Your last {days} days",
        "summary": [
            f"{stats['sessions']} local sessions",
            f"{money(float(stats['api_value_usd']))} API-equivalent value",
            f"{compact_int(int(stats['tokens']))} tokens observed",
            f"{stats['tool_calls']} tool results/calls observed",
        ],
        "highlights": [
            f"Top project: {projects[0]['short_name']} ({projects[0]['api_value_label']})" if projects else "No project activity found.",
            f"Top model: {models[0]['name']} ({models[0]['tokens_label']} tokens)" if models else "No model activity found.",
            f"Top tool: {tools[0]['name']} ({tools[0]['sessions']} sessions)" if tools else "No tool activity found.",
        ],
        "next_checks": [
            "Review the top project for long-running or accidental sessions.",
            "Treat API-equivalent value as usage pressure, not subscription invoice spend.",
            "Export event hashes if you want privacy-safe local evidence.",
        ],
    }


def build_summary(days: int = 7) -> dict[str, object]:
    now = datetime.now().astimezone()
    since = now - timedelta(days=days)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    all_rows = scan_all()
    rows = [row for row in all_rows if in_window(row, since)]
    month_rows = [row for row in all_rows if in_window(row, month_start)]

    stats = summarize(rows)
    month_stats = summarize(month_rows)
    split = token_split(rows)
    day_of_month = max(1, now.day)
    projected_month = float(month_stats["api_value_usd"]) / day_of_month * 30

    projects = group_rows(rows, lambda row: row.project_path or "unknown")
    tools = group_rows(rows, lambda row: row.tool)
    models = group_rows(rows, lambda row: row.model or "unknown")

    recent = sorted(rows, key=lambda row: row.updated_at or row.started_at or datetime.min.astimezone(), reverse=True)[:12]
    detected = discover_tools()
    notes = sorted({note for row in rows for note in row.notes})

    insights = []
    if projects:
        top = projects[0]
        insights.append({
            "title": "Top project",
            "body": f"{top['short_name']} accounts for {top['api_value_label']} API-equivalent value.",
        })
    if split["plan_limited"] > 0:
        insights.append({
            "title": "Subscription/limited usage detected",
            "body": f"{compact_int(split['plan_limited'])} tokens came from plan-based or limited-cost sources. Treat them as observed usage, not invoice spend.",
        })
    if detected.get("cursor") and not any(row.tool == "cursor" for row in rows):
        insights.append({
            "title": "Cursor detected, but usage is limited",
            "body": "Cursor is installed, but local token/cost history is not reliably exposed yet.",
        })

    return {
        "generated_at": now.isoformat(),
        "days": days,
        "privacy": [
            "Read-only local scan",
            "No LLM calls",
            "No source or prompt content in summaries",
            "No cloud upload unless you connect Cloud",
        ],
        "totals": {
            "window_label": "Last 24 hours" if days == 1 else f"Last {days} days",
            "sessions": stats["sessions"],
            "api_value_usd": round(float(stats["api_value_usd"]), 6),
            "api_value_label": money(float(stats["api_value_usd"])),
            "projected_month_label": money(projected_month),
            "tokens_label": compact_int(int(stats["tokens"])),
            "api_priced_tokens_label": compact_int(split["api_priced"]),
            "plan_limited_tokens_label": compact_int(split["plan_limited"]),
            "calls": stats["calls"],
            "tool_calls": stats["tool_calls"],
        },
        "projects": projects[:10],
        "tools": tools,
        "models": models[:10],
        "insights": insights,
        "notes": notes[:5],
        "recent_sessions": [
            {
                "tool": row.tool,
                "session_id": row.session_id,
                "project": short_path(row.project_path),
                "project_full": row.project_path or "unknown",
                "model": row.model or "unknown",
                "tokens": compact_int(row.tokens_in + row.tokens_out),
                "api_value": money(row.cost_usd),
                "updated_at": (row.updated_at or row.started_at).isoformat() if (row.updated_at or row.started_at) else None,
            }
            for row in recent
        ],
    }


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AIWatcher Local</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #090b10;
      --panel: #111722;
      --panel-2: #151d2b;
      --text: #eef3fb;
      --muted: #98a5b8;
      --line: #263145;
      --green: #24d38b;
      --blue: #6ba6ff;
      --amber: #ffc857;
      --red: #ff7a90;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: radial-gradient(circle at top left, #18243a 0, #090b10 36rem);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    main { max-width: 1180px; margin: 0 auto; padding: 28px; }
    header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 22px; }
    h1 { font-size: 32px; margin: 0 0 8px; letter-spacing: 0; }
    p { color: var(--muted); line-height: 1.55; margin: 0; }
    button, select {
      border: 1px solid var(--line);
      background: #0d1320;
      color: var(--text);
      border-radius: 8px;
      padding: 9px 11px;
      font: inherit;
    }
    .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .link-button {
      text-decoration: none;
      border: 1px solid #33537f;
      background: rgba(107,166,255,.12);
      color: #dcebff;
      border-radius: 8px;
      padding: 9px 11px;
      font-weight: 650;
    }
    .grid { display: grid; gap: 14px; }
    .product-nav { display: flex; gap: 8px; flex-wrap: wrap; margin: -6px 0 18px; }
    .nav-pill { border: 1px solid var(--line); background: rgba(255,255,255,.025); border-radius: 999px; padding: 7px 10px; color: #cbd7e9; font-size: 12px; }
    .kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 14px; }
    .two { grid-template-columns: minmax(0, 1.25fr) minmax(320px, .75fr); }
    .card {
      background: linear-gradient(180deg, rgba(255,255,255,.035), rgba(255,255,255,.015)), var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }
    .value { font-size: 28px; font-weight: 750; margin-top: 8px; }
    .sub { color: var(--muted); font-size: 13px; margin-top: 4px; }
    h2 { margin: 0 0 14px; font-size: 16px; }
    .bar-row { display: grid; grid-template-columns: minmax(140px, 1fr) minmax(120px, 1.6fr) 92px; gap: 12px; align-items: center; margin: 12px 0; }
    .bar-row.clickable, tr.clickable { cursor: pointer; }
    .bar-row.clickable:hover .bar-label, tr.clickable:hover td { color: white; }
    .bar-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #dce6f6; }
    .bar-shell { height: 10px; background: #0b111b; border-radius: 99px; overflow: hidden; border: 1px solid #1d2637; }
    .bar { height: 100%; background: linear-gradient(90deg, var(--green), var(--blue)); border-radius: 99px; min-width: 2px; }
    .amount { text-align: right; color: #dce6f6; font-variant-numeric: tabular-nums; }
    .insight { border-left: 3px solid var(--green); padding: 10px 0 10px 12px; margin: 10px 0; }
    .pill-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
    .pill { border: 1px solid var(--line); background: #0d1320; border-radius: 999px; padding: 6px 9px; color: #cdd8ea; font-size: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; }
    th { color: var(--muted); font-weight: 600; }
    td:last-child, th:last-child { text-align: right; }
    .trust { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }
    .trust div { border: 1px solid #244134; background: rgba(36,211,139,.08); border-radius: 8px; padding: 10px; color: #cdf7e5; font-size: 12px; }
    .empty { color: var(--muted); padding: 16px; border: 1px dashed var(--line); border-radius: 8px; }
    .detail { margin-top: 14px; border-top: 1px solid var(--line); padding-top: 14px; }
    .mini-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 10px 0 12px; }
    .mini { border: 1px solid var(--line); border-radius: 8px; padding: 9px; background: rgba(0,0,0,.12); }
    .mini strong { display: block; font-size: 15px; }
    @media (max-width: 860px) {
      main { padding: 18px; }
      header { flex-direction: column; }
      .kpis, .two, .trust { grid-template-columns: 1fr; }
      .bar-row { grid-template-columns: 1fr; gap: 6px; }
      .amount { text-align: left; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>AIWatcher Local</h1>
      <p>Local mode for AIWatcher. Start privately on one laptop, then connect Enterprise when you need team visibility, policy controls, and audit evidence.</p>
    </div>
    <div class="actions">
      <select id="days" onchange="load()">
        <option value="1">Last 24 hours</option>
        <option value="7" selected>Last 7 days</option>
        <option value="30">Last 30 days</option>
      </select>
      <button onclick="load()">Refresh</button>
      <a class="link-button" href="https://www.getaiwatcher.com" target="_blank" rel="noreferrer">Open AIWatcher Cloud</a>
    </div>
  </header>

  <nav class="product-nav" aria-label="AIWatcher Local sections">
    <span class="nav-pill">Local overview</span>
    <span class="nav-pill">Projects</span>
    <span class="nav-pill">Sessions</span>
    <span class="nav-pill">Weekly report</span>
    <span class="nav-pill">Privacy-safe export</span>
  </nav>

  <section class="grid kpis">
    <div class="card"><div class="label">API-equivalent value</div><div class="value" id="apiValue">-</div><div class="sub"><span id="windowLabel">-</span> · not subscription invoice spend</div></div>
    <div class="card"><div class="label">Projected month</div><div class="value" id="projected">-</div><div class="sub">At current pace</div></div>
    <div class="card"><div class="label">Sessions</div><div class="value" id="sessions">-</div><div class="sub">Local machine only</div></div>
    <div class="card"><div class="label">API-priced tokens</div><div class="value" id="apiTokens">-</div><div class="sub" id="limitedTokens">-</div></div>
  </section>

  <section class="grid two">
    <div class="card">
      <h2>Projects Driving AI Usage</h2>
      <div id="projects"></div>
    </div>
    <div class="card">
      <h2>What changed</h2>
      <div id="insights"></div>
      <div class="pill-row" id="privacy"></div>
      <div class="detail" id="detail">
        <p>Select a project or session to inspect local detail.</p>
      </div>
    </div>
  </section>

  <section class="grid two" style="margin-top:14px">
    <div class="card">
      <h2>Models and Tools</h2>
      <div id="models"></div>
    </div>
    <div class="card">
      <h2>Recent Sessions</h2>
      <table>
        <thead><tr><th>Tool</th><th>Project</th><th>Tokens</th><th>API value</th></tr></thead>
        <tbody id="recent"></tbody>
      </table>
    </div>
  </section>

  <section class="grid two" style="margin-top:14px">
    <div class="card">
      <h2>Local Weekly Report</h2>
      <div id="report"></div>
    </div>
    <div class="card">
      <h2>Enterprise handoff</h2>
      <p>AIWatcher Local is for one developer. Enterprise adds team history, policy controls, HITL approvals, evidence packs, and integrations.</p>
      <div class="pill-row">
        <span class="pill">Team visibility</span>
        <span class="pill">Budget guardrails</span>
        <span class="pill">Audit evidence</span>
        <span class="pill">SSO/RBAC</span>
      </div>
    </div>
  </section>
</main>
<script>
function maxValue(rows) {
  return Math.max(0.000001, ...rows.map(r => Number(r.api_value_usd || 0)));
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
function bars(rows, valueKey = "api_value_label", kind = "project") {
  if (!rows.length) return '<div class="empty">No local usage found for this window.</div>';
  const max = maxValue(rows);
  return rows.map(row => {
    const width = Math.max(2, Math.round(Number(row.api_value_usd || 0) / max * 100));
    const id = encodeURIComponent(row.id || row.name);
    const click = kind === "project" ? `onclick="selectProject(decodeURIComponent(this.dataset.id))" data-id="${id}"` : "";
    return `<div class="bar-row ${kind === "project" ? "clickable" : ""}" title="${esc(row.name)}" ${click}>
      <div class="bar-label">${esc(row.short_name || row.name)}</div>
      <div class="bar-shell"><div class="bar" style="width:${width}%"></div></div>
      <div class="amount">${esc(row[valueKey])}</div>
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
  const days = document.getElementById('days').value;
  const res = await fetch(`/api/project?days=${days}&project=${encodeURIComponent(project)}`);
  const data = await res.json();
  document.getElementById('detail').innerHTML = `<h2>${esc(data.project_short)}</h2>
    ${miniStats(data.totals)}
    <p>Top models in this project</p>
    ${bars(data.models, "api_value_label", "model")}
    <table><thead><tr><th>Tool</th><th>Model</th><th>Tokens</th><th>API value</th></tr></thead>
      <tbody>${data.sessions.map(s => `<tr class="clickable" onclick="selectSession('${s.session_id}')">
        <td>${esc(s.tool)}</td><td>${esc(s.model)}</td><td>${esc(s.tokens_label)}</td><td>${esc(s.api_value)}</td>
      </tr>`).join('')}</tbody></table>`;
}
async function selectSession(sessionId) {
  const res = await fetch(`/api/session?id=${encodeURIComponent(sessionId)}`);
  const s = await res.json();
  document.getElementById('detail').innerHTML = `<h2>Session detail</h2>
    <p>${esc(s.project_short)} · ${esc(s.tool)} · ${esc(s.model)}</p>
    ${miniStats({ sessions: 1, api_value: s.api_value, tokens: s.tokens_label, tool_calls: s.tool_calls })}
    <table><tbody>
      <tr><th>Started</th><td>${esc(s.started_at || 'unknown')}</td></tr>
      <tr><th>Updated</th><td>${esc(s.updated_at || 'unknown')}</td></tr>
      <tr><th>Source</th><td>${esc(s.source_path || 'unknown')}</td></tr>
      <tr><th>Privacy</th><td>${esc(s.privacy)}</td></tr>
    </tbody></table>`;
}
function renderReport(report) {
  return `<p>${esc(report.title)}</p>
    <div class="pill-row">${report.summary.map(item => `<span class="pill">${esc(item)}</span>`).join('')}</div>
    ${report.highlights.map(item => `<div class="insight"><strong>${esc(item)}</strong></div>`).join('')}
    <p>${esc(report.next_checks.join(' '))}</p>`;
}
async function load() {
  const days = document.getElementById('days').value;
  const [summaryRes, reportRes] = await Promise.all([
    fetch(`/api/summary?days=${days}`),
    fetch(`/api/report?days=${days}`)
  ]);
  const data = await summaryRes.json();
  const report = await reportRes.json();
  const totals = data.totals;
  document.getElementById('apiValue').textContent = totals.api_value_label;
  document.getElementById('windowLabel').textContent = totals.window_label;
  document.getElementById('projected').textContent = totals.projected_month_label;
  document.getElementById('sessions').textContent = totals.sessions;
  document.getElementById('apiTokens').textContent = totals.api_priced_tokens_label;
  document.getElementById('limitedTokens').textContent = `${totals.plan_limited_tokens_label} plan/limited tokens observed`;
  document.getElementById('projects').innerHTML = bars(data.projects, "api_value_label", "project");
  document.getElementById('models').innerHTML = bars(data.models, "api_value_label", "model");
  document.getElementById('insights').innerHTML = data.insights.length
    ? data.insights.map(i => `<div class="insight"><strong>${esc(i.title)}</strong><p>${esc(i.body)}</p></div>`).join('')
    : '<div class="empty">No notable local signals yet.</div>';
  document.getElementById('privacy').innerHTML = data.privacy.map(p => `<span class="pill">${esc(p)}</span>`).join('');
  document.getElementById('recent').innerHTML = data.recent_sessions.map(s => `<tr class="clickable" onclick="selectSession('${s.session_id}')">
    <td>${esc(s.tool)}</td><td>${esc(s.project)}</td><td>${esc(s.tokens)}</td><td>${esc(s.api_value)}</td>
  </tr>`).join('');
  document.getElementById('report').innerHTML = renderReport(report);
  document.getElementById('detail').innerHTML = '<p>Select a project or session to inspect local detail.</p>';
}
load();
</script>
</body>
</html>
"""


class UIHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/summary":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(90, int(params.get("days", ["7"])[0])))
            except ValueError:
                days = 7
            self._send(200, json.dumps(build_summary(days)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/project":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(90, int(params.get("days", ["7"])[0])))
            except ValueError:
                days = 7
            project = params.get("project", ["unknown"])[0]
            self._send(200, json.dumps(build_project_detail(project, days)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/session":
            params = parse_qs(parsed.query)
            session_id = params.get("id", [""])[0]
            self._send(200, json.dumps(build_session_detail(session_id)), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/report":
            params = parse_qs(parsed.query)
            try:
                days = max(1, min(90, int(params.get("days", ["7"])[0])))
            except ValueError:
                days = 7
            self._send(200, json.dumps(build_report(days)), "application/json; charset=utf-8")
            return
        self._send(404, "Not found", "text/plain; charset=utf-8")


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_available_port(host: str, preferred_port: int, attempts: int = 20) -> int:
    for candidate in range(preferred_port, preferred_port + max(1, attempts)):
        if is_port_available(host, candidate):
            return candidate
    raise OSError(f"No available port found from {preferred_port} to {preferred_port + max(1, attempts) - 1}.")


def _pids_for_port(port: int) -> list[str]:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return []

        pids: set[str] = set()
        suffix = f":{port}"
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            local_address, state, pid = parts[1], parts[3], parts[-1]
            if local_address.endswith(suffix) and state.upper() == "LISTENING" and pid.isdigit():
                pids.add(pid)
        return sorted(pids)

    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []

    return [pid.strip() for pid in result.stdout.splitlines() if pid.strip()]


def restart_local_server(port: int) -> bool:
    pids = _pids_for_port(port)
    if not pids:
        return False

    stopped = False
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", pid, "/F"], check=False, capture_output=True, text=True)
            else:
                subprocess.run(["kill", pid], check=False, capture_output=True, text=True)
            stopped = True
        except OSError:
            continue
    return stopped


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    auto_port: bool = True,
    port_attempts: int = 20,
    restart: bool = False,
) -> None:
    if restart:
        stopped = restart_local_server(port)
        if stopped:
            print(f"Stopped existing process on port {port}.")

    selected_port = port
    if auto_port:
        selected_port = find_available_port(host, port, port_attempts)
        if selected_port != port:
            print(f"Port {port} is busy. Using {selected_port} instead.")

    server = ThreadingHTTPServer((host, selected_port), UIHandler)
    print(f"AIWatcher Local UI running at http://{host}:{selected_port}")
    print("Local-only. No data leaves this machine. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped AIWatcher Local UI.")
    finally:
        server.server_close()
