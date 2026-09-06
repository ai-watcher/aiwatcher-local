from __future__ import annotations

import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import cli
from aiwatcher_cli.processes import (
    ProcessRow,
    classify_process,
    parse_ps_line,
    process_record,
    seconds_label,
)


class RuntimeProcessTests(unittest.TestCase):
    def test_parse_ps_line_handles_command_with_spaces(self) -> None:
        row = parse_ps_line(
            "123 1 T 02:03:04 204800 12.5 /Applications/Codex.app/Contents/MacOS/cua_node kernel.js --session-id abc --working-dir /repo"
        )

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.pid, 123)
        self.assertEqual(row.ppid, 1)
        self.assertEqual(row.state, "T")
        self.assertEqual(row.age_seconds, 7384)
        self.assertEqual(row.rss_kb, 204800)
        self.assertEqual(row.cpu_percent, 12.5)
        self.assertIn("--session-id abc", row.command)

    def test_classifies_orphaned_codex_kernel_without_claiming_spend(self) -> None:
        row = ProcessRow(
            pid=123,
            ppid=1,
            state="S",
            age_seconds=3 * 3600,
            rss_kb=512000,
            cpu_percent=2.3,
            command="/Applications/Codex.app/Contents/MacOS/cua_node /tmp/missing-kernel/kernel.js --session-id codex-1 --working-dir /tmp/missing-work",
        )

        with patch("aiwatcher_cli.processes.os.path.exists", return_value=False):
            process = classify_process(row, stale_minutes=120)

        self.assertIsNotNone(process)
        assert process is not None
        self.assertEqual(process.tool, "Codex runtime")
        self.assertEqual(process.session_id, "codex-1")
        self.assertEqual(process.cwd, "/tmp/missing-work")
        self.assertIn("orphaned parent (PPID=1)", process.stale_reasons)
        self.assertIn("older than 120m", process.stale_reasons)
        self.assertIn("kernel path is missing", process.stale_reasons)
        self.assertIn("working directory is missing", process.stale_reasons)
        record = process_record(process)
        self.assertEqual(record["suggested_kill_command"], "taskkill /PID 123" if os.name == "nt" else "kill 123")
        self.assertIn("not model/API spend", record["resource_note"])

    def test_classifies_stopped_computer_use_client(self) -> None:
        row = ProcessRow(
            pid=456,
            ppid=88,
            state="T",
            age_seconds=60,
            rss_kb=64000,
            cpu_percent=0.0,
            command="/usr/local/bin/SkyComputerUseClient --workspace /repo",
        )

        with patch("aiwatcher_cli.processes.os.path.exists", return_value=True):
            process = classify_process(row, stale_minutes=120)

        self.assertIsNotNone(process)
        assert process is not None
        self.assertEqual(process.tool, "Computer Use")
        self.assertEqual(process.cwd, "/repo")
        self.assertEqual(process.stale_reasons, ["stopped/suspended process"])

    def test_ignores_unrelated_processes(self) -> None:
        row = ProcessRow(
            pid=789,
            ppid=1,
            state="S",
            age_seconds=10000,
            rss_kb=1000,
            cpu_percent=0.0,
            command="/usr/bin/python3 -m http.server",
        )

        self.assertIsNone(classify_process(row))

    def test_ignores_tool_names_in_unrelated_arguments(self) -> None:
        row = ProcessRow(
            pid=790,
            ppid=1,
            state="S",
            age_seconds=10000,
            rss_kb=1000,
            cpu_percent=0.0,
            command="/usr/bin/python3 script.py --note codex",
        )

        self.assertIsNone(classify_process(row))

    def test_long_running_app_is_not_stale_from_age_alone(self) -> None:
        row = ProcessRow(
            pid=791,
            ppid=42,
            state="S",
            age_seconds=5 * 3600,
            rss_kb=256000,
            cpu_percent=0.0,
            command="/Applications/Codex.app/Contents/MacOS/Codex",
        )

        process = classify_process(row, stale_minutes=120)

        self.assertIsNotNone(process)
        assert process is not None
        self.assertEqual(process.tool, "Codex")
        self.assertFalse(process.stale)
        self.assertEqual(process.stale_reasons, [])

    def test_seconds_label_is_compact(self) -> None:
        self.assertEqual(seconds_label(None), "unknown")
        self.assertEqual(seconds_label(90), "2m")
        self.assertEqual(seconds_label(7200), "2h")
        self.assertEqual(seconds_label(49 * 3600), "2d 1h")


class RuntimeProcessCliTests(unittest.TestCase):
    def test_processes_command_renders_stale_reason_and_manual_kill_suggestion(self) -> None:
        process = classify_process(
            ProcessRow(
                pid=321,
                ppid=1,
                state="S",
                age_seconds=9000,
                rss_kb=128000,
                cpu_percent=1.5,
                command="node node_repl/kernel.js --session-id repl-1 --working-dir /repo",
            ),
            stale_minutes=60,
        )
        assert process is not None
        output = io.StringIO()
        args = SimpleNamespace(stale_only=True, min_age_minutes=60, json=False)

        with (
            patch("aiwatcher_cli.cli.discover_runtime_processes", return_value=[process]),
            patch("sys.stdout", output),
        ):
            result = cli.command_processes(args)

        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn("runtime hygiene", rendered)
        self.assertIn("RSS/CPU are local machine resources, not model/API spend", rendered)
        self.assertIn("orphaned parent", rendered)
        self.assertIn("Potential local reward: up to 125MB RAM relief", rendered)
        self.assertIn("dollar/API savings need provider billing or session-token evidence", rendered)
        expected_kill = "taskkill /PID 321" if os.name == "nt" else "kill 321"
        self.assertIn(f"suggest: review, then run `{expected_kill}`", rendered)

    def test_processes_command_json_is_structured(self) -> None:
        process = classify_process(
            ProcessRow(
                pid=654,
                ppid=1,
                state="S",
                age_seconds=9000,
                rss_kb=128000,
                cpu_percent=1.5,
                command="node node_repl/kernel.js --session-id repl-2 --working-dir /repo",
            ),
            stale_minutes=60,
        )
        assert process is not None
        output = io.StringIO()
        args = SimpleNamespace(stale_only=False, min_age_minutes=60, json=True)

        with (
            patch("aiwatcher_cli.cli.discover_runtime_processes", return_value=[process]),
            patch("sys.stdout", output),
        ):
            result = cli.command_processes(args)

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schema"], "aiwatcher.local_runtime_processes.v0")
        self.assertEqual(payload["processes"][0]["pid"], 654)
        self.assertEqual(
            payload["processes"][0]["suggested_kill_command"],
            "taskkill /PID 654" if os.name == "nt" else "kill 654",
        )
        self.assertNotIn("command", payload["processes"][0])
        self.assertEqual(payload["processes"][0]["executable"], "node")
