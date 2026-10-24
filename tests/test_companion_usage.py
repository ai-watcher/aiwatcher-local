from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import threading
import unittest
from unittest.mock import patch
from urllib import request, error

from aiwatcher_cli import ui
from aiwatcher_cli.scanner import LocalSession


class CompanionUsageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        ui._PRESSURE_TRANSCRIPT_CACHE.clear()
        self.addCleanup(ui._PRESSURE_TRANSCRIPT_CACHE.clear)
        self.sessions = [LocalSession(
            session_id=f"session-{i}", tool="claude-code", model="claude-opus-4-6",
            project_path=f"/repo/project-{i}", source_path=f"/logs/private-{i}.jsonl",
            updated_at=datetime.now(timezone.utc) - timedelta(seconds=20 + i),
            tokens_in=3_000_000, tokens_out=120_000,
            cache_read_tokens=2_000_000, cache_write_tokens=20_000,
            cost_usd=12.34, agent_calls=42, tool_calls=11,
            title="PRIVATE PROMPT TITLE",
        ) for i in range(2)]
        self.stack.enter_context(patch.object(ui, "_cached_session_rows", side_effect=lambda: self.sessions))
        self.stack.enter_context(patch.object(ui, "_freshened_for_presence", side_effect=lambda rows: rows))
        self.stack.enter_context(patch.object(ui, "session_waiting_signals", return_value={}))
        # presence_for_sessions reads the function in its defining module.
        self.stack.enter_context(patch("aiwatcher_cli.session_presence.session_waiting_signals", return_value={}))
        self.stats = self.stack.enter_context(patch.object(ui.statusline, "read_transcript", return_value={
            "available": True, "latest_context": 966_000, "peak_context": 966_000,
            "model": "claude-opus-4-6",
        }))

    def test_selection_reads_only_the_named_session_and_preserves_scopes(self):
        result = ui.build_companion_usage("session-1")
        selected = result["selected"]
        self.stats.assert_called_once_with("/logs/private-1.jsonl")
        self.assertEqual(selected["session_id"], "session-1")
        self.assertEqual(selected["context"]["latest_turn_tokens"], 966_000)
        self.assertEqual(selected["context"]["context_window"], 1_000_000)
        self.assertEqual(selected["input_tokens"], 3_000_000)
        self.assertEqual(selected["output_tokens"], 120_000)
        self.assertIn("included in input", selected["cache_label"])
        self.assertIn("not provider billing", selected["cost_label"])
        self.assertIn("not available", selected["breakdown_note"])
        self.assertNotIn("PRIVATE PROMPT", json.dumps(result))
        self.assertNotIn("private-1.jsonl", json.dumps(result))

    def test_missing_selection_never_falls_back_to_another_session(self):
        result = ui.build_companion_usage("deleted-session")
        self.assertIsNone(result["selected"])
        self.assertIn("no longer available", result["reason"])
        self.stats.assert_not_called()

    def test_no_sessions_has_no_zero_percent_or_transcript_reads(self):
        self.sessions.clear()
        result = ui.build_companion_usage()
        self.assertEqual(result["sessions"], [])
        self.assertIsNone(result["selected"])
        self.stats.assert_not_called()

    def test_unknown_window_keeps_session_totals_without_fabricated_percent(self):
        self.stats.return_value["model"] = "unknown-model"
        self.sessions[0].model = "unknown-model"
        selected = ui.build_companion_usage("session-0")["selected"]
        self.assertFalse(selected["context"]["available"])
        self.assertNotIn("pct_of_turn_limit", selected["context"])
        self.assertEqual(selected["input_tokens"], 3_000_000)

    def test_cumulative_source_does_not_become_context(self):
        self.stack.enter_context(patch.object(ui, "has_cumulative_totals", return_value=True))
        selected = ui.build_companion_usage("session-0")["selected"]
        self.assertFalse(selected["context"]["available"])
        self.assertIn("cumulative", selected["context_label"])
        self.stats.assert_not_called()

    def test_refresh_reuses_the_selected_transcript_until_it_changes(self):
        ui.build_companion_usage("session-1")
        ui.build_companion_usage("session-1")
        self.assertEqual(self.stats.call_count, 1)
        self.sessions[1].updated_at += timedelta(seconds=1)
        ui.build_companion_usage("session-1")
        self.assertEqual(self.stats.call_count, 2)

    def test_picker_is_bounded_but_explicit_selection_remains_visible(self):
        original = self.sessions[0]
        from dataclasses import replace
        self.sessions[:] = [replace(original, session_id=f"s{i}") for i in range(35)]
        result = ui.build_companion_usage("s34")
        self.assertEqual(len(result["sessions"]), 20)
        self.assertEqual(result["total_recent_sessions"], 35)
        self.assertIn("s34", [row["session_id"] for row in result["sessions"]])
        self.assertEqual(result["selected"]["session_id"], "s34")
        self.assertEqual(self.stats.call_count, 1)

    def test_gone_selection_is_labelled_and_not_silently_replaced(self):
        self.sessions[1].updated_at -= timedelta(days=2)
        selected = ui.build_companion_usage("session-1")["selected"]
        self.assertEqual(selected["session_id"], "session-1")
        self.assertEqual(selected["state"], "gone")

    def test_recent_unmeasurable_session_is_visible_with_its_reason(self):
        self.sessions[1].source_path = None
        result = ui.build_companion_usage("session-1")
        self.assertIn("session-1", [row["session_id"] for row in result["sessions"]])
        self.assertFalse(result["selected"]["context"]["available"])
        self.assertNotEqual(result["selected"]["context_label"], "no working session right now")
        self.stats.assert_not_called()

    def test_fresh_activity_does_not_relabel_old_indexed_totals_as_fresh(self):
        from dataclasses import replace
        original_stamp = self.sessions[1].updated_at
        self.stack.enter_context(patch.object(ui, "_freshened_for_presence", side_effect=lambda rows: [
            replace(row, updated_at=row.updated_at + timedelta(seconds=10)) for row in rows
        ]))
        selected = ui.build_companion_usage("session-1")["selected"]
        self.assertEqual(selected["totals_updated_at"], original_stamp.isoformat())
        self.assertNotEqual(selected["updated_at"], selected["totals_updated_at"])

    def test_missing_token_counters_are_not_presented_as_zero_usage(self):
        self.sessions[0].tokens_in = self.sessions[0].tokens_out = 0
        selected = ui.build_companion_usage("session-0")["selected"]
        self.assertIn("not recorded", selected["usage_label"])
        self.assertIn("unavailable", selected["input_label"])

    def test_http_route_selects_session_and_rejects_foreign_origins(self):
        server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.UIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/companion-usage?session_id=session-1"
        try:
            with request.urlopen(base, timeout=5) as response:
                self.assertEqual(json.load(response)["selected"]["session_id"], "session-1")
            with self.assertRaises(error.HTTPError) as caught:
                request.urlopen(request.Request(base, headers={"Origin": "http://localhost:3000"}), timeout=5)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
