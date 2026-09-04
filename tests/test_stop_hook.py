"""The Stop hook behind 'turn ended'. Same posture as the activity hook: never fails the host, never prints."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from aiwatcher_cli import cli, local_state


class ClaudeStopHookTests(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.mkdtemp()
        self._previous = os.environ.get("AIWATCHER_HOME")
        os.environ["AIWATCHER_HOME"] = self._home

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("AIWATCHER_HOME", None)
        else:
            os.environ["AIWATCHER_HOME"] = self._previous
        shutil.rmtree(self._home, ignore_errors=True)

    def _run(self, payload):
        args = argparse.Namespace(text=None)
        with patch.object(cli, "_read_stdin_text", return_value=payload):
            return cli.command_claude_stop_hook(args)

    def test_it_records_the_turn_end(self):
        code = self._run(json.dumps({"session_id": "abc", "cwd": "/repo", "hook_event_name": "Stop"}))
        self.assertEqual(code, 0)
        ends = local_state.session_turn_ends()
        self.assertIn("abc", ends)
        self.assertEqual(ends["abc"]["cwd"], "/repo")

    def test_it_keeps_nothing_but_the_stamp(self):
        self._run(json.dumps({"session_id": "abc", "last_assistant_message": "here is the secret plan", "stop_hook_active": False}))
        self.assertNotIn("secret plan", json.dumps(local_state.session_turn_ends()))

    def test_it_never_fails_the_host(self):
        for payload in ("", "not json", "[]", json.dumps({"no_session": True})):
            with self.subTest(payload=payload):
                self.assertEqual(self._run(payload), 0)

    def test_it_writes_nothing_to_stdout(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self._run(json.dumps({"session_id": "abc"}))
        self.assertEqual(buffer.getvalue(), "")

    def test_the_installer_registers_its_own_event(self):
        merged = cli._merge_claude_stop_hook({}, "aiwatcher")
        self.assertIn("Stop", merged["hooks"])
        self.assertNotIn("UserPromptSubmit", merged["hooks"])
        self.assertIn("claude-stop-hook", json.dumps(merged))

    def test_installing_twice_does_not_duplicate_it(self):
        once = cli._merge_claude_stop_hook({}, "aiwatcher")
        twice = cli._merge_claude_stop_hook(once, "aiwatcher")
        self.assertEqual(len(twice["hooks"]["Stop"]), 1)

    def test_it_leaves_other_hooks_alone_when_removed(self):
        settings = cli._merge_claude_activity_hook({}, "aiwatcher")
        settings = cli._merge_claude_stop_hook(settings, "aiwatcher")
        updated, removed = cli._remove_claude_stop_hook(settings)
        self.assertTrue(removed)
        self.assertNotIn("Stop", updated["hooks"])
        self.assertIn("Notification", updated["hooks"])

    def test_removing_when_absent_is_not_an_error(self):
        _, removed = cli._remove_claude_stop_hook({"hooks": {}})
        self.assertFalse(removed)

    def test_main_routes_the_hook_name(self):
        with patch.object(cli, "_read_stdin_text", return_value=json.dumps({"session_id": "xyz"})):
            self.assertEqual(cli.main(["claude-stop-hook"]), 0)
        self.assertIn("xyz", local_state.session_turn_ends())


if __name__ == "__main__":
    unittest.main()
