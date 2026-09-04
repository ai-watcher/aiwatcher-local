from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiwatcher_cli import pull_requests, tasks
from aiwatcher_cli.scanner import LocalSession


T0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _completed(stdout: str = "[]", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


class ListPullRequestsTests(unittest.TestCase):
    def setUp(self) -> None:
        pull_requests._CACHE.clear()

    def test_missing_gh_is_unmeasurable_not_empty(self) -> None:
        with patch.object(pull_requests.shutil, "which", return_value=None):
            lookup = pull_requests.list_pull_requests("/tmp/repo")
        self.assertFalse(lookup.available)
        self.assertIn("not installed", lookup.reason)
        self.assertEqual(lookup.pull_requests, [])

    def test_gh_failure_carries_its_first_line_as_the_reason(self) -> None:
        with patch.object(pull_requests.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(pull_requests.subprocess, "run", return_value=_completed("", 1, "gh: not logged in\nrun gh auth login")):
            lookup = pull_requests.list_pull_requests("/tmp/repo")
        self.assertFalse(lookup.available)
        self.assertIn("not logged in", lookup.reason)

    def test_rows_are_normalised_and_cached(self) -> None:
        rows = [{"number": 99, "title": "fix(pricing)", "url": "https://x/99", "headRefName": "fix/pricing",
                 "createdAt": "2026-09-03T07:25:00Z", "mergedAt": None, "closedAt": None, "state": "OPEN"}]
        with patch.object(pull_requests.shutil, "which", return_value="/usr/bin/gh"), \
             patch.object(pull_requests.subprocess, "run", return_value=_completed(json.dumps(rows))) as run:
            first = pull_requests.list_pull_requests("/tmp/repo", now=100.0)
            second = pull_requests.list_pull_requests("/tmp/repo", now=200.0)
        self.assertTrue(first.available)
        self.assertEqual(first.pull_requests[0]["number"], 99)
        self.assertEqual(first.pull_requests[0]["state"], "open")
        self.assertEqual(first.pull_requests[0]["repo_root"], "/tmp/repo")
        self.assertIs(first, second)
        self.assertEqual(run.call_count, 1)
        self.assertIn("--author", run.call_args.args[0])


class AttachPullRequestTests(unittest.TestCase):
    def test_pr_lands_on_the_task_open_when_it_was_opened(self) -> None:
        session = LocalSession(session_id="s", tool="claude-code", project_path="/tmp/repo/sub", started_at=T0,
                               updated_at=T0 + timedelta(hours=2), source_path="/tmp/s.jsonl")
        segments = [
            {"prompt": "fix the pricing table", "turn": 1, "at": T0.isoformat(), "tokens": 10, "cost_usd": 0.1, "tool_calls": 1},
            {"prompt": "now write the release notes for v2", "turn": 2, "at": (T0 + timedelta(minutes=40)).isoformat(),
             "tokens": 10, "cost_usd": 0.1, "tool_calls": 1},
        ]
        result = tasks.build_session_tasks(session, segments, now=T0 + timedelta(days=1))
        for task in result:
            task["repo_root"] = "/tmp/repo"
        tasks.attach_pull_requests(result, [
            {"number": 7, "title": "pricing", "url": "u", "state": "merged", "opened_at": (T0 + timedelta(minutes=20)).isoformat(),
             "merged_at": None, "repo_root": "/tmp/repo"},
            {"number": 8, "title": "elsewhere", "url": "u", "state": "open", "opened_at": (T0 + timedelta(minutes=20)).isoformat(),
             "merged_at": None, "repo_root": "/tmp/other"},
        ])
        self.assertEqual([pr["number"] for pr in result[0]["pull_requests"]], [7])
        self.assertEqual(result[1]["pull_requests"], [])


if __name__ == "__main__":
    unittest.main()
