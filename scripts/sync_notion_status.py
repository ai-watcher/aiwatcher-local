#!/usr/bin/env python3
"""Append AIWatcher Local scenario status to a private Notion page.

This script is intentionally optional. It reads public repo status files but
requires private environment variables for Notion access:

  NOTION_TOKEN      Internal integration token
  NOTION_PAGE_ID    Private Notion page id where updates should be appended

No Notion credentials or page ids should be committed to this public repo.
The action is intentionally safe for public contributors: if secrets are not
configured, it exits successfully without attempting a network call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "docs" / "scenarios.json"
STATUS = ROOT / "docs" / "scenario-status.md"
CHECKLIST = ROOT / "docs" / "release-checklist.md"
NOTION_VERSION = "2022-06-28"


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _rich_text(text: str) -> list[dict[str, object]]:
    # Notion text objects have a practical size limit. Keep each paragraph short.
    return [{"type": "text", "text": {"content": text[:1800]}}]


def _paragraph(text: str) -> dict[str, object]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}}


def _heading(text: str) -> dict[str, object]:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rich_text(text)}}


def _bullets(lines: list[str]) -> list[dict[str, object]]:
    return [
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich_text(line)}}
        for line in lines
    ]


def _status_lines(data: dict[str, object]) -> list[str]:
    scenarios = data.get("scenarios", [])
    counts: dict[str, int] = {"done": 0, "test": 0, "partial": 0, "gap": 0}
    for scenario in scenarios if isinstance(scenarios, list) else []:
        if isinstance(scenario, dict):
            status = str(scenario.get("status", ""))
            if status in counts:
                counts[status] += 1
    labels = {"done": "Done", "test": "To test", "partial": "Partial", "gap": "Not built"}
    return [f"{labels[key]}: {counts[key]}" for key in ["done", "test", "partial", "gap"]]


def _phase_lines(data: dict[str, object]) -> list[str]:
    scenarios = data.get("scenarios", [])
    phases = ["Plan", "Watch", "Control", "Prove", "Improve", "Failsafe"]
    lines: list[str] = []
    for phase in phases:
        phase_items = [
            scenario
            for scenario in scenarios
            if isinstance(scenario, dict) and str(scenario.get("phase", "")) == phase
        ]
        if not phase_items:
            continue
        done = sum(1 for scenario in phase_items if scenario.get("status") == "done")
        lines.append(f"{phase}: {done}/{len(phase_items)} scenarios done")
    return lines


def _top_open_scenarios(data: dict[str, object], limit: int = 12) -> list[str]:
    priority = {"gap": 0, "partial": 1, "test": 2, "done": 3}
    scenarios = data.get("scenarios", [])
    open_items = [
        scenario
        for scenario in scenarios
        if isinstance(scenario, dict) and scenario.get("status") != "done"
    ]
    open_items.sort(key=lambda scenario: (priority.get(str(scenario.get("status")), 9), str(scenario.get("id"))))
    return [
        f"{scenario.get('id')} [{scenario.get('status')}] {scenario.get('phase')}: {scenario.get('title')}"
        for scenario in open_items[:limit]
    ]


def _scope_blocks(data: dict[str, object]) -> list[dict[str, object]]:
    scope = data.get("scope", {})
    if not isinstance(scope, dict):
        return []
    blocks = [_heading("Scope and product position")]
    position = scope.get("position")
    boundary = scope.get("oss_boundary")
    if position:
        blocks.append(_paragraph(str(position)))
    if boundary:
        blocks.append(_paragraph(f"Boundary: {boundary}"))
    strategic_filter = scope.get("strategic_filter", [])
    if isinstance(strategic_filter, list) and strategic_filter:
        blocks.append(_heading("Strategic filter"))
        blocks.extend(_bullets([str(item) for item in strategic_filter]))
    not_scope = scope.get("not_scope", [])
    if isinstance(not_scope, list) and not_scope:
        blocks.append(_heading("Not in OSS scope"))
        blocks.extend(_bullets([str(item) for item in not_scope]))
    return blocks


def _acceptance_blocks(data: dict[str, object]) -> list[dict[str, object]]:
    rules = data.get("acceptance_rules", [])
    if not isinstance(rules, list) or not rules:
        return []
    return [
        _heading("Acceptance rules"),
        *_bullets([
            f"{rule.get('title')}: {rule.get('body')}"
            for rule in rules
            if isinstance(rule, dict)
        ]),
    ]


def _requirement_blocks(data: dict[str, object]) -> list[dict[str, object]]:
    requirements = data.get("requirements", [])
    if not isinstance(requirements, list) or not requirements:
        return []
    return [
        _heading("Requirements by lifecycle"),
        *_bullets([
            f"{item.get('lifecycle')} [{item.get('status')}]: {item.get('requirement')} - {item.get('user_value')} ({item.get('covered_by')})"
            for item in requirements
            if isinstance(item, dict)
        ]),
    ]


def _workflow_blocks(data: dict[str, object]) -> list[dict[str, object]]:
    workflows = data.get("workflows", [])
    examples = data.get("examples", [])
    blocks: list[dict[str, object]] = []
    if isinstance(workflows, list) and workflows:
        blocks.append(_heading("UX workflows"))
        blocks.extend(_bullets([
            f"{workflow.get('title')} [{workflow.get('status')}]: {workflow.get('body')}"
            for workflow in workflows
            if isinstance(workflow, dict)
        ]))
    if isinstance(examples, list) and examples:
        blocks.append(_heading("Concrete use cases"))
        blocks.extend(_bullets([
            f"{example.get('situation')} -> {example.get('response')} ({example.get('status')})"
            for example in examples
            if isinstance(example, dict)
        ]))
    return blocks


def _platform_blocks(data: dict[str, object]) -> list[dict[str, object]]:
    platforms = data.get("platforms", [])
    if not isinstance(platforms, list) or not platforms:
        return []
    return [
        _heading("Platform coverage"),
        _paragraph("Do not claim universal interception. Verify each surface with hook-status, observed host UI behavior, extension telemetry, or an explicit fallback path."),
        *_bullets([
            f"{platform.get('surface')} [{platform.get('status')}]: {platform.get('coverage')} via {platform.get('mechanism')}. Verify: {platform.get('verify')}"
            for platform in platforms
            if isinstance(platform, dict)
        ]),
    ]


def _decision_blocks(data: dict[str, object]) -> list[dict[str, object]]:
    decisions = data.get("open_decisions", [])
    if not isinstance(decisions, list) or not decisions:
        return []
    return [
        _heading("Open decisions"),
        *_bullets([
            f"{decision.get('decision')} [{decision.get('status')}]: {decision.get('options')} Recommendation: {decision.get('recommendation')}"
            for decision in decisions
            if isinstance(decision, dict)
        ]),
    ]


def _scenario_blocks(data: dict[str, object]) -> list[dict[str, object]]:
    scenarios = data.get("scenarios", [])
    if not isinstance(scenarios, list) or not scenarios:
        return []
    return [
        _heading("Detailed scenario checklist"),
        *_bullets([
            f"{scenario.get('id')} [{scenario.get('status')}] {scenario.get('phase')} / {scenario.get('platform')}: {scenario.get('title')} | Test: {scenario.get('do')} | Expected: {scenario.get('expected')}"
            for scenario in scenarios
            if isinstance(scenario, dict)
        ]),
    ]


def build_blocks() -> list[dict[str, object]]:
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    sha = _git(["rev-parse", "--short", "HEAD"])
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_url = os.environ.get("GITHUB_RUN_URL", "")
    body = textwrap.dedent(f"""
    Source: ai-watcher/aiwatcher-local
    Branch: {branch}
    Commit: {sha}
    Generated: {stamp}
    Scenario source: docs/scenarios.json
    Run: {run_url or 'local/manual'}
    """).strip()
    blocks = [
        _heading(f"AIWatcher Local status - {stamp}"),
        _paragraph(body),
        *_scope_blocks(data),
        *_acceptance_blocks(data),
        _heading("Status counts"),
        *_bullets(_status_lines(data)),
        _heading("Lifecycle coverage"),
        *_bullets(_phase_lines(data)),
        *_requirement_blocks(data),
        *_workflow_blocks(data),
        *_platform_blocks(data),
        *_decision_blocks(data),
    ]
    top_open = _top_open_scenarios(data)
    if top_open:
        blocks.append(_heading("Top open scenarios"))
        blocks.extend(_bullets(top_open))
        remaining = max(0, len([s for s in data.get("scenarios", []) if isinstance(s, dict) and s.get("status") != "done"]) - len(top_open))
        if remaining:
            blocks.append(_paragraph(f"{remaining} more open scenarios are in docs/release-checklist.md."))
    blocks.extend(_scenario_blocks(data))
    blocks.append(_paragraph("Generated from docs/scenarios.json. Public repo contains no Notion credentials. Generated HTML: docs/aiwatcher-scenario-tests.html. Compact status: docs/scenario-status.md. Release checklist: docs/release-checklist.md."))
    return blocks


def append_to_notion(page_id: str, token: str, blocks: list[dict[str, object]]) -> None:
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    for index in range(0, len(blocks), 90):
        chunk = blocks[index:index + 90]
        payload = json.dumps({"children": chunk}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="PATCH",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Notion API failed: HTTP {exc.code} {detail}") from exc


def main() -> int:
    token = os.environ.get("NOTION_TOKEN")
    page_id = os.environ.get("NOTION_PAGE_ID")
    if not token or not page_id:
        print("Skipping Notion sync: NOTION_TOKEN and NOTION_PAGE_ID are required.")
        return 0
    append_to_notion(page_id, token, build_blocks())
    print("Synced AIWatcher scenario status to Notion.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
