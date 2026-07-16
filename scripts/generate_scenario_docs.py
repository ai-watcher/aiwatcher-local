#!/usr/bin/env python3
"""Generate AIWatcher Local scenario docs from docs/scenarios.json.

The JSON file is the source of truth. Generated outputs are intentionally
self-contained so they can be opened from a clone without a build step.
"""

from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "scenarios.json"
HTML_OUT = ROOT / "docs" / "aiwatcher-scenario-tests.html"
STATUS_OUT = ROOT / "docs" / "scenario-status.md"
CHECKLIST_OUT = ROOT / "docs" / "release-checklist.md"

STATUS_LABELS = {
    "done": "Done",
    "test": "To test",
    "partial": "Partial",
    "gap": "Not built",
}

STATUS_ORDER = {"gap": 0, "partial": 1, "test": 2, "done": 3}


def load_data() -> dict[str, Any]:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def status_pill(status: str) -> str:
    label = STATUS_LABELS.get(status, status)
    return f'<span class="pill p-{html.escape(status)}">{html.escape(label)}</span>'


def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    counts = Counter(s.get("status", "unknown") for s in data["scenarios"])
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(data['title'])}</title>
<style>
:root{{--bg:#0b0f14;--panel:#131a23;--panel2:#182231;--panel3:#0f151d;--line:#263244;--ink:#edf3fb;--dim:#a4b0c0;--faint:#687587;--done:#36c47c;--test:#58a6ff;--partial:#b58cff;--gap:#ff6b6b;--amber:#f5b45b;--cyan:#42d9c8;--mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace;--sans:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}}
*{{box-sizing:border-box;margin:0;padding:0}} body{{background:radial-gradient(circle at 20% 0%,rgba(66,217,200,.08),transparent 30%),var(--bg);color:var(--ink);font:15px/1.55 var(--sans)}} .wrap{{max-width:1240px;margin:0 auto;padding:32px 24px 90px}} header{{display:grid;grid-template-columns:1fr auto;gap:24px;align-items:start;margin-bottom:24px}} .brand{{font:12px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--amber)}} h1{{font-size:34px;line-height:1.08;margin:8px 0 10px;letter-spacing:-.02em}} .sub{{max-width:820px;color:var(--dim);font-size:16px}} .meta{{font:12px var(--mono);color:var(--faint);margin-top:12px}} .thesis{{background:linear-gradient(135deg,rgba(66,217,200,.13),rgba(88,166,255,.08));border:1px solid var(--line);border-radius:10px;padding:16px 18px;max-width:400px}} .thesis b{{display:block;color:var(--ink);margin-bottom:6px}} .thesis p{{color:var(--dim);font-size:13px}}
.stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:22px 0}} .stat{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}} .stat .n{{font:800 27px var(--mono)}} .stat .l{{font-size:11px;color:var(--dim);letter-spacing:.08em;text-transform:uppercase}} .stat.done .n{{color:var(--done)}} .stat.test .n{{color:var(--test)}} .stat.partial .n{{color:var(--partial)}} .stat.gap .n{{color:var(--gap)}}
.tabs{{position:sticky;top:0;z-index:5;background:rgba(11,15,20,.92);backdrop-filter:blur(12px);border:1px solid var(--line);border-radius:12px;padding:8px;margin:24px 0;display:flex;gap:8px;overflow:auto}} .tab{{border:1px solid transparent;background:transparent;color:var(--dim);font:12px var(--mono);letter-spacing:.04em;text-transform:uppercase;border-radius:8px;padding:9px 12px;cursor:pointer;white-space:nowrap}} .tab:hover{{color:var(--ink);background:var(--panel2)}} .tab.active{{color:#071016;background:var(--cyan);border-color:var(--cyan);font-weight:800}} .section{{display:none}} .section.active{{display:block}}
h2{{font:16px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--amber);margin:34px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line)}} h3{{font-size:18px;margin:0 0 8px}} .grid{{display:grid;gap:14px}} .grid.two{{grid-template-columns:repeat(2,minmax(0,1fr))}} .card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}} .card p,.card li{{color:var(--dim)}} .card ul{{margin-left:18px}} .kicker{{font:11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin-bottom:8px}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13.5px}} th{{font:11px var(--mono);text-transform:uppercase;letter-spacing:.08em;color:var(--dim);text-align:left;padding:10px 12px;background:var(--panel2);border-bottom:1px solid var(--line)}} td{{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}} tr:last-child td{{border-bottom:none}} .mono{{font-family:var(--mono);font-size:12.5px}} code{{font:12.5px var(--mono);background:var(--panel3);border:1px solid var(--line);border-radius:5px;padding:1px 6px;color:var(--test)}} .pill{{display:inline-flex;align-items:center;gap:4px;font:800 11px var(--mono);padding:3px 9px;border-radius:999px;white-space:nowrap}} .p-done{{background:rgba(54,196,124,.14);color:var(--done);border:1px solid rgba(54,196,124,.42)}} .p-test{{background:rgba(88,166,255,.13);color:var(--test);border:1px solid rgba(88,166,255,.42)}} .p-partial{{background:rgba(181,140,255,.13);color:var(--partial);border:1px solid rgba(181,140,255,.42)}} .p-gap{{background:rgba(255,107,107,.12);color:var(--gap);border:1px solid rgba(255,107,107,.42)}}
.lifecycle{{display:grid;grid-template-columns:repeat(5,minmax(180px,1fr));gap:0;overflow:auto;border:1px solid var(--line);border-radius:12px}} .life{{min-width:180px;background:var(--panel);padding:15px;border-right:1px solid var(--line);cursor:pointer}} .life:hover,.life.active{{background:var(--panel2)}} .life.active{{box-shadow:inset 0 -3px 0 var(--cyan)}} .life:last-child{{border-right:none}} .life .name{{font:800 13px var(--mono);color:var(--cyan);letter-spacing:.08em;text-transform:uppercase}} .life .purpose{{color:var(--dim);font-size:13px;margin:7px 0 10px}} .life .ids{{font:12px var(--mono);color:var(--faint)}}
.filters{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:18px 0}} .chip{{font:12px var(--mono);padding:7px 11px;border-radius:999px;border:1px solid var(--line);background:var(--panel);color:var(--dim);cursor:pointer}} .chip.on{{color:#071016;background:var(--ink);font-weight:800}} input[type=search]{{margin-left:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;color:var(--ink);font:13px var(--mono);padding:8px 12px;min-width:260px}}
.scenario{{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:12px 0;overflow:hidden}} .scenario[data-status=done]{{border-left:3px solid var(--done)}} .scenario[data-status=test]{{border-left:3px solid var(--test)}} .scenario[data-status=partial]{{border-left:3px solid var(--partial)}} .scenario[data-status=gap]{{border-left:3px solid var(--gap)}} .scenario-head{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:14px 16px;cursor:pointer}} .scenario-head:hover{{background:var(--panel2)}} .sid{{font-family:var(--mono);font-weight:900;color:var(--amber)}} .stitle{{font-weight:750;flex:1;min-width:220px}} .platform{{font:11px var(--mono);color:var(--faint)}} .scenario-body{{display:none;border-top:1px solid var(--line);padding:4px 16px 16px}} .scenario.open .scenario-body{{display:block}} .row{{display:grid;grid-template-columns:110px 1fr;gap:12px;padding:9px 0;border-bottom:1px dashed var(--line)}} .row:last-child{{border-bottom:none}} .label{{font:11px var(--mono);text-transform:uppercase;letter-spacing:.07em;color:var(--faint)}} .value{{color:var(--dim)}} .impact .value{{color:var(--done)}} .hidden{{display:none!important}} .empty{{color:var(--faint);font-family:var(--mono);padding:18px 0}} .callout{{border-left:3px solid var(--cyan);background:rgba(66,217,200,.08);border-radius:10px;padding:14px 16px;color:var(--dim)}} .callout b{{color:var(--ink)}}
@media(max-width:900px){{header{{grid-template-columns:1fr}}.stats{{grid-template-columns:repeat(2,1fr)}}.grid.two{{grid-template-columns:1fr}}.row{{grid-template-columns:1fr}}input[type=search]{{margin-left:0;width:100%}}table{{display:block;overflow-x:auto}}}}
</style>
</head>
<body>
<div class="wrap">
<header><div><div class="brand">AIWatcher Local</div><h1>Lifecycle Requirements and Scenario Test Suite</h1><p class="sub">Generated from <code>docs/scenarios.json</code>. This suite tracks AIWatcher as a local control loop: plan, watch, control, prove, and improve AI work without uploading prompt or source content.</p><div class="meta">Updated {html.escape(data.get('updated',''))} · {len(data['scenarios'])} scenarios · generated by scripts/generate_scenario_docs.py</div></div><div class="thesis"><b>Testing principle</b><p>Done means verified behavior, not merely code presence. Unsupported platforms should be labeled honestly and routed to Prompt Companion, MCP, wrappers, or extensions.</p></div></header>
<div class="stats"><div class="stat"><div class="n">{len(data['scenarios'])}</div><div class="l">Total scenarios</div></div><div class="stat done"><div class="n">{counts['done']}</div><div class="l">Done</div></div><div class="stat test"><div class="n">{counts['test']}</div><div class="l">To test</div></div><div class="stat partial"><div class="n">{counts['partial']}</div><div class="l">Partial</div></div><div class="stat gap"><div class="n">{counts['gap']}</div><div class="l">Not built</div></div></div>
<nav class="tabs"><button class="tab active" data-tab="requirements">Requirements</button><button class="tab" data-tab="workflows">UX Workflows</button><button class="tab" data-tab="platforms">Platform Coverage</button><button class="tab" data-tab="tests">Test Cases</button><button class="tab" data-tab="gaps">Gaps</button></nav>
<section class="section active" id="requirements"><h2>Lifecycle Requirements</h2><div class="lifecycle" id="lifecycleRail"></div><h2>Requirement Matrix</h2><table><thead><tr><th>Requirement</th><th>Lifecycle</th><th>User value</th><th>Status</th><th>Covered by</th></tr></thead><tbody id="requirementsRows"></tbody></table></section>
<section class="section" id="workflows"><h2>User Experience Workflows</h2><div class="grid two" id="workflowCards"></div></section>
<section class="section" id="platforms"><h2>Platform Coverage</h2><table><thead><tr><th>Surface</th><th>Current mechanism</th><th>Coverage</th><th>Status</th><th>What to verify</th></tr></thead><tbody id="platformRows"></tbody></table><h2>Coverage Guidance</h2><div class="callout"><b>Do not claim universal interception.</b> Verify each platform with hook-status or live behavior. Where hooks do not exist, use Prompt Companion, MCP, wrappers, or thin extensions through the local preflight API.</div></section>
<section class="section" id="tests"><h2>Scenario Test Cases</h2><div class="filters"><span class="chip on" data-kind="status" data-filter="all">All</span><span class="chip" data-kind="status" data-filter="done">Done</span><span class="chip" data-kind="status" data-filter="test">To test</span><span class="chip" data-kind="status" data-filter="partial">Partial</span><span class="chip" data-kind="status" data-filter="gap">Not built</span><span class="chip on" data-kind="phase" data-filter="all">All phases</span><span class="chip" data-kind="phase" data-filter="plan">Plan</span><span class="chip" data-kind="phase" data-filter="watch">Watch</span><span class="chip" data-kind="phase" data-filter="control">Control</span><span class="chip" data-kind="phase" data-filter="prove">Prove</span><span class="chip" data-kind="phase" data-filter="improve">Improve</span><input type="search" id="search" placeholder="Search ID, platform, phase, text"></div><div id="scenarioCards"></div><div class="empty hidden" id="empty">No scenarios match the current filter.</div></section>
<section class="section" id="gaps"><h2>Open Work</h2><div class="grid two" id="gapCards"></div></section>
</div>
<script>const DATA = {payload};</script>
<script>
const statusLabel={{done:'<span class="pill p-done">Done</span>',test:'<span class="pill p-test">To test</span>',partial:'<span class="pill p-partial">Partial</span>',gap:'<span class="pill p-gap">Not built</span>'}};
const phases=['Plan','Watch','Control','Prove','Improve'];
function esc(v){{return String(v??'').replace(/[&<>"']/g,ch=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));}}
function render(){{
 const byPhase=Object.fromEntries(phases.map(p=>[p,DATA.scenarios.filter(s=>s.phase===p)]));
 document.getElementById('lifecycleRail').innerHTML=phases.map(p=>`<div class="life" data-phase="${{p.toLowerCase()}}"><div class="name">${{p}}</div><div class="purpose">${{p==='Plan'?'Identify risky, broad, or expensive work before it starts.':p==='Watch'?'Detect context bloat, loop pressure, quota risk, and session fatigue.':p==='Control'?'Warn, gate, block, rescope, or stop risky paths.':p==='Prove'?'Record decisions, resulting sessions, local evidence, and measured impact.':'Learn what worked and make the next run smaller, safer, or more successful.'}}</div><div class="ids">${{byPhase[p].map(s=>s.id).join(', ')}}</div></div>`).join('');
 document.getElementById('requirementsRows').innerHTML=DATA.requirements.map(r=>`<tr><td>${{esc(r.requirement)}}</td><td class="mono">${{esc(r.lifecycle)}}</td><td>${{esc(r.user_value)}}</td><td>${{statusLabel[r.status]||esc(r.status)}}</td><td class="mono">${{esc(r.covered_by)}}</td></tr>`).join('');
 document.getElementById('workflowCards').innerHTML=DATA.workflows.map(w=>`<div class="card"><div class="kicker">${{esc(w.phase)}} · ${{statusLabel[w.status]||esc(w.status)}}</div><h3>${{esc(w.title)}}</h3><p>${{esc(w.body)}}</p></div>`).join('');
 document.getElementById('platformRows').innerHTML=DATA.platforms.map(p=>`<tr><td class="mono">${{esc(p.surface)}}</td><td>${{esc(p.mechanism)}}</td><td>${{esc(p.coverage)}}</td><td>${{statusLabel[p.status]||esc(p.status)}}</td><td>${{esc(p.verify)}}</td></tr>`).join('');
 document.getElementById('scenarioCards').innerHTML=DATA.scenarios.map(s=>`<article class="scenario" data-status="${{esc(s.status)}}" data-phase="${{esc(s.phase.toLowerCase())}}" data-text="${{esc((s.id+' '+s.title+' '+s.platform+' '+s.phase+' '+s.expected+' '+s.value).toLowerCase())}}"><div class="scenario-head"><span class="sid">${{esc(s.id)}}</span><span class="stitle">${{esc(s.title)}}</span>${{statusLabel[s.status]||esc(s.status)}}<span class="platform">${{esc(s.phase)}} · ${{esc(s.platform)}}</span></div><div class="scenario-body"><div class="row"><div class="label">Go to</div><div class="value">${{esc(s.go)}}</div></div><div class="row"><div class="label">Do</div><div class="value">${{esc(s.do)}}</div></div><div class="row"><div class="label">Expected</div><div class="value">${{esc(s.expected)}}</div></div><div class="row impact"><div class="label">Value</div><div class="value">${{esc(s.value)}}</div></div><div class="row"><div class="label">Why</div><div class="value">${{esc(s.why)}}</div></div></div></article>`).join('');
 const open=DATA.scenarios.filter(s=>s.status!=='done');
 document.getElementById('gapCards').innerHTML=open.map(s=>`<div class="card"><div class="kicker">${{esc(s.status)}} · ${{esc(s.phase)}} · ${{esc(s.id)}}</div><h3>${{esc(s.title)}}</h3><p>${{esc(s.expected)}}</p></div>`).join('');
 document.querySelectorAll('.scenario-head').forEach(h=>h.addEventListener('click',()=>h.closest('.scenario').classList.toggle('open')));
 document.querySelectorAll('.life').forEach(l=>l.addEventListener('click',()=>{{document.querySelector('.tab[data-tab="tests"]').click(); setFilter('phase',l.dataset.phase);}}));
}}
let statusFilter='all',phaseFilter='all',search='';
function setFilter(kind,value){{document.querySelectorAll(`.chip[data-kind="${{kind}}"]`).forEach(c=>c.classList.toggle('on',c.dataset.filter===value)); if(kind==='status')statusFilter=value; else phaseFilter=value; applyFilters();}}
function applyFilters(){{let any=false;document.querySelectorAll('.scenario').forEach(card=>{{const ok=(statusFilter==='all'||card.dataset.status===statusFilter)&&(phaseFilter==='all'||card.dataset.phase===phaseFilter)&&(!search||card.dataset.text.includes(search));card.classList.toggle('hidden',!ok); if(ok)any=true;}});document.getElementById('empty').classList.toggle('hidden',any);}}
document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>{{document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));tab.classList.add('active');document.getElementById(tab.dataset.tab).classList.add('active');}}));
document.querySelectorAll('.chip').forEach(chip=>chip.addEventListener('click',()=>setFilter(chip.dataset.kind,chip.dataset.filter)));
document.getElementById('search').addEventListener('input',e=>{{search=e.target.value.trim().toLowerCase();applyFilters();}});
render();
</script>
</body>
</html>
"""


def render_status_markdown(data: dict[str, Any]) -> str:
    counts = Counter(s["status"] for s in data["scenarios"])
    lines = [
        "# AIWatcher Local Scenario Status",
        "",
        "Generated from `docs/scenarios.json`. Do not edit by hand.",
        "",
        f"Updated: {data.get('updated', 'unknown')}",
        "",
        "| Status | Count |",
        "| --- | ---: |",
    ]
    for status in ["done", "test", "partial", "gap"]:
        lines.append(f"| {STATUS_LABELS[status]} | {counts[status]} |")
    lines.extend(["", "| ID | Phase | Status | Scenario |", "| --- | --- | --- | --- |"])
    for scenario in data["scenarios"]:
        lines.append(
            f"| {scenario['id']} | {scenario['phase']} | {STATUS_LABELS.get(scenario['status'], scenario['status'])} | {scenario['title']} |"
        )
    return "\n".join(lines) + "\n"


def render_release_checklist(data: dict[str, Any]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for scenario in sorted(data["scenarios"], key=lambda s: (STATUS_ORDER.get(s["status"], 9), s["id"])):
        if scenario["status"] != "done":
            grouped[scenario["status"]].append(scenario)
    lines = [
        "# AIWatcher Local Release Checklist",
        "",
        "Generated from `docs/scenarios.json`. Do not edit by hand.",
        "",
        "Use this before public OSS releases and before syncing status to a private team workspace.",
        "",
    ]
    for status in ["gap", "partial", "test"]:
        items = grouped.get(status, [])
        if not items:
            continue
        lines.extend([f"## {STATUS_LABELS[status]}", ""])
        for scenario in items:
            lines.extend([
                f"- [ ] `{scenario['id']}` {scenario['title']}",
                f"  - Phase: {scenario['phase']}",
                f"  - Verify: {scenario['do']}",
                f"  - Expected: {scenario['expected']}",
            ])
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    data = load_data()
    HTML_OUT.write_text(render_html(data), encoding="utf-8")
    STATUS_OUT.write_text(render_status_markdown(data), encoding="utf-8")
    CHECKLIST_OUT.write_text(render_release_checklist(data), encoding="utf-8")
    print(f"Generated {HTML_OUT.relative_to(ROOT)}")
    print(f"Generated {STATUS_OUT.relative_to(ROOT)}")
    print(f"Generated {CHECKLIST_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
