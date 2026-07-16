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
        _heading("Status counts"),
        *_bullets(_status_lines(data)),
        _heading("Lifecycle coverage"),
        *_bullets(_phase_lines(data)),
    ]
    top_open = _top_open_scenarios(data)
    if top_open:
        blocks.append(_heading("Top open scenarios"))
        blocks.extend(_bullets(top_open))
        remaining = max(0, len([s for s in data.get("scenarios", []) if isinstance(s, dict) and s.get("status") != "done"]) - len(top_open))
        if remaining:
            blocks.append(_paragraph(f"{remaining} more open scenarios are in docs/release-checklist.md."))
    blocks.append(_paragraph("Generated from docs/scenarios.json. Public repo contains no Notion credentials."))
    return blocks


def append_to_notion(page_id: str, token: str, blocks: list[dict[str, object]]) -> None:
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    payload = json.dumps({"children": blocks}).encode("utf-8")
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
