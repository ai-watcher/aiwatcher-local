from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401

from aiwatcher_cli import cli, runtime_lifecycle


class RuntimeLockTests(unittest.TestCase):
    def test_role_lock_is_singleton_and_reusable_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = str(Path(temp_dir) / "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                first = runtime_lifecycle.acquire_runtime_lock(
                    "dashboard", metadata={"port": 8765}
                )
                self.assertIsNotNone(first)
                self.assertIsNone(runtime_lifecycle.acquire_runtime_lock("dashboard"))
                status = runtime_lifecycle.runtime_lock_status("dashboard")
                self.assertTrue(status["active"])
                self.assertEqual(status["pid"], os.getpid())
                self.assertEqual(status["port"], 8765)

                first.release()
                second = runtime_lifecycle.acquire_runtime_lock(
                    "dashboard", metadata={"port": 8799}
                )
                self.assertIsNotNone(second)
                restarted = runtime_lifecycle.runtime_lock_status("dashboard")
                self.assertTrue(restarted["active"])
                self.assertEqual(restarted["port"], 8799)
                second.release()

    def test_role_lock_excludes_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = str(Path(temp_dir) / "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                ownership = runtime_lifecycle.acquire_runtime_lock("companion")
                self.assertIsNotNone(ownership)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from aiwatcher_cli.runtime_lifecycle import acquire_runtime_lock; "
                            "raise SystemExit(0 if acquire_runtime_lock('companion') is None else 1)"
                        ),
                    ],
                    check=False,
                    env=os.environ.copy(),
                )
                ownership.release()

        self.assertEqual(completed.returncode, 0)

    def test_unowned_lock_reports_inactive_with_last_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = str(Path(temp_dir) / "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                ownership = runtime_lifecycle.acquire_runtime_lock(
                    "companion-ui", metadata={"url": "http://127.0.0.1:8765"}
                )
                ownership.release()
                status = runtime_lifecycle.runtime_lock_status("companion-ui")

        self.assertFalse(status["active"])
        self.assertEqual(status["url"], "http://127.0.0.1:8765")

    def test_metadata_is_stored_separately_from_the_locked_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = str(Path(temp_dir) / "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                ownership = runtime_lifecycle.acquire_runtime_lock(
                    "dashboard", metadata={"port": 8765}
                )
                ownership.release()
                lock_payload = runtime_lifecycle._lock_path("dashboard").read_bytes()
                metadata = json.loads(
                    runtime_lifecycle._metadata_path("dashboard").read_text(encoding="utf-8")
                )

        self.assertEqual(lock_payload, b"\0")
        self.assertEqual(metadata["port"], 8765)

    def test_invalid_lock_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runtime_lifecycle.acquire_runtime_lock("../../unsafe")

    def test_role_start_waits_for_a_short_status_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = str(Path(temp_dir) / "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                probe = runtime_lifecycle.acquire_runtime_lock("dashboard")
                self.assertIsNotNone(probe)
                released = threading.Event()

                def release_probe() -> None:
                    time.sleep(0.05)
                    probe.release()
                    released.set()

                thread = threading.Thread(target=release_probe)
                thread.start()
                ownership = runtime_lifecycle.acquire_runtime_lock("dashboard", timeout=1)
                thread.join()

                self.assertTrue(released.is_set())
                self.assertIsNotNone(ownership)
                ownership.release()


class RuntimeStatusTests(unittest.TestCase):
    def test_runtime_status_groups_product_roles_and_keeps_mcp_optional(self) -> None:
        rows = {
            "dashboard": {"active": True, "pid": 101},
            "companion": {"active": True, "pid": 102},
            "companion-ui": {"active": True, "pid": 103},
        }
        with patch.object(cli, "runtime_lock_status", side_effect=lambda role: rows[role]):
            snapshot = cli.runtime_status_snapshot()
            rendered = cli.render_runtime_status(snapshot)

        self.assertTrue(snapshot["healthy"])
        self.assertFalse(snapshot["mcp"]["required"])
        self.assertIn("Dashboard: running (PID 101)", rendered)
        self.assertIsNone(snapshot["mcp"]["process_count"])
        self.assertIn("MCP servers: optional and connection-scoped", rendered)
        self.assertIn("process count is not observed", rendered)

    def test_runtime_status_parser_supports_json(self) -> None:
        args = cli.build_parser().parse_args(["runtime", "status", "--json"])

        self.assertIs(args.func, cli.command_runtime)
        self.assertTrue(args.json)

    def test_runtime_status_is_available_to_mcp_clients(self) -> None:
        names = {str(spec["name"]) for spec in cli._mcp_tool_specs()}
        snapshot = {
            "roles": {
                "dashboard": {"active": True, "pid": 1},
                "companion": {"active": True, "pid": 2},
                "companion_ui": {"active": False},
            },
            "healthy": True,
            "mcp": {"required": False},
        }
        with patch.object(cli, "runtime_status_snapshot", return_value=snapshot):
            result = cli._mcp_tool_call("aiwatcher_runtime_status", {})

        self.assertIn("aiwatcher_runtime_status", names)
        self.assertIn("Healthy local runtime: yes", result)


if __name__ == "__main__":
    unittest.main()
