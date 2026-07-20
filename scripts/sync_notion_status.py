#!/usr/bin/env python3
"""Sync AIWatcher Local scenario status to a private Notion workspace.

This script is intentionally optional. It reads public repo status files but
requires private environment variables for Notion access:

  NOTION_TOKEN      Internal integration token
  NOTION_PAGE_ID    Private Notion page id that owns the team status workspace

No Notion credentials or page ids should be committed to this public repo.
The action is safe for public contributors: if secrets are not configured, it
exits successfully without attempting a network call.
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = Path(os.environ.get("AIWATCHER_SCENARIOS_PATH", ROOT / "docs" / "scenarios.json"))
NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"

SECTION_PAGES = [
    "AIWatcher Review Home",
    "Scope",
    "Requirements",
    "UX Workflows",
    "Gaps",
    "Test Cases",
]
TRACKER_TITLE = "Scenario Tracker"


def _git(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _request(method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Notion API failed: {method} {path} HTTP {exc.code} {detail}") from exc
    return json.loads(raw) if raw else {}


def _children(block_id: str, token: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor = None
    while True:
        suffix = f"&start_cursor={cursor}" if cursor else ""
        page = _request("GET", f"/blocks/{block_id}/children?page_size=100{suffix}", token)
        results.extend(page.get("results", []))
        if not page.get("has_more"):
            return results
        cursor = page.get("next_cursor")


def _archive_block(block_id: str, token: str) -> bool:
    try:
        _request("DELETE", f"/blocks/{block_id}", token)
        return True
    except RuntimeError as exc:
        print(f"Warning: could not archive Notion block {block_id}: {exc}", file=sys.stderr)
        return False


def _clean_page_content(page_id: str, token: str, *, keep_children: bool = True) -> bool:
    cleaned = True
    for block in _children(page_id, token):
        block_type = block.get("type")
        if keep_children and block_type in {"child_page", "child_database"}:
            continue
        cleaned = _archive_block(str(block["id"]), token) and cleaned
    return cleaned


def _append_blocks(block_id: str, token: str, blocks: list[dict[str, Any]]) -> None:
    for index in range(0, len(blocks), 90):
        _request("PATCH", f"/blocks/{block_id}/children", token, {"children": blocks[index:index + 90]})


def _rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": str(text)[:1900]}}]


def _paragraph(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}}


def _heading(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rich_text(text)}}


def _heading3(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rich_text(text)}}


def _divider() -> dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def _bullets(lines: list[str]) -> list[dict[str, Any]]:
    return [
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich_text(line)}}
        for line in lines
    ]


def _code(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "code",
        "code": {"rich_text": _rich_text(text), "language": "plain text"},
    }


def _page_id_to_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


def _status_counts(data: dict[str, Any]) -> dict[str, int]:
    counts = {"done": 0, "test": 0, "partial": 0, "gap": 0}
    for scenario in data.get("scenarios", []):
        if isinstance(scenario, dict):
            status = str(scenario.get("status", ""))
            if status in counts:
                counts[status] += 1
    return counts


def _team_status(status: str) -> str:
    return {
        "done": "Done",
        "partial": "In progress",
        "test": "To verify",
        "gap": "Blocker",
    }.get(status, "In progress")


def _priority(status: str) -> str:
    return {
        "gap": "P1",
        "partial": "P2",
        "test": "P2",
        "done": "Done",
    }.get(status, "P2")


def _phase_lines(data: dict[str, Any]) -> list[str]:
    scenarios = data.get("scenarios", [])
    phases = ["Plan", "Watch", "Control", "Prove", "Improve", "Failsafe"]
    lines: list[str] = []
    for phase in phases:
        phase_items = [item for item in scenarios if isinstance(item, dict) and item.get("phase") == phase]
        if not phase_items:
            continue
        done = sum(1 for item in phase_items if item.get("status") == "done")
        blockers = sum(1 for item in phase_items if item.get("status") == "gap")
        lines.append(f"{phase}: {done}/{len(phase_items)} done, {blockers} blocker(s)")
    return lines


def _top_open_scenarios(data: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    order = {"gap": 0, "partial": 1, "test": 2, "done": 3}
    items = [item for item in data.get("scenarios", []) if isinstance(item, dict) and item.get("status") != "done"]
    return sorted(items, key=lambda item: (order.get(str(item.get("status")), 9), str(item.get("id"))))[:limit]


def _find_existing_children(parent_id: str, token: str) -> tuple[dict[str, str], dict[str, str]]:
    pages: dict[str, str] = {}
    databases: dict[str, str] = {}
    for block in _children(parent_id, token):
        if block.get("type") == "child_page":
            pages[str(block["child_page"]["title"])] = str(block["id"])
        if block.get("type") == "child_database":
            databases[str(block["child_database"]["title"])] = str(block["id"])
    return pages, databases


def _ensure_page(parent_id: str, token: str, title: str, existing_pages: dict[str, str]) -> str:
    if title in existing_pages:
        return existing_pages[title]
    response = _request(
        "POST",
        "/pages",
        token,
        {
            "parent": {"type": "page_id", "page_id": parent_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        },
    )
    return str(response["id"])


def _ensure_tracker(parent_id: str, token: str, existing_databases: dict[str, str]) -> str:
    if TRACKER_TITLE in existing_databases:
        return existing_databases[TRACKER_TITLE]
    response = _request(
        "POST",
        "/databases",
        token,
        {
            "parent": {"type": "page_id", "page_id": parent_id},
            "title": [{"type": "text", "text": {"content": TRACKER_TITLE}}],
            "properties": {
                "Name": {"title": {}},
                "ID": {"rich_text": {}},
                "Status": {
                    "select": {
                        "options": [
                            {"name": "Done", "color": "green"},
                            {"name": "In progress", "color": "purple"},
                            {"name": "To verify", "color": "blue"},
                            {"name": "Blocker", "color": "red"},
                        ]
                    }
                },
                "Phase": {
                    "select": {
                        "options": [
                            {"name": "Plan", "color": "blue"},
                            {"name": "Watch", "color": "yellow"},
                            {"name": "Control", "color": "red"},
                            {"name": "Prove", "color": "green"},
                            {"name": "Improve", "color": "purple"},
                            {"name": "Failsafe", "color": "gray"},
                        ]
                    }
                },
                "Priority": {
                    "select": {
                        "options": [
                            {"name": "P1", "color": "red"},
                            {"name": "P2", "color": "yellow"},
                            {"name": "Done", "color": "green"},
                        ]
                    }
                },
                "Platform": {"rich_text": {}},
                "Verify": {"rich_text": {}},
                "Expected": {"rich_text": {}},
                "Value": {"rich_text": {}},
            },
        },
    )
    return str(response["id"])


def _query_tracker(database_id: str, token: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    cursor = None
    while True:
        payload: dict[str, Any] = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        response = _request("POST", f"/databases/{database_id}/query", token, payload)
        for page in response.get("results", []):
            props = page.get("properties", {})
            rich_id = props.get("ID", {}).get("rich_text", [])
            if rich_id:
                rows[str(rich_id[0].get("plain_text", ""))] = str(page["id"])
        if not response.get("has_more"):
            return rows
        cursor = response.get("next_cursor")


def _scenario_properties(scenario: dict[str, Any]) -> dict[str, Any]:
    scenario_id = str(scenario.get("id", ""))
    return {
        "Name": {"title": [{"type": "text", "text": {"content": f"{scenario_id} {scenario.get('title', '')}"[:1900]}}]},
        "ID": {"rich_text": [{"type": "text", "text": {"content": scenario_id}}]},
        "Status": {"select": {"name": _team_status(str(scenario.get("status", "")))}},
        "Phase": {"select": {"name": str(scenario.get("phase", "Plan"))}},
        "Priority": {"select": {"name": _priority(str(scenario.get("status", "")))}},
        "Platform": {"rich_text": [{"type": "text", "text": {"content": str(scenario.get("platform", ""))[:1900]}}]},
        "Verify": {"rich_text": [{"type": "text", "text": {"content": str(scenario.get("do", ""))[:1900]}}]},
        "Expected": {"rich_text": [{"type": "text", "text": {"content": str(scenario.get("expected", ""))[:1900]}}]},
        "Value": {"rich_text": [{"type": "text", "text": {"content": str(scenario.get("value", ""))[:1900]}}]},
    }


def _sync_tracker(database_id: str, token: str, data: dict[str, Any]) -> None:
    existing = _query_tracker(database_id, token)
    for scenario in data.get("scenarios", []):
        if not isinstance(scenario, dict):
            continue
        scenario_id = str(scenario.get("id", ""))
        properties = _scenario_properties(scenario)
        if scenario_id in existing:
            _request("PATCH", f"/pages/{existing[scenario_id]}", token, {"properties": properties})
        else:
            _request(
                "POST",
                "/pages",
                token,
                {"parent": {"type": "database_id", "database_id": database_id}, "properties": properties},
            )


def _home_blocks(data: dict[str, Any], pages: dict[str, str], database_id: str) -> list[dict[str, Any]]:
    sha = _git(["rev-parse", "--short", "HEAD"])
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_url = os.environ.get("GITHUB_RUN_URL", "local/manual")
    counts = _status_counts(data)
    total = len([item for item in data.get("scenarios", []) if isinstance(item, dict)])
    lines = [
        f"{total} scenarios: {counts['done']} done, {counts['partial']} in progress, {counts['test']} to verify, {counts['gap']} blockers.",
        f"Branch: {branch} · Commit: {sha} · Generated: {stamp}",
        f"Run: {run_url}",
    ]
    links = [f"{title}: {_page_id_to_url(page_id)}" for title, page_id in pages.items() if title in SECTION_PAGES]
    links.append(f"{TRACKER_TITLE}: {_page_id_to_url(database_id)}")
    open_work = [
        f"{item.get('id')} [{_team_status(str(item.get('status')))}] {item.get('phase')}: {item.get('title')}"
        for item in _top_open_scenarios(data)
    ]
    return [
        _heading("AIWatcher Local review"),
        _paragraph("Team-facing status for the OSS control-loop roadmap. Use Scenario Tracker for Notion-native filters; keep any richer HTML review surface in a private owner-only location."),
        *_bullets(lines),
        _heading("Navigation"),
        *_bullets(links),
        _heading("Lifecycle progress"),
        *_bullets(_phase_lines(data)),
        _heading("Highest-priority open work"),
        *_bullets(open_work),
        _divider(),
        _paragraph("Generated from docs/scenarios.json. Public repo contains no Notion credentials."),
    ]


def _scope_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    scope = data.get("scope", {})
    if not isinstance(scope, dict):
        return [_paragraph("No scope data found.")]
    blocks = [
        _heading("Scope and product position"),
        _paragraph(str(scope.get("position", ""))),
        _heading3("OSS boundary"),
        _paragraph(str(scope.get("oss_boundary", ""))),
        _heading3("Strategic filter"),
        *_bullets([str(item) for item in scope.get("strategic_filter", []) if item]),
        _heading3("Not in OSS scope"),
        *_bullets([str(item) for item in scope.get("not_scope", []) if item]),
    ]
    return blocks


def _requirements_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [_heading("Requirements by lifecycle")]
    for lifecycle in ["Plan", "Watch", "Control", "Prove", "Improve", "Failsafe"]:
        items = [
            item for item in data.get("requirements", [])
            if isinstance(item, dict) and item.get("lifecycle") == lifecycle
        ]
        if not items:
            continue
        blocks.append(_heading3(lifecycle))
        blocks.extend(_bullets([
            f"{item.get('requirement')} [{_team_status(str(item.get('status')))}] - {item.get('user_value')} Covered by: {item.get('covered_by')}"
            for item in items
        ]))
    return blocks


def _workflow_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [_heading("UX workflows and concrete use cases")]
    blocks.append(_heading3("Workflows"))
    blocks.extend(_bullets([
        f"{item.get('title')} [{_team_status(str(item.get('status')))}]: {item.get('body')}"
        for item in data.get("workflows", [])
        if isinstance(item, dict)
    ]))
    blocks.append(_heading3("Examples"))
    blocks.extend(_bullets([
        f"{item.get('situation')} -> {item.get('response')} Expected feeling: {item.get('feeling')} [{_team_status(str(item.get('status')))}]"
        for item in data.get("examples", [])
        if isinstance(item, dict)
    ]))
    return blocks


def _gaps_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [_heading("Gaps and open decisions")]
    for status, title in [("gap", "Blockers"), ("partial", "In progress"), ("test", "To verify")]:
        items = [item for item in data.get("scenarios", []) if isinstance(item, dict) and item.get("status") == status]
        if not items:
            continue
        blocks.append(_heading3(title))
        blocks.extend(_bullets([
            f"{item.get('id')} {item.get('phase')}: {item.get('title')} - {item.get('expected')}"
            for item in items
        ]))
    decisions = data.get("open_decisions", [])
    if isinstance(decisions, list) and decisions:
        blocks.append(_heading3("Open decisions"))
        blocks.extend(_bullets([
            f"{item.get('decision')} [{item.get('status')}]: {item.get('options')} Recommendation: {item.get('recommendation')}"
            for item in decisions
            if isinstance(item, dict)
        ]))
    return blocks


def _test_case_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = [
        _heading("Test cases"),
        _paragraph("Use Scenario Tracker for filtering and search. This page keeps the full checklist readable in one place."),
    ]
    for phase in ["Plan", "Watch", "Control", "Prove", "Improve", "Failsafe"]:
        items = [item for item in data.get("scenarios", []) if isinstance(item, dict) and item.get("phase") == phase]
        if not items:
            continue
        blocks.append(_heading3(phase))
        for item in items:
            blocks.append(_code(textwrap.dedent(f"""
            {item.get('id')} [{_team_status(str(item.get('status')))}] {item.get('title')}
            Platform: {item.get('platform')}
            Go to: {item.get('go')}
            Do: {item.get('do')}
            Expected: {item.get('expected')}
            Value: {item.get('value')}
            Why: {item.get('why')}
            """).strip()))
    return blocks


def _platform_blocks(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _heading("Platform coverage"),
        _paragraph("Do not claim universal interception. Verify each surface with hook-status, observed host UI behavior, extension telemetry, or an explicit fallback path."),
        *_bullets([
            f"{item.get('surface')} [{_team_status(str(item.get('status')))}]: {item.get('coverage')} via {item.get('mechanism')}. Verify: {item.get('verify')}"
            for item in data.get("platforms", [])
            if isinstance(item, dict)
        ]),
    ]


def _sync_page(page_id: str, token: str, blocks: list[dict[str, Any]]) -> None:
    existing = _children(page_id, token)
    cleaned = _clean_page_content(page_id, token, keep_children=False)
    if existing and not cleaned:
        print(
            f"Warning: skipped refreshing page {page_id} because old blocks could not be archived.",
            file=sys.stderr,
        )
        return
    _append_blocks(page_id, token, blocks)


def sync_workspace(parent_id: str, token: str) -> None:
    if not SCENARIOS.exists():
        print(
            f"Skipping Notion sync: scenario source not found at {SCENARIOS}. "
            "Set AIWATCHER_SCENARIOS_PATH to a private scenarios.json file.",
            file=sys.stderr,
        )
        return
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    _clean_page_content(parent_id, token, keep_children=True)
    pages, databases = _find_existing_children(parent_id, token)
    page_ids = {title: _ensure_page(parent_id, token, title, pages) for title in SECTION_PAGES}
    database_id = _ensure_tracker(parent_id, token, databases)
    _sync_tracker(database_id, token, data)
    _sync_page(page_ids["AIWatcher Review Home"], token, _home_blocks(data, page_ids, database_id))
    _sync_page(page_ids["Scope"], token, _scope_blocks(data))
    _sync_page(page_ids["Requirements"], token, _requirements_blocks(data))
    _sync_page(page_ids["UX Workflows"], token, _workflow_blocks(data))
    _sync_page(page_ids["Gaps"], token, _gaps_blocks(data))
    _sync_page(page_ids["Test Cases"], token, _test_case_blocks(data))


def main() -> int:
    token = os.environ.get("NOTION_TOKEN")
    page_id = os.environ.get("NOTION_PAGE_ID")
    if not token or not page_id:
        print("Skipping Notion sync: NOTION_TOKEN and NOTION_PAGE_ID are required.")
        return 0
    sync_workspace(page_id, token)
    print("Synced AIWatcher scenario workspace to Notion.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
