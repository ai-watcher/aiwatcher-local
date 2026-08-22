from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib import error, request
from unittest.mock import Mock, patch

from aiwatcher_cli import ui
from aiwatcher_cli.local_state import (
    link_handoff_decision_next_session,
    recent_handoff_decisions,
    record_command_decision,
    record_evidence_snapshot,
    record_handoff_decision,
    record_intervention,
    record_outcome,
)
from aiwatcher_cli.scanner import LocalEvent, LocalSession, SurfaceCoverage


class DashboardServeTests(unittest.TestCase):
    def _serve_one(self):
        server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.UIHandler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        return server, thread, f"http://127.0.0.1:{server.server_address[1]}"

    def test_runtime_return_requires_post(self) -> None:
        server, thread, base = self._serve_one()
        with patch.object(ui, "build_runtime_return", return_value={"ok": True}) as build_runtime_return:
            try:
                with self.assertRaises(error.HTTPError) as raised:
                    request.urlopen(f"{base}/api/runtime-return?id=sess-1", timeout=5)
                self.assertEqual(raised.exception.code, 404)
                build_runtime_return.assert_not_called()
            finally:
                thread.join(timeout=5)
                server.server_close()

        server, thread, base = self._serve_one()
        payload = json.dumps({"session_id": "sess-1"}).encode("utf-8")
        http_request = request.Request(
            f"{base}/api/runtime-return",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch.object(ui, "build_runtime_return", return_value={"ok": True}) as build_runtime_return:
            try:
                with request.urlopen(http_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                build_runtime_return.assert_called_once_with("sess-1", 30)
            finally:
                thread.join(timeout=5)
                server.server_close()

    def test_serve_records_the_actually_bound_port(self) -> None:
        """Issue #31 (S-32): `watch --notify`'s dashboard deep link has to
        know where the dashboard actually landed after auto-port fallback,
        not just assume the requested default -- regression found by
        manually testing this feature against a real fallback port."""
        with (
            patch.object(ui, "find_available_port", return_value=8799),
            patch.object(ui, "ThreadingHTTPServer") as server_cls,
            patch.object(ui, "record_ui_server") as record_mock,
        ):
            server_cls.return_value.serve_forever.side_effect = KeyboardInterrupt
            ui.serve(host="127.0.0.1", port=8765, auto_port=True)

        record_mock.assert_called_once_with("127.0.0.1", 8799)

    def test_companion_skip_and_receipt_view_posts_are_routable(self) -> None:
        server, thread, base = self._serve_one()
        payload = json.dumps({"state": "proof_pending"}).encode("utf-8")
        http_request = request.Request(
            f"{base}/api/companion-skip",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with (
            patch.object(ui, "mark_recent_handoff_receipts_viewed", return_value=1),
            patch.object(ui, "record_companion_skip", return_value={}),
        ):
            try:
                with request.urlopen(http_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
            finally:
                thread.join(timeout=5)
                server.server_close()

    def test_companion_group_snooze_records_project_cooldowns(self) -> None:
        server, thread, base = self._serve_one()
        payload = json.dumps({
            "state": "control_recommended_group",
            "projects": ["/repo/app", "/repo/api"],
        }).encode("utf-8")
        http_request = request.Request(
            f"{base}/api/companion-skip",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch.object(ui, "record_companion_skip", return_value={}) as record_skip:
            try:
                with request.urlopen(http_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    body = json.loads(response.read().decode("utf-8"))
            finally:
                thread.join(timeout=5)
                server.server_close()

        self.assertEqual(body["projects"], 2)
        keys = [call.kwargs["key"] for call in record_skip.call_args_list]
        self.assertEqual(keys, [
            "control_recommended_project:/repo/app",
            "control_recommended_project:/repo/api",
        ])

        server, thread, base = self._serve_one()
        http_request = request.Request(
            f"{base}/api/handoff-receipts-viewed",
            data=b"",
            method="POST",
        )
        with patch.object(ui, "mark_recent_handoff_receipts_viewed", return_value=1):
            try:
                with request.urlopen(http_request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
            finally:
                thread.join(timeout=5)
                server.server_close()


class DashboardWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_dir.cleanup)
        env = patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(self._state_dir.name, "state.json")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._wait_for_summary_refresh)

    @staticmethod
    def _wait_for_summary_refresh() -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with ui._SUMMARY_CACHE_LOCK:
                if not ui._SUMMARY_REFRESHING:
                    return
            time.sleep(0.01)

    def test_dashboard_script_is_valid_javascript(self) -> None:
        # A single unquoted/malformed token anywhere in this one large inline
        # <script> block breaks the entire dashboard silently -- every tab
        # shows blank stats, no console error is obvious, and nothing in a
        # content-only assertIn() check would catch it (this exact class of
        # bug shipped once already: the unquoted `last-prompt` object key).
        # Only actually parsing the script catches this.
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available to check JS syntax")
        script = re.search(r"<script>(.*?)</script>", ui.HTML, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            script_path = handle.name
        try:
            completed = subprocess.run([node, "-c", script_path], capture_output=True, text=True)
        finally:
            os.unlink(script_path)
        self.assertEqual(completed.returncode, 0, f"Dashboard's inline JS has a syntax error:\n{completed.stderr}")

    def test_dashboard_uses_focused_drawer_and_inline_feedback(self) -> None:
        self.assertIn('id="detailDrawer"', ui.HTML)
        self.assertIn('data-view="prompt"', ui.HTML)
        self.assertIn('data-view="receipts"', ui.HTML)
        self.assertIn('data-view="coverage"', ui.HTML)
        self.assertIn('data-view="setup"', ui.HTML)
        self.assertIn('id="latestIntervention"', ui.HTML)
        self.assertIn('id="actionQueue"', ui.HTML)
        self.assertIn("Needs action", ui.HTML)
        self.assertIn("buildActionQueue", ui.HTML)
        self.assertIn("Every row says what AIWatcher knows", ui.HTML)
        self.assertIn('id="contextHealth"', ui.HTML)
        self.assertIn('id="handoffBubble"', ui.HTML)
        self.assertIn('id="latestHandoffDecision"', ui.HTML)
        self.assertIn('id="handoffDecisionRows"', ui.HTML)
        self.assertIn('id="coverageRows"', ui.HTML)
        self.assertIn('id="setupRows"', ui.HTML)
        self.assertIn('id="promptInput"', ui.HTML)
        self.assertIn("const requestedView = new URLSearchParams(location.search).get('view')", ui.HTML)
        self.assertIn("showView(requestedView)", ui.HTML)
        self.assertIn("document.getElementById('promptInput').focus()", ui.HTML)
        self.assertIn('class="outcome-button useful', ui.HTML)
        self.assertIn('class="outcome-button rework', ui.HTML)
        self.assertIn('class="outcome-button abandoned', ui.HTML)
        self.assertIn("showToast(`Outcome saved:", ui.HTML)
        self.assertIn("Outcome evidence", ui.HTML)
        self.assertIn('recommended-action action-composer', ui.HTML)
        self.assertIn('recommended-action loading-action', ui.HTML)
        self.assertIn('ai-loading-panel', ui.HTML)
        self.assertIn("Recommended: continue in a fresh session", ui.HTML)
        self.assertIn("Build Fresh Start brief", ui.HTML)
        self.assertIn("Fresh Start", ui.HTML)
        self.assertIn("renderIdentityStrip", ui.HTML)
        self.assertIn("identity_label", ui.HTML)
        self.assertIn("Copy it into a fresh chat only after the identity below matches", ui.HTML)
        self.assertIn("copyFreshStartFromDrawer", ui.HTML)
        self.assertIn("const canOpenRuntime = !!runtime.available", ui.HTML)
        self.assertNotIn("runtime.available && runtime.level !== 'app'", ui.HTML)
        self.assertIn("Fresh Start receipt saved", ui.HTML)
        self.assertIn("Loading session details for", ui.HTML)
        self.assertIn("/api/session-summary", ui.HTML)
        self.assertIn("/api/handoff-basic", ui.HTML)
        self.assertIn("/api/handoff-demo", ui.HTML)
        self.assertIn("openDemoHandoff", ui.HTML)
        self.assertIn("Try Fresh Start demo", ui.HTML)
        self.assertIn('id="handoffType"', ui.HTML)
        self.assertIn('id="handoffObjective"', ui.HTML)
        self.assertIn('id="handoffSources"', ui.HTML)
        self.assertIn('id="handoffConstraints"', ui.HTML)
        self.assertIn('id="handoffAcceptance"', ui.HTML)
        self.assertIn("Regenerate brief", ui.HTML)
        self.assertIn("handoffPayload", ui.HTML)
        self.assertIn("postJson('/api/handoff'", ui.HTML)
        self.assertIn("if (!isDemo)", ui.HTML)
        self.assertIn("Proof pending means the brief was copied", ui.HTML)
        self.assertIn("Protection:", ui.HTML)
        self.assertIn("companion/history-only until proven otherwise", ui.HTML)
        self.assertIn("renderSessionSummary", ui.HTML)
        self.assertIn("Loading session identity for", ui.HTML)
        self.assertIn("Building Fresh Start brief", ui.HTML)
        self.assertIn("Still indexing this session. Retrying", ui.HTML)
        self.assertIn("returnToRuntime", ui.HTML)
        self.assertIn("requestRuntimeReturn", ui.HTML)
        self.assertIn("/api/runtime-return", ui.HTML)
        self.assertIn("method: 'POST'", ui.HTML)
        self.assertIn("JSON.stringify({session_id: sessionId})", ui.HTML)
        self.assertIn("primaryId === 'handoff' ? 'btn-primary' : 'btn-quiet'", ui.HTML)
        self.assertIn("onclick=\"openHandoff", ui.HTML)
        self.assertIn("watch --notify", ui.HTML)
        self.assertIn("/api/handoff", ui.HTML)
        self.assertIn("/api/handoff-decision", ui.HTML)
        self.assertIn("/api/handoff-receipts-viewed", ui.HTML)
        self.assertIn("/api/companion-skip", ui.HTML)
        self.assertIn("markFreshStartReceiptsViewed", ui.HTML)
        self.assertIn("quietFreshStartReminders", ui.HTML)
        self.assertIn("handoffDecisionBubble", ui.HTML)
        self.assertIn("Fresh Start brief copied from the session review.", ui.HTML)
        self.assertIn("renderHandoffCopied", ui.HTML)
        self.assertIn("Fresh Start ready", ui.HTML)
        self.assertIn("Fresh Start receipt saved", ui.HTML)
        self.assertIn("Snooze all 48h", ui.HTML)
        self.assertIn("continueFreshStartProject", ui.HTML)
        self.assertNotIn("Copy handoff", ui.HTML)
        self.assertIn("Include prompt excerpt", ui.HTML)
        self.assertIn("Evidence captured", ui.HTML)
        self.assertNotIn("window.alert", ui.HTML)

    def test_companion_state_surfaces_control_recommendation(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
            "handoff_bubble": {
                "session_id": "sess-1",
                "tool": "codex-cli",
                "severity": "critical",
                "body": "Context is getting expensive.",
                "runtime_attachment": {"surface": "cli"},
            },
            "intervention_receipts": [],
            "handoff_decisions": [],
            "insights": [],
            "watcher": {"running": True},
            }),
            patch.object(ui, "companion_skip_active", return_value=False),
            patch.object(ui, "foreground_tool", return_value="terminal"),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "control_recommended")
        self.assertEqual(state["primary_label"], "Fresh Start")
        self.assertEqual(state["primary_action"], "copy_fresh_start")
        self.assertEqual(state["primary_session_id"], "sess-1")
        self.assertEqual(state["primary_url"], "/?session=sess-1")
        self.assertEqual(state["continue_label"], "Continue")
        self.assertEqual(state["continue_session_id"], "sess-1")
        self.assertEqual(state["skip_label"], "Skip")
        self.assertEqual(state["skip_state"], "control_recommended")
        self.assertEqual(state["skip_session_id"], "sess-1")
        self.assertEqual(state["plan_url"], "/?view=prompt")

    def test_companion_state_does_not_blink_for_fresh_start_when_ai_tool_is_not_foreground(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "context_health": [{
                    "session_id": "sess-1",
                    "project_full": "/repo/app",
                    "severity": "critical",
                    "can_handoff": True,
                }],
                "handoff_bubble": {
                    "session_id": "sess-1",
                    "project_full": "/repo/app",
                    "tool": "codex-cli",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                    "runtime_attachment": {"surface": "cli"},
                },
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "companion_skip_active", return_value=False),
            patch.object(ui, "foreground_tool", return_value="chrome"),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["label"], "Watching quietly")
        self.assertEqual(state["primary_label"], "Console")
        self.assertIn("Console", state["subtitle"])

    def test_companion_state_groups_multiple_fresh_start_projects(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "context_health": [
                    {
                        "session_id": "sess-1",
                        "project": "app",
                        "project_full": "/repo/app",
                        "tool": "codex-cli",
                        "severity": "critical",
                        "can_handoff": True,
                        "estimated_replayed_context_tokens": 200_000,
                        "runtime_attachment": {"surface": "cli"},
                    },
                    {
                        "session_id": "sess-2",
                        "project": "api",
                        "project_full": "/repo/api",
                        "tool": "codex-cli",
                        "severity": "warning",
                        "can_handoff": True,
                        "estimated_replayed_context_tokens": 80_000,
                        "runtime_attachment": {"surface": "cli"},
                    },
                ],
                "handoff_bubble": {
                    "session_id": "sess-1",
                    "project_full": "/repo/app",
                    "tool": "codex-cli",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                    "runtime_attachment": {"surface": "cli"},
                },
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "companion_skip_active", return_value=False),
            patch.object(ui, "foreground_tool", return_value="terminal"),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "control_review")
        self.assertEqual(state["label"], "Review context")
        self.assertEqual(state["primary_label"], "Review")
        self.assertEqual(state["primary_url"], "/?view=sessions#contextHealth")
        self.assertEqual(state["skip_state"], "control_recommended_group")
        self.assertEqual(state["fresh_start_project_count"], 2)
        self.assertIn("/repo/app", state["skip_project"])
        self.assertIn("/repo/api", state["skip_project"])

    def test_handoff_bubble_skips_project_during_fresh_start_cooldown(self) -> None:
        rows = [
            {
                "session_id": "skipped",
                "project": "skipped",
                "project_full": "/repo/skipped",
                "tool": "codex-cli",
                "severity": "critical",
                "can_handoff": True,
                "estimated_replayed_context_label": "200.0k",
            },
            {
                "session_id": "ready",
                "project": "ready",
                "project_full": "/repo/ready",
                "tool": "codex-cli",
                "severity": "warning",
                "can_handoff": True,
                "estimated_replayed_context_label": "80.0k",
            },
        ]
        with patch.object(
            ui,
            "companion_skip_active",
            side_effect=lambda key: key == "control_recommended_project:/repo/skipped",
        ):
            bubble = ui._handoff_bubble(rows)

        self.assertIsNotNone(bubble)
        self.assertEqual(bubble["session_id"], "ready")

    def test_companion_allows_app_level_fresh_start_return(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-1",
                    "tool": "claude-code",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                    "runtime_attachment": {
                        "available": True,
                        "level": "app",
                        "action_label": "Open Claude",
                        "surface": "desktop",
                    },
                },
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "companion_skip_active", return_value=False),
            patch.object(ui, "foreground_tool", return_value="claude"),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "control_recommended")
        self.assertTrue(state["primary_runtime_available"])

    def test_companion_state_surfaces_prompt_workflow_label(self) -> None:
        with (
            patch.object(ui, "active_prompt_gate", return_value={
                "tool": "codex",
                "risk": "medium",
                "score": 5,
                "url": "http://127.0.0.1:5555/",
                "workflow_label": "Fork this task",
                "workflow_reward": "Likely reward: isolates exploratory context.",
            }),
            patch.object(ui, "build_summary_cached", return_value={
                "totals": {"sessions": 1, "api_value_label": "$0.10", "tokens_label": "1.0k"},
                "handoff_bubble": None,
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [],
                "watcher": {"running": True},
            }),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "prompt_gate")
        self.assertEqual(state["label"], "Fork this task")
        self.assertEqual(state["subtitle"], "Likely reward: isolates exploratory context.")
        self.assertEqual(state["primary_label"], "Review Gate")

    def test_companion_state_does_not_blink_for_generic_insights(self) -> None:
        with (
            patch.object(ui, "active_prompt_gate", return_value=None),
            patch.object(ui, "build_summary_cached", return_value={
                "totals": {"sessions": 2, "api_value_label": "$0.50", "tokens_label": "12.0k"},
                "handoff_bubble": None,
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [{"title": "Top project", "body": "Review this later."}],
                "watcher": {"running": True},
            }),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["label"], "Watching quietly")
        self.assertEqual(state["primary_label"], "Console")

    def test_companion_state_stays_quiet_when_watching(self) -> None:
        with (
            patch.object(ui, "active_prompt_gate", return_value=None),
            patch.object(ui, "build_summary_cached", return_value={
                "totals": {
                    "window_label": "Last 7 days",
                    "sessions": 3,
                    "api_value_label": "$1.25",
                    "tokens_label": "42.0k",
                },
                "handoff_bubble": None,
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [],
                "watcher": {"running": True},
            }),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["label"], "Watching quietly")
        self.assertEqual(state["primary_label"], "Console")
        self.assertEqual(state["subtitle"], "7 days: 3 sessions · $1.25 · 42.0k tokens")

    def test_companion_state_surfaces_active_prompt_gate(self) -> None:
        with (
            patch.object(ui, "active_prompt_gate", return_value={
                "id": "gate-1",
                "tool": "claude",
                "risk": "high",
                "score": 8,
                "url": "http://127.0.0.1:9999/",
            }),
            patch.object(ui, "mark_active_prompt_gate_seen") as mark_seen,
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-1",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                },
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [],
                "watcher": {"running": True},
            }),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "prompt_gate")
        self.assertEqual(state["label"], "Prompt Gate")
        self.assertEqual(state["primary_label"], "Review Gate")
        self.assertEqual(state["primary_url"], "http://127.0.0.1:9999/")
        self.assertEqual(state["continue_label"], "Continue")
        self.assertEqual(state["continue_action"], "run_original_prompt")
        self.assertEqual(state["continue_url"], "http://127.0.0.1:9999/")
        mark_seen.assert_called_once_with("gate-1")

    def test_companion_state_surfaces_fresh_start_proof_as_passive_status(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
            "handoff_bubble": None,
            "intervention_receipts": [],
            "handoff_decisions": [{
                "created_at": datetime.now(timezone.utc).isoformat(),
                "proof_status": "Proof pending",
                "proof_reason": "No later same-project local session has been observed yet.",
            }],
            "insights": [],
            "watcher": {"running": True},
            }),
            patch.object(ui, "companion_skip_active", return_value=False),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["label"], "Watching proof")
        self.assertEqual(state["primary_label"], "Console")
        self.assertEqual(state["primary_url"], "/?view=receipts")

    def test_companion_state_does_not_stay_stuck_on_stale_proof_pending(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": None,
                "intervention_receipts": [],
                "handoff_decisions": [{
                    "created_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                    "proof_status": "Proof pending",
                    "proof_reason": "No later same-project local session has been observed yet.",
                }],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "companion_skip_active", return_value=False),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["label"], "Watching quietly")

    def test_companion_state_quiets_after_proof_pending_receipt_is_viewed(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": None,
                "intervention_receipts": [],
                "handoff_decisions": [{
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "proof_status": "Proof pending",
                    "proof_reason": "No later same-project local session has been observed yet.",
                    "receipt_viewed_at": datetime.now(timezone.utc).isoformat(),
                }],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "companion_skip_active", return_value=False),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["primary_label"], "Console")

    def test_companion_state_prioritizes_live_control_over_pending_receipt(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-live",
                    "tool": "codex-cli",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                    "runtime_attachment": {"surface": "cli"},
                },
                "intervention_receipts": [],
                "handoff_decisions": [{
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "proof_status": "Proof pending",
                    "proof_reason": "No later same-project local session has been observed yet.",
                }],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "companion_skip_active", return_value=False),
            patch.object(ui, "foreground_tool", return_value="terminal"),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "control_recommended")
        self.assertEqual(state["primary_url"], "/?session=sess-live")

    def test_companion_state_quiets_after_continue_here(self) -> None:
        decision = {
            "session_id": "sess-live",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "decision": "continue_here",
        }
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-live",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                },
                "intervention_receipts": [],
                "handoff_decisions": [decision],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "recent_handoff_decisions", return_value=[]),
            patch.object(ui, "companion_skip_active", return_value=False),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["label"], "Watching quietly")
        self.assertEqual(state["primary_label"], "Console")

    def test_companion_state_quiets_from_direct_decision_before_summary_refresh(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-live",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                },
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "recent_handoff_decisions", return_value=[{
                "session_id": "sess-live",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "decision": "continue_here",
            }]),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["subtitle"], "Fresh Start snoozed for this project")

    def test_companion_state_moves_to_proof_pending_after_fresh_start_copy(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-live",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                },
                "intervention_receipts": [],
                "handoff_decisions": [{
                    "session_id": "sess-live",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "decision": "copy_handoff",
                }],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "recent_handoff_decisions", return_value=[]),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["label"], "Watching proof")
        self.assertEqual(state["primary_label"], "Console")

    def test_companion_state_quiets_viewed_fresh_start_copy_even_with_live_bubble(self) -> None:
        viewed_at = datetime.now(timezone.utc).isoformat()
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-live",
                    "severity": "critical",
                    "body": "Context is getting expensive.",
                },
                "intervention_receipts": [],
                "handoff_decisions": [],
                "insights": [],
                "watcher": {"running": True},
            }),
            patch.object(ui, "recent_handoff_decisions", return_value=[{
                "session_id": "sess-live",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "decision": "copy_handoff",
                "receipt_viewed_at": viewed_at,
            }]),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "watching")
        self.assertEqual(state["subtitle"], "Fresh Start receipt reviewed; proof still pending.")

    def test_companion_state_surfaces_optimize_soft_nudge(self) -> None:
        with (
            patch.object(ui, "active_prompt_gate", return_value=None),
            patch.object(ui, "companion_skip_active", return_value=False),
            patch.object(ui, "build_summary_cached", return_value={
                "totals": {"sessions": 4, "api_value_label": "$1.20", "tokens_label": "1.4M"},
                "handoff_bubble": None,
                "handoff_decisions": [],
                "optimize": {
                    "status": "needs_action",
                    "summary": "1 cleanup opportunity found.",
                    "impact_label": "~1.4M context at risk",
                    "top": {"project_full": "/repo/app"},
                    "candidates": [],
                },
                "intervention_receipts": [],
                "insights": [],
                "watcher": {"running": True},
            }),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["state"], "optimize_available")
        self.assertEqual(state["label"], "Optimize")
        self.assertEqual(state["primary_url"], "/?view=prompt#optimizeWorkspace")
        self.assertEqual(state["skip_state"], "optimize_available")

    def test_companion_state_exposes_ask_deep_link(self) -> None:
        with (
            patch.object(ui, "active_prompt_gate", return_value=None),
            patch.object(ui, "build_summary_cached", return_value={
                "totals": {"window_label": "Last 7 days", "sessions": 1, "api_value_label": "$0.00", "tokens_label": "12.0k"},
                "handoff_bubble": None,
                "handoff_decisions": [],
                "optimize": {"status": "quiet"},
                "watcher": {"running": True},
            }),
        ):
            state = ui.build_companion_state()

        self.assertEqual(state["ask_url"], "/?ask=1")
        self.assertIn("Ask AIWatcher", ui.HTML)
        self.assertIn("/api/ask-aiwatcher", ui.HTML)

    def test_ask_aiwatcher_answers_archive_question_from_local_evidence(self) -> None:
        with patch.object(ui, "build_summary_cached", return_value={
            "optimize": {
                "top": {
                    "project_full": "/repo/app",
                    "impact_label": "~1.4M context at risk",
                    "summary": "4 inactive same-project sessions are carrying old context.",
                    "session_count": 4,
                },
                "candidates": [],
            },
        }):
            answer = ui.answer_local_question("Can I archive this chat?")

        self.assertIn("archive candidate", answer["answer"])
        self.assertTrue(any("Do not" in bullet or "cannot archive" in bullet for bullet in answer["bullets"]))
        self.assertEqual(answer["actions"][0]["url"], "/?view=prompt#optimizeWorkspace")

    def test_ask_aiwatcher_answers_context_health_from_local_evidence(self) -> None:
        with patch.object(ui, "build_summary_cached", return_value={
            "context_health": [{
                "session_id": "sess-1",
                "project": "/repo/app",
                "tool": "codex-cli",
                "severity": "critical",
                "latest_turn_tokens": "203.0k",
                "recommendation": "Build a Fresh Start brief before continuing.",
                "can_handoff": True,
            }],
        }):
            answer = ui.answer_local_question("What is my context health?")

        self.assertIn("needs attention", answer["answer"])
        self.assertIn("203.0k", " ".join(answer["bullets"]))
        self.assertEqual(answer["actions"][0]["label"], "Build Fresh Start")

    def test_fresh_start_receipt_rows_show_observed_next_session_proof(self) -> None:
        now = datetime.now(timezone.utc)
        source = LocalSession(
            session_id="source",
            tool="claude-code",
            project_path="/repo/app",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=1),
            tokens_in=180_000,
            tokens_out=12_000,
            cost_usd=1.25,
            agent_calls=10,
            tool_calls=6,
        )
        next_session = LocalSession(
            session_id="next",
            tool="codex-cli",
            project_path="/repo/app",
            started_at=now + timedelta(minutes=5),
            updated_at=now + timedelta(minutes=12),
            tokens_in=14_000,
            tokens_out=2_000,
            cost_usd=0.08,
            agent_calls=4,
            tool_calls=2,
        )
        record = record_handoff_decision(
            session_id="source",
            decision="new_chat",
            reason="Context pressure.",
            expected_saved_context_tokens=166_000,
        )
        link_handoff_decision_next_session(
            record["id"],
            next_session_id="next",
            correlation={"status": "linked", "confidence": "high", "reason": "same project after action"},
        )
        record_outcome("next", "useful")
        record_evidence_snapshot(
            "next",
            {
                "inferred_outcome": "useful",
                "confidence": "observed",
                "commits": [{"sha": "abc123"}],
                "tests": [{"artifact": "pytest"}],
            },
        )

        rows = ui._handoff_decision_rows(limit=5, sessions=[source, next_session])

        self.assertEqual(rows[0]["proof_status"], "Follow-up observed")
        self.assertEqual(rows[0]["source_session_id"], "source")
        self.assertEqual(rows[0]["next_session_id"], "next")
        self.assertEqual(rows[0]["next_usage"]["tokens_label"], "16.0k")
        self.assertEqual(rows[0]["source_usage"]["api_value_label"], "$1.25")
        self.assertEqual(rows[0]["next_usage"]["cost_per_model_call_label"], "$0.02")
        self.assertEqual(rows[0]["outcome"], "useful")
        self.assertEqual(rows[0]["inferred_outcome"], "useful")
        self.assertEqual(rows[0]["proof_evidence"]["label"], "observed")
        self.assertEqual(rows[0]["proof_evidence"]["commits"], 1)
        self.assertEqual(rows[0]["proof_evidence"]["tests"], 1)
        self.assertEqual(rows[0]["observed_followup"]["source_tokens_label"], "192.0k")
        self.assertEqual(rows[0]["observed_followup"]["next_tokens_label"], "16.0k")
        self.assertEqual(rows[0]["observed_followup"]["source_api_value_label"], "$1.25")
        self.assertEqual(rows[0]["observed_followup"]["next_api_value_label"], "$0.08")
        self.assertEqual(rows[0]["observed_followup"]["next_tokens_per_model_call_label"], "4.0k")
        self.assertEqual(rows[0]["observed_followup"]["direction"], "smaller")
        self.assertIn("not a final saved-token", rows[0]["observed_followup"]["basis"])

    def test_optimize_inventory_detects_stale_session_cluster(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id=f"sess-{index}",
                tool="codex-cli",
                project_path="/repo/app",
                started_at=now - timedelta(hours=9 + index),
                updated_at=now - timedelta(hours=8 + index),
                tokens_in=240_000,
                tokens_out=20_000,
                cost_usd=0.15,
                agent_calls=55,
                tool_calls=20,
            )
            for index in range(3)
        ]
        outcomes = {"sess-0": {"outcome": "useful"}}
        with (
            patch.object(ui, "safe_runtime_processes", return_value=[]),
            patch.object(ui, "_worktree_rows", return_value=[]),
            patch.object(ui, "recent_optimize_decisions", return_value=[]),
        ):
            inventory = ui.build_optimize_inventory(rows, outcomes=outcomes, handoff_decisions=[])

        self.assertEqual(inventory["status"], "needs_action")
        self.assertEqual(inventory["top"]["kind"], "session_cluster")
        self.assertEqual(inventory["top"]["tokens_at_risk"], 780_000)
        self.assertIn("why_inactive", inventory["top"])
        self.assertIn("Copy", inventory["top"]["action_label"])
        self.assertIn("Do not delete", inventory["top"]["checklist"])
        self.assertIn("/repo/app", inventory["top"]["checklist"])

    def test_detected_tools_are_listed_without_measured_spend(self) -> None:
        rows = [{
            "name": "claude-code (desktop)",
            "id": "claude-code (desktop)",
            "short_name": "claude-code (desktop)",
            "sessions": 2,
            "tokens": 1200,
            "tokens_label": "1.2k",
            "api_value_usd": 1.5,
            "api_value_label": "$1.50",
            "calls": 3,
            "tool_calls": 1,
        }]
        tools = ui._append_detected_tool_rows(rows, {"cursor": True, "ollama": True})

        labels = {row["name"]: row for row in tools}
        self.assertTrue(labels["Cursor"]["detected_only"])
        self.assertEqual(labels["Cursor"]["status_label"], "Detected, not measured")
        self.assertTrue(labels["Ollama"]["detected_only"])
        self.assertEqual(labels["Ollama"]["api_value_label"], "$0.00")

    def test_optimize_inventory_suppresses_recently_reviewed_project(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id=f"sess-{index}",
                tool="codex-cli",
                project_path="/repo/app",
                started_at=now - timedelta(hours=9 + index),
                updated_at=now - timedelta(hours=8 + index),
                tokens_in=240_000,
                tokens_out=20_000,
                agent_calls=55,
            )
            for index in range(3)
        ]
        with (
            patch.object(ui, "safe_runtime_processes", return_value=[]),
            patch.object(ui, "_worktree_rows", return_value=[]),
            patch.object(ui, "recent_optimize_decisions", return_value=[{
                "decision": "marked_done",
                "project_path": "/repo/app",
                "created_at": now.isoformat(),
            }]),
        ):
            inventory = ui.build_optimize_inventory(rows, outcomes={}, handoff_decisions=[])

        self.assertEqual(inventory["status"], "quiet")
        self.assertEqual(inventory["candidates"], [])

    def test_fresh_start_receipt_rows_wait_before_follow_up_is_observed(self) -> None:
        record_handoff_decision(
            session_id="source",
            decision="copy_handoff",
            reason="Context pressure.",
            expected_saved_context_tokens=42_000,
        )

        rows = ui._handoff_decision_rows(limit=5, sessions=[
            LocalSession(session_id="source", tool="claude-code", project_path="/repo/app")
        ])

        self.assertEqual(rows[0]["proof_status"], "Proof pending")
        self.assertIn("will not claim saved tokens", rows[0]["proof_reason"])
        self.assertIsNone(rows[0]["next_usage"])
        self.assertIsNone(rows[0]["outcome"])

    def test_overlay_page_is_a_local_handoff_companion(self) -> None:
        self.assertIn("AIWatcher Fresh Start", ui.OVERLAY_HTML)
        self.assertIn("/api/summary?days=7", ui.OVERLAY_HTML)
        self.assertIn("/api/handoff-decision", ui.OVERLAY_HTML)
        self.assertIn("/api/ambient-intervention-action", ui.OVERLAY_HTML)
        self.assertIn("/api/ambient-intervention?id=", ui.OVERLAY_HTML)
        self.assertIn("Copy Fresh Start brief", ui.OVERLAY_HTML)
        self.assertIn("Copy focused next step", ui.OVERLAY_HTML)
        self.assertIn("Inspect and stop", ui.OVERLAY_HTML)
        self.assertIn("Loading this session evidence", ui.OVERLAY_HTML)
        self.assertIn("Local session", ui.OVERLAY_HTML)
        self.assertIn("Last activity:", ui.OVERLAY_HTML)
        self.assertIn("Snooze 15 min", ui.OVERLAY_HTML)
        self.assertIn("Dismiss", ui.OVERLAY_HTML)
        self.assertIn("Prompt/source content is not stored", ui.OVERLAY_HTML)

    def test_overlay_script_is_valid_javascript(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available to check JS syntax")
        script = re.search(r"<script>(.*?)</script>", ui.OVERLAY_HTML, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            script_path = handle.name
        try:
            completed = subprocess.run([node, "-c", script_path], capture_output=True, text=True)
        finally:
            os.unlink(script_path)
        self.assertEqual(completed.returncode, 0, f"Overlay inline JS has a syntax error:\n{completed.stderr}")

    def test_prompt_plan_routes_broad_codex_work_to_fork(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={"handoff_bubble": None}),
            patch("aiwatcher_cli.cli.sessions_since", return_value=[]),
        ):
            result = ui.build_prompt_preflight(
                "Refactor every screen in the app and update all routes after reviewing the current architecture",
                tool="codex",
                cwd="/repo",
            )

        self.assertEqual(result["plan_action"]["kind"], "fork")
        self.assertIn("Fork", result["plan_action"]["label"])

    def test_prompt_plan_routes_destructive_work_to_prompt_change(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={"handoff_bubble": None}),
            patch("aiwatcher_cli.cli.sessions_since", return_value=[]),
        ):
            result = ui.build_prompt_preflight(
                "Delete the repo, reset all history, and force push over origin/main",
                tool="claude",
                cwd="/repo",
            )

        self.assertEqual(result["plan_action"]["kind"], "prompt_change")
        self.assertEqual(result["plan_action"]["primary_label"], "Copy safer brief")

    def test_prompt_plan_destructive_work_wins_over_ambient_fresh_start(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-1",
                    "body": "Context is getting expensive.",
                }
            }),
            patch("aiwatcher_cli.cli.sessions_since", return_value=[]),
        ):
            result = ui.build_prompt_preflight(
                "AIWatcher Fresh Start brief\n\ndelete the repo",
                tool="codex",
                cwd="/repo",
            )

        self.assertEqual(result["risk"], "high")
        self.assertEqual(result["plan_action"]["kind"], "prompt_change")
        self.assertEqual(result["plan_action"]["primary_label"], "Copy safer brief")

    def test_prompt_plan_routes_context_pressure_to_fresh_start(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-1",
                    "body": "Context is getting expensive.",
                }
            }),
            patch("aiwatcher_cli.cli.sessions_since", return_value=[]),
        ):
            result = ui.build_prompt_preflight(
                "Continue implementing the next phase",
                tool="claude",
                cwd="/repo",
            )

        self.assertEqual(result["plan_action"]["kind"], "fresh_start")
        self.assertEqual(result["plan_action"]["primary_url"], "/?session=sess-1")

    def test_prompt_plan_does_not_reopen_fresh_start_for_existing_fresh_start_prompt(self) -> None:
        with (
            patch.object(ui, "build_summary_cached", return_value={
                "handoff_bubble": {
                    "session_id": "sess-1",
                    "body": "Context is getting expensive.",
                }
            }),
            patch("aiwatcher_cli.cli.sessions_since", return_value=[]),
        ):
            result = ui.build_prompt_preflight(
                "AIWatcher Fresh Start brief\n\nGoal\n- Continue from local evidence.\n",
                tool="codex",
                cwd="/repo",
            )

        self.assertEqual(result["risk"], "low")
        self.assertEqual(result["plan_action"]["kind"], "continue")
        self.assertEqual(result["plan_action"]["primary_label"], "Copy brief")

    def test_summary_includes_surface_coverage_and_context_health(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="bloated",
                tool="claude-code",
                project_path="/repo",
                started_at=now - timedelta(hours=4),
                updated_at=now - timedelta(minutes=10),
                tokens_in=500_000,
                tokens_out=10_000,
                cost_usd=2.0,
            )
        ]
        health = ui.ContextHealth(
            session_id="bloated",
            tool="claude-code",
            project_path="/repo",
            age_hours=4,
            age_days=0.16,
            event_count=4,
            total_input_tokens=500_000,
            total_output_tokens=10_000,
            latest_turn_tokens=225_000,
            peak_turn_tokens=225_000,
            avg_turn_tokens=125_000,
            growth_rate=20_000,
            bloat_ratio=0.82,
            efficiency_pct=18.0,
            bloat_measurable=True,
            replayed_cost_usd=1.64,
            analyzed_cost_usd=2.0,
            latest_turn_replayed_tokens=220_000,
            is_stale=False,
            is_critical_stale=False,
            is_context_pressure=True,
            is_context_critical=True,
            is_high_bloat=True,
            is_extreme_bloat=True,
            severity="critical",
            recommendations=["Start a fresh session before continuing."],
        )
        coverage = [
            SurfaceCoverage(
                surface_id="claude-code-cli",
                label="Claude Code CLI",
                status="automatic",
                status_label="Automatic gate + history",
                detected=True,
                automatic_gate="hook",
                history="Full local history",
                action="Verify with hook-status.",
                detail="Best-covered surface.",
                session_count=1,
            )
        ]
        with (
            patch.object(ui, "scan_all", return_value=rows),
            patch.object(ui, "scan_all_events", return_value=[]),
            patch.object(ui, "discover_tools", return_value={}),
            patch.object(ui, "evidence_for_sessions", return_value={}),
            patch.object(ui, "survival_by_session", return_value={}),
            patch.object(ui, "analyze_all_sessions", return_value=[health]),
            patch.object(ui, "surface_coverage", return_value=coverage),
            patch.object(ui, "recent_handoff_decisions", return_value=[]),
        ):
            summary = ui.build_summary(7)

        self.assertEqual(summary["coverage"][0]["surface_id"], "claude-code-cli")
        self.assertTrue(any(step["command"] == "aiwatcher hook-status" for step in summary["setup"]))
        self.assertEqual(summary["context_health"][0]["severity"], "critical")
        self.assertEqual(summary["context_health"][0]["action"]["label"], "Start fresh")
        self.assertEqual(summary["handoff_bubble"]["session_id"], "bloated")
        self.assertIn("Fresh Start recommended", summary["handoff_bubble"]["title"])
        # Measured cache reads on the latest turn, not latest_turn_tokens * bloat_ratio.
        self.assertEqual(summary["handoff_bubble"]["expected_saved_context_tokens"], 220_000)
        self.assertEqual(summary["handoff_decisions"], [])

    def test_handoff_bubble_does_not_auto_open_unverified_desktop_app(self) -> None:
        bubble = ui._handoff_bubble([{
            "session_id": "claude-desktop",
            "project": "/repo/app",
            "tool": "claude-code",
            "severity": "critical",
            "latest_turn_tokens": "165.0k",
            "estimated_replayed_context_tokens": 120_000,
            "estimated_replayed_context_label": "120.0k",
            "can_handoff": True,
            "recommendation": "Context is critical.",
            "runtime_attachment": {
                "available": True,
                "level": "app",
                "action_label": "Open Claude",
            },
        }])

        self.assertIsNotNone(bubble)
        self.assertEqual(bubble["primary_label"], "Copy Fresh Start brief")
        self.assertNotIn("Open Claude", bubble["primary_label"])

    def test_recent_handoff_decision_suppresses_repeat_bubble(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="bloated",
            tool="claude-code",
            project_path="/repo",
            started_at=now - timedelta(hours=4),
            updated_at=now - timedelta(minutes=10),
            tokens_in=500_000,
            tokens_out=10_000,
            cost_usd=2.0,
        )
        health = ui.ContextHealth(
            session_id="bloated",
            tool="claude-code",
            project_path="/repo",
            age_hours=4,
            age_days=0.16,
            event_count=4,
            total_input_tokens=500_000,
            total_output_tokens=10_000,
            latest_turn_tokens=225_000,
            peak_turn_tokens=225_000,
            avg_turn_tokens=125_000,
            growth_rate=20_000,
            bloat_ratio=0.82,
            efficiency_pct=18.0,
            bloat_measurable=True,
            replayed_cost_usd=1.64,
            analyzed_cost_usd=2.0,
            latest_turn_replayed_tokens=220_000,
            is_stale=False,
            is_critical_stale=False,
            is_context_pressure=True,
            is_context_critical=True,
            is_high_bloat=True,
            is_extreme_bloat=True,
            severity="critical",
            recommendations=["Start a fresh session before continuing."],
        )
        with (
            patch.object(ui, "scan_all", return_value=[row]),
            patch.object(ui, "scan_all_events", return_value=[]),
            patch.object(ui, "discover_tools", return_value={}),
            patch.object(ui, "evidence_for_sessions", return_value={}),
            patch.object(ui, "survival_by_session", return_value={}),
            patch.object(ui, "analyze_all_sessions", return_value=[health]),
            patch.object(ui, "surface_coverage", return_value=[]),
            patch.object(ui, "recent_handoff_decisions", return_value=[{
                "created_at": now.isoformat(),
                "session_id": "bloated",
                "decision": "continue_here",
                "reason": "User already chose to continue.",
                "expected_saved_context_tokens": 220_500,
            }]),
        ):
            summary = ui.build_summary(7)

        self.assertIsNone(summary["handoff_bubble"])
        self.assertEqual(summary["handoff_decisions"][0]["decision"], "continue_here")
        self.assertEqual(summary["handoff_decisions"][0]["expected_saved_context_label"], "220.5k")

    def test_handoff_bubble_is_absent_when_context_is_healthy(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="healthy",
            tool="claude-code",
            project_path="/repo",
            started_at=now - timedelta(minutes=30),
            updated_at=now,
            tokens_in=30_000,
            tokens_out=10_000,
            cost_usd=0.1,
        )
        with (
            patch.object(ui, "scan_all", return_value=[row]),
            patch.object(ui, "scan_all_events", return_value=[]),
            patch.object(ui, "discover_tools", return_value={}),
            patch.object(ui, "evidence_for_sessions", return_value={}),
            patch.object(ui, "survival_by_session", return_value={}),
            patch.object(ui, "analyze_all_sessions", return_value=[]),
            patch.object(ui, "surface_coverage", return_value=[]),
            patch.object(ui, "recent_handoff_decisions", return_value=[]),
        ):
            summary = ui.build_summary(7)

        self.assertIsNone(summary["handoff_bubble"])

    def test_handoff_decision_endpoint_records_local_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                record = ui.record_handoff_decision(
                    session_id="s1",
                    decision="continue_here",
                    reason="User chose to continue despite context pressure.",
                    expected_saved_context_tokens=123_000,
                )
                decisions = recent_handoff_decisions()

        self.assertEqual(record["decision"], "continue_here")
        self.assertEqual(decisions[0]["session_id"], "s1")
        self.assertEqual(decisions[0]["expected_saved_context_tokens"], 123_000)

    def test_prompt_preflight_response_is_privacy_scoped(self) -> None:
        with patch.object(
            ui,
            "analyze_prompt",
            return_value={
                "risk": "high",
                "score": 8,
                "tool": "codex",
                "findings": ["Broad scope"],
                "suggestions": ["Inspect first"],
                "workflow": {
                    "mode": "fork_task",
                    "label": "Fork this task",
                    "instruction": "Fork the current chat and paste the brief.",
                    "reward": "Likely reward: isolates exploratory context.",
                },
                "suggested_prompt": "Task\nRefactor safely",
                "estimated_impact": {"available": False, "direction": "Narrower prompt should reduce pressure."},
            },
        ):
            result = ui.build_prompt_preflight(
                "Refactor the entire codebase and delete secrets",
                tool="codex",
                cwd="/repo",
            )

        self.assertEqual(result["risk"], "high")
        self.assertIn("Narrower prompt", result["impact_label"])
        self.assertIn("not persisted", result["privacy"])
        self.assertEqual(result["suggested_prompt"], "Task\nRefactor safely")
        self.assertEqual(result["workflow"]["mode"], "fork_task")

    def test_summary_respects_selected_window(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="recent",
                tool="claude-code",
                project_path="/repo/recent",
                started_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.1,
            ),
            LocalSession(
                session_id="old",
                tool="claude-code",
                project_path="/repo/old",
                started_at=now - timedelta(days=20),
                updated_at=now - timedelta(days=20),
                tokens_in=200,
                tokens_out=100,
                cost_usd=0.2,
            ),
        ]
        with (
            patch.object(ui, "scan_all", return_value=rows),
            patch.object(ui, "discover_tools", return_value={}),
        ):
            seven_days = ui.build_summary(7)
            thirty_days = ui.build_summary(30)

        self.assertEqual(seven_days["totals"]["sessions"], 1)
        self.assertEqual(thirty_days["totals"]["sessions"], 2)

    def test_cached_summary_shell_skips_heavy_event_scan_for_first_paint(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="recent",
                tool="claude-code",
                project_path="/repo/recent",
                started_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.1,
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "_cached_session_rows", return_value=rows),
                patch.object(ui, "scan_all", side_effect=AssertionError("session scan should be background-only")),
                patch.object(ui, "scan_all_events", side_effect=AssertionError("event scan should be background-only")),
                patch.object(ui, "discover_tools", return_value={}),
                patch.object(ui, "surface_coverage", return_value=[]),
                patch.object(ui, "_maybe_refresh_summary_cache", return_value=False),
            ):
                ui._SUMMARY_CACHE.clear()
                summary = ui.build_summary_cached(7)

        self.assertEqual(summary["totals"]["sessions"], 1)
        self.assertEqual(summary["cache"]["status"], "building")
        self.assertIn("watcher", summary)
        self.assertEqual(summary["context_health_status"], "pending")

    def test_first_paint_shell_is_never_persisted_to_the_disk_cache(self) -> None:
        """A shell must not outlive the process that built it.

        The shell carries the same schema version as a full summary, so if a
        background refresh crashes or is killed after first paint, a persisted
        shell would be served as a normal summary for the whole disk TTL --
        blank survival, context health, and receipts, with no error anywhere.
        """
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="recent",
                tool="claude-code",
                project_path="/repo/recent",
                started_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.1,
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "discover_tools", return_value={}),
                patch.object(ui, "surface_coverage", return_value=[]),
                # Stand in for a background refresh that never completes.
                patch.object(ui, "_maybe_refresh_summary_cache", return_value=False),
            ):
                ui._SUMMARY_CACHE.clear()
                ui._SUMMARY_REFRESHED_AT.clear()
                first = ui.build_summary_cached(7)
                self.assertEqual(first["cache"]["status"], "building")
                self.assertFalse(ui._summary_cache_path(7).exists())

                # With memory cleared there is nothing to fall back to, so the
                # next request rebuilds a shell rather than serving a stale one.
                ui._SUMMARY_CACHE.clear()
                ui._SUMMARY_REFRESHED_AT.clear()
                second = ui.build_summary_cached(7)

        self.assertEqual(second["cache"]["status"], "building")
        self.assertEqual(second["cache"]["source"], "computed")

    def test_incomplete_summary_on_disk_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                cache_path = ui._summary_cache_path(7)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "cache_schema_version": ui.SUMMARY_CACHE_SCHEMA_VERSION,
                    "summary_complete": False,
                    "survival": {"available": False, "reason": "Background evidence refresh pending."},
                }
                cache_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNone(ui._read_summary_disk_cache(7))

                payload["summary_complete"] = True
                cache_path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertIsNotNone(ui._read_summary_disk_cache(7))

    def test_shared_refresh_scans_once_and_materializes_all_windows(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [LocalSession(
            session_id="shared-refresh",
            tool="codex-cli",
            project_path="/repo/shared",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=1),
        )]
        complete = {
            "generated_at": now.isoformat(),
            "cache_schema_version": ui.SUMMARY_CACHE_SCHEMA_VERSION,
            "summary_complete": True,
            "_session_index": [row.to_json() for row in rows],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows) as scan_sessions,
                patch.object(ui, "scan_all_events", return_value=[]) as scan_events,
                patch.object(ui, "build_summary", side_effect=lambda days, **kwargs: {**complete, "days": days}) as build,
            ):
                with ui._SUMMARY_CACHE_LOCK:
                    ui._SUMMARY_CACHE.clear()
                    ui._SUMMARY_REFRESHING.clear()
                ui._refresh_summary_cache(7)

        scan_sessions.assert_called_once()
        scan_events.assert_called_once()
        self.assertEqual([call.args[0] for call in build.call_args_list], [7, 1, 30])
        self.assertTrue(all(window in ui._SUMMARY_CACHE for window in (1, 7, 30)))

    def test_http_session_detail_returns_fast_pending_card_before_event_index(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="pending-detail",
            tool="codex-cli",
            project_path="/repo/pending",
            started_at=now - timedelta(hours=1),
            updated_at=now - timedelta(minutes=2),
        )
        with ui._SUMMARY_CACHE_LOCK:
            ui._SESSION_INDEX.clear()
            ui._EVENT_INDEX.clear()
            ui._EVENT_INDEX_READY = False
        ui._index_sessions([row])
        with patch.object(ui, "scan_all_events", side_effect=AssertionError("request thread must not scan transcripts")):
            detail = ui.build_session_detail("pending-detail", allow_pending=True)

        self.assertTrue(detail["detail_pending"])
        self.assertEqual(detail["session_id"], "pending-detail")
        self.assertTrue(detail["summary_only"])

    def test_stale_summary_disk_cache_without_schema_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            cache_dir = os.path.join(temp_dir, "cache")
            os.makedirs(cache_dir)
            cache_path = os.path.join(cache_dir, "ui-summary-7.json")
            with open(cache_path, "w", encoding="utf-8") as handle:
                json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "projects": [{"id": "/"}]}, handle)

            with patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}):
                self.assertIsNone(ui._read_summary_disk_cache(7))

    def test_project_health_flags_heavy_usage_as_actionable_review(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="heavy",
                tool="codex-cli",
                project_path="/repo/heavy",
                started_at=now - timedelta(hours=3),
                updated_at=now - timedelta(hours=1),
                tokens_in=1_500_000,
                tokens_out=50_000,
                agent_calls=300,
                tool_calls=320,
                cost_usd=12.0,
            )
        ]

        projects = ui.group_projects(rows)

        self.assertEqual(projects[0]["health"]["status"], "review")
        self.assertEqual(projects[0]["health"]["action_label"], "Review")

    def test_root_project_is_labeled_unattributed_and_sorted_after_real_projects(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="root",
                tool="claude-code",
                project_path="/",
                started_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
                tokens_in=10_000_000,
                cost_usd=92.0,
            ),
            LocalSession(
                session_id="real",
                tool="claude-code",
                project_path="/repo/real",
                started_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
                tokens_in=10,
                cost_usd=1.0,
            ),
        ]

        projects = ui.group_projects(rows)

        self.assertEqual(projects[0]["id"], "/repo/real")
        self.assertTrue(projects[0]["attributed"])
        self.assertEqual(projects[1]["id"], ui.UNATTRIBUTED_PROJECT)
        self.assertEqual(projects[1]["short_name"], ui.UNATTRIBUTED_PROJECT_LABEL)
        self.assertFalse(projects[1]["attributed"])

    def test_session_state_and_actions_do_not_overclaim_tool_deeplinks(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="active-heavy",
            tool="codex-cli",
            project_path="/repo",
            started_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=2),
            tokens_in=600_000,
            tokens_out=20_000,
            agent_calls=260,
            tool_calls=10,
        )

        state = ui.session_state(row, now=now)
        actions = ui.session_actions(row, outcome=None)

        self.assertEqual(state["status"], "active")
        self.assertTrue(any(action["id"] == "review_outcome" for action in actions))
        self.assertTrue(any(action["id"] == "handoff" for action in actions))
        self.assertEqual(sum(1 for action in actions if action.get("primary")), 1)
        handoff = next(action for action in actions if action["id"] == "handoff")
        self.assertTrue(handoff["primary"])
        self.assertEqual(handoff["label"], "Build Fresh Start brief")
        open_tool = next(action for action in actions if action["id"] == "open_tool")
        self.assertNotEqual(open_tool["level"], "exact_session")
        self.assertIn(open_tool["level"], {"workspace", "unavailable"})

    def test_historical_heavy_session_still_offers_copyable_fresh_start(self) -> None:
        row = LocalSession(
            session_id="old-heavy",
            tool="codex-cli",
            project_path="/repo",
            updated_at=datetime.now(timezone.utc) - timedelta(days=8),
            tokens_in=600_000,
            tokens_out=20_000,
            agent_calls=260,
        )

        actions = ui.session_actions(row, outcome=None)
        handoff = next(action for action in actions if action["id"] == "handoff")

        self.assertEqual(handoff["label"], "Build Fresh Start brief")
        self.assertFalse(handoff["primary"])
        self.assertIn("historical local evidence", handoff["reason"])
        self.assertTrue(next(action for action in actions if action["id"] == "review_outcome")["primary"])

    def test_serve_terminates_started_ambient_resource_on_stop(self) -> None:
        resource = Mock()
        server = Mock()
        server.serve_forever.side_effect = KeyboardInterrupt()

        with (
            patch.object(ui, "ThreadingHTTPServer", return_value=server),
            patch.object(ui, "record_ui_server"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            ui.serve(
                host="127.0.0.1",
                port=8765,
                auto_port=False,
                on_started=lambda _host, _port: resource,
            )

        resource.terminate.assert_called_once()
        server.server_close.assert_called_once()

    def test_today_reports_window_scoped_useful_outcomes(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="recent",
                tool="claude-code",
                project_path="/repo",
                started_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.4,
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "discover_tools", return_value={}),
            ):
                record_outcome("recent", "useful")
                record_intervention(
                    tool="claude",
                    cwd="/repo",
                    risk="medium",
                    score=4,
                    findings=["Broad scope"],
                    original_prompt="original",
                    suggested_prompt="safer",
                    decision="suggested",
                    selected_prompt="safer",
                )
                summary = ui.build_summary(7)

        self.assertEqual(summary["totals"]["useful_outcomes"], 1)
        self.assertEqual(summary["totals"]["cost_per_useful_change"], "$0.40")
        self.assertEqual(summary["totals"]["preflight_decisions"], 1)
        self.assertEqual(summary["recent_sessions"][0]["outcome"], "useful")

    def test_today_surfaces_inferred_outcome_evidence_for_review(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="recent",
                tool="claude-code",
                project_path="/repo",
                started_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=1),
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.4,
            )
        ]

        class Evidence:
            inferred_outcome = "useful"

            def to_json(self):
                return {
                    "inferred_outcome": "useful",
                    "confidence": "low",
                    "commits": [{"sha": "abc123"}],
                    "changed_files": [],
                    "tests": [],
                    "reasons": ["A nearby commit was detected."],
                }

        with (
            patch.object(ui, "scan_all", return_value=rows),
            patch.object(ui, "scan_all_events", return_value=[]),
            patch.object(ui, "discover_tools", return_value={}),
            patch.object(ui, "evidence_for_sessions", return_value={"recent": Evidence()}),
            patch.object(ui, "survival_by_session", return_value={}),
        ):
            summary = ui.build_summary(7)

        self.assertEqual(summary["totals"]["inferred_useful_outcomes"], 1)
        self.assertEqual(summary["recent_sessions"][0]["inferred_outcome"], "useful")
        self.assertTrue(any(item["id"] == "outcome-review" for item in summary["insights"]))

    def test_summary_marks_passively_captured_evidence(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="recent",
                tool="claude-code",
                project_path="/repo",
                started_at=now - timedelta(hours=5),
                updated_at=now - timedelta(hours=4),
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.4,
            )
        ]

        class Evidence:
            inferred_outcome = None

            def to_json(self):
                return {
                    "inferred_outcome": None,
                    "confidence": "low",
                    "commits": [],
                    "changed_files": [],
                    "tests": [],
                    "reasons": [],
                }

        with (
            patch.object(ui, "scan_all", return_value=rows),
            patch.object(ui, "scan_all_events", return_value=[]),
            patch.object(ui, "discover_tools", return_value={}),
            patch.object(ui, "evidence_for_sessions", return_value={"recent": Evidence()}),
            patch.object(ui, "survival_by_session", return_value={}),
        ):
            summary = ui.build_summary(7)

        self.assertTrue(summary["recent_sessions"][0]["evidence_captured"])
        self.assertIsNotNone(summary["recent_sessions"][0]["evidence_recorded_at"])

    # The three _cost_per_surviving_change tests that stood here were deleted with
    # the function. They kept passing after the reachability metric was replaced by
    # line survival, which is why nothing caught `aiwatcher report` crashing on the
    # new schema: the suite was exercising a function no caller reached.

    def test_today_surfaces_churned_insight(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(
                session_id="recent", tool="claude-code", project_path="/repo",
                started_at=now - timedelta(hours=2), updated_at=now - timedelta(hours=1),
                tokens_in=100, tokens_out=50, cost_usd=0.4,
            )
        ]

        class ChurnedEvidence:
            inferred_outcome = "churned"

            def to_json(self):
                return {"inferred_outcome": "churned"}

        with (
            patch.object(ui, "scan_all", return_value=rows),
            patch.object(ui, "scan_all_events", return_value=[]),
            patch.object(ui, "discover_tools", return_value={}),
            patch.object(ui, "evidence_for_sessions", return_value={"recent": ChurnedEvidence()}),
            patch.object(ui, "survival_by_session", return_value={}),
            patch.object(ui, "evidence_snapshots_for_sessions", return_value={}),
        ):
            summary = ui.build_summary(7)

        self.assertTrue(any(item["id"] == "churned" for item in summary["insights"]))
        self.assertEqual(summary["recent_sessions"][0]["inferred_outcome"], "churned")

    def test_handoff_detail_returns_capsule_for_session(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="recent",
            tool="claude-code",
            project_path="/repo",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=1),
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.4,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=[row]),
                patch.object(ui, "scan_all_events", return_value=[]),
            ):
                capsule = ui.build_handoff_detail("recent", days=7, target="cursor")

        self.assertEqual(capsule["session_id"], "recent")
        self.assertEqual(capsule["target"], "cursor")
        self.assertIn("next_brief", capsule)
        self.assertIn("Project", capsule["next_brief"])
        self.assertIn("Cursor", capsule["next_brief"])

    def test_handoff_detail_defaults_prompt_excerpt_off_and_respects_opt_in(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="recent",
            tool="claude-code",
            project_path="/repo",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=1),
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.4,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=[row]),
                patch.object(ui, "scan_all_events", return_value=[]),
            ):
                default_capsule = ui.build_handoff_detail("recent", days=7, target="generic")
                opted_in_capsule = ui.build_handoff_detail("recent", days=7, target="generic", include_prompt_excerpt=True)

        self.assertFalse(default_capsule["include_prompt_excerpt"])
        self.assertTrue(opted_in_capsule["include_prompt_excerpt"])

    def test_handoff_and_session_detail_use_cached_session_index(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="cached-fast",
            tool="codex-cli",
            project_path="/repo/fast",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(minutes=5),
            tokens_in=100_000,
            tokens_out=1_000,
            agent_calls=22,
            tool_calls=9,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with ui._SUMMARY_CACHE_LOCK:
                ui._SESSION_INDEX.clear()
                ui._SUMMARY_CACHE.clear()
            ui._index_sessions([row])
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "_read_summary_disk_cache", return_value=None),
                patch.object(ui, "rows_for_window", side_effect=AssertionError("slow session scan should not run")),
                patch.object(ui, "scan_all", side_effect=AssertionError("full scanner should not run")),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "safe_runtime_processes", return_value=[]),
            ):
                detail = ui.build_session_detail("cached-fast", days=7)
                capsule = ui.build_handoff_detail("cached-fast", days=7, target="codex")

        self.assertEqual(detail["session_id"], "cached-fast")
        self.assertEqual(capsule["session_id"], "cached-fast")
        self.assertIn("runtime_attachment", capsule)

    def test_basic_handoff_detail_is_copyable_without_event_scan(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="basic-fast",
            tool="codex-cli",
            project_path="/repo/fast",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(minutes=5),
            tokens_in=120_000,
            tokens_out=8_000,
            cost_usd=0.42,
            agent_calls=8,
            tool_calls=3,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with ui._SUMMARY_CACHE_LOCK:
                ui._SESSION_INDEX.clear()
                ui._SUMMARY_CACHE.clear()
            ui._index_sessions([row])
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all_events", side_effect=AssertionError("basic handoff should not scan events")),
                patch.object(ui, "build_outcome_evidence", side_effect=AssertionError("basic handoff should not build git evidence")),
                patch.object(ui, "safe_runtime_processes", return_value=[]),
            ):
                capsule = ui.build_basic_handoff_detail("basic-fast", days=7, target="codex")

        self.assertTrue(capsule["basic"])
        self.assertEqual(capsule["enrichment_status"], "loading")
        self.assertEqual(capsule["usage"]["tokens_label"], "128.0k")
        self.assertIn("AIWatcher Fresh Start brief", capsule["next_brief"])
        self.assertIn("How to continue", capsule["next_brief"])
        self.assertIn("If this is a forked chat", capsule["next_brief"])
        self.assertIn("If this is a subagent task", capsule["next_brief"])
        self.assertIn("First response required", capsule["next_brief"])
        self.assertIn("Detailed git, timeline, and prompt evidence is still loading", capsule["next_brief"])

    def test_structured_handoff_fields_shape_the_brief(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="structured",
            tool="codex-cli",
            project_path="/repo/product",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(minutes=5),
            tokens_in=140_000,
            tokens_out=9_000,
            cost_usd=0.92,
            agent_calls=12,
            tool_calls=4,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=[row]),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "safe_runtime_processes", return_value=[]),
            ):
                capsule = ui.build_handoff_detail(
                    "structured",
                    days=7,
                    target="cursor",
                    handoff_type="review",
                    objective="Review the Fresh Start flow against the OSS strategy.",
                    source_refs=["strategy.md", "PR 46"],
                    constraints=["Findings first.", "Do not broaden scope."],
                    acceptance_criteria=["No fake savings claims.", "Tests cover structured fields."],
                )

        self.assertEqual(capsule["handoff_type"], "review")
        self.assertEqual(capsule["objective"], "Review the Fresh Start flow against the OSS strategy.")
        self.assertEqual(capsule["source_refs"], ["strategy.md", "PR 46"])
        self.assertEqual(capsule["constraints"], ["Findings first.", "Do not broaden scope."])
        self.assertEqual(capsule["acceptance_criteria"], ["No fake savings claims.", "Tests cover structured fields."])
        self.assertIn("Continuation type: Review continuation.", capsule["next_brief"])
        self.assertIn("Review the Fresh Start flow", capsule["next_brief"])
        self.assertIn("strategy.md", capsule["next_brief"])
        self.assertIn("No fake savings claims.", capsule["next_brief"])

    def test_basic_handoff_detail_accepts_structured_fields_without_event_scan(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="basic-structured",
            tool="codex-cli",
            project_path="/repo/fast",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(minutes=5),
            tokens_in=120_000,
            tokens_out=8_000,
            cost_usd=0.42,
        )
        with ui._SUMMARY_CACHE_LOCK:
            ui._SESSION_INDEX.clear()
            ui._SUMMARY_CACHE.clear()
        ui._index_sessions([row])
        with (
            patch.object(ui, "scan_all_events", side_effect=AssertionError("basic handoff should not scan events")),
            patch.object(ui, "safe_runtime_processes", return_value=[]),
        ):
            capsule = ui.build_basic_handoff_detail(
                "basic-structured",
                days=7,
                target="codex",
                handoff_type="bugbash",
                objective="Reproduce and fix the broken handoff drawer.",
                source_refs=["screenshot", "local UI"],
                constraints=["Keep privacy opt-in."],
                acceptance_criteria=["Copy flow works from Settings."],
            )

        self.assertTrue(capsule["basic"])
        self.assertEqual(capsule["handoff_type"], "bugbash")
        self.assertIn("Continuation type: Bug bash continuation.", capsule["next_brief"])
        self.assertIn("Reproduce and fix", capsule["next_brief"])
        self.assertIn("Keep privacy opt-in.", capsule["next_brief"])

    def test_demo_handoff_is_seeded_privacy_safe_and_not_live(self) -> None:
        capsule = ui.build_demo_handoff_detail(
            target="codex",
            handoff_type="product",
            objective="Validate Fresh Start before testing real sessions.",
        )

        self.assertTrue(capsule["demo"])
        self.assertEqual(capsule["session_id"], "demo-fresh-start")
        self.assertEqual(capsule["handoff_type"], "product")
        self.assertIn("Validate Fresh Start", capsule["next_brief"])
        self.assertTrue(any("Demo context pressure" in item for item in capsule["warnings"]))
        runtime = capsule["runtime_attachment"]
        self.assertFalse(runtime["available"])
        self.assertEqual(runtime["identity_level"], "demo")
        self.assertEqual(runtime["identity_label"], "Demo sample")

    def test_invalid_demo_handoff_type_falls_back_to_demo_default(self) -> None:
        options = ui._handoff_options_from_query({"type": ["bad"]}, default_type="product")

        self.assertEqual(options["handoff_type"], "product")

    def test_handoff_demo_endpoint_accepts_posted_structured_fields(self) -> None:
        server, thread, base = DashboardServeTests()._serve_one()
        payload = json.dumps({
            "target": "cursor",
            "type": "review",
            "objective": "Review the handoff flow.",
            "source_refs": ["strategy.md"],
            "constraints": ["Findings first."],
            "acceptance_criteria": ["No overclaims."],
        }).encode("utf-8")
        http_request = request.Request(
            f"{base}/api/handoff-demo",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
        finally:
            thread.join(timeout=5)
            server.server_close()

        self.assertTrue(body["demo"])
        self.assertEqual(body["target"], "cursor")
        self.assertEqual(body["handoff_type"], "review")
        self.assertEqual(body["source_refs"], ["strategy.md"])
        self.assertEqual(body["constraints"], ["Findings first."])
        self.assertEqual(body["acceptance_criteria"], ["No overclaims."])

    def test_session_summary_uses_cached_index_without_event_scan(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="summary-fast",
            tool="codex-cli",
            project_path="/repo/fast",
            started_at=now - timedelta(hours=1),
            updated_at=now - timedelta(minutes=3),
            tokens_in=60_000,
            tokens_out=3_000,
            agent_calls=12,
            tool_calls=5,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with ui._SUMMARY_CACHE_LOCK:
                ui._SESSION_INDEX.clear()
                ui._SUMMARY_CACHE.clear()
            ui._index_sessions([row])
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "_read_summary_disk_cache", return_value=None),
                patch.object(ui, "rows_for_window", side_effect=AssertionError("slow session scan should not run")),
                patch.object(ui, "scan_all_events", side_effect=AssertionError("event scan should not run")),
                patch.object(ui, "safe_runtime_processes", return_value=[]),
            ):
                summary = ui.build_session_summary("summary-fast", days=7)

        self.assertEqual(summary["session_id"], "summary-fast")
        self.assertTrue(summary["summary_only"])
        self.assertEqual(summary["detail_status"], "loading")
        self.assertIn("runtime_attachment", summary)

    def test_recent_log_does_not_claim_live_chat_attachment(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="recent-log",
            tool="codex-cli",
            project_path="/repo/fast",
            source_path="/Users/test/.codex/sessions/recent-log.jsonl",
            started_at=now - timedelta(minutes=10),
            updated_at=now - timedelta(minutes=2),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with ui._SUMMARY_CACHE_LOCK:
                ui._SESSION_INDEX.clear()
                ui._SUMMARY_CACHE.clear()
            ui._index_sessions([row])
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "_read_summary_disk_cache", return_value=None),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "safe_runtime_processes", return_value=[]),
                patch("aiwatcher_cli.runtime_attachment.sys.platform", "darwin"),
                patch("aiwatcher_cli.runtime_attachment.shutil.which", return_value=None),
            ):
                detail = ui.build_session_detail("recent-log", days=7)
                capsule = ui.build_handoff_detail("recent-log", days=7)

        self.assertEqual(detail["state"]["label"], "Active log")
        self.assertEqual(detail["runtime_attachment"]["action_label"], "Open workspace")
        self.assertEqual(detail["runtime_attachment"]["identity_label"], "Likely workspace")
        self.assertFalse(detail["runtime_attachment"]["exact_return_available"])
        self.assertEqual(detail["runtime_attachment"]["exact_return_label"], "Workspace only")
        self.assertIn("Exact AI chat return is not available", detail["runtime_attachment"]["reason"])
        self.assertEqual(capsule["source_path"], "/Users/test/.codex/sessions/recent-log.jsonl")
        self.assertEqual(capsule["runtime_attachment"]["exact_return_label"], "Workspace only")
        self.assertEqual(capsule["runtime_attachment"]["identity_label"], "Likely workspace")
        self.assertNotIn("/Users/test/.codex/sessions/recent-log.jsonl", capsule["next_brief"])

    def test_context_health_groups_duplicate_project_sessions(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            LocalSession(session_id="s1", tool="codex-cli", project_path="/repo/app", updated_at=now),
            LocalSession(session_id="s2", tool="codex-cli", project_path="/repo/app", updated_at=now - timedelta(minutes=2)),
            LocalSession(session_id="s3", tool="claude-code", project_path="/repo/docs", updated_at=now - timedelta(minutes=3)),
        ]

        def _health(
            session_id: str,
            project_path: str,
            latest_tokens: int,
            *,
            tool: str = "codex-cli",
            severity: str = "critical",
            hours: float = 1.0,
        ) -> ui.ContextHealth:
            return ui.ContextHealth(
                session_id=session_id,
                tool=tool,
                project_path=project_path,
                age_hours=hours,
                age_days=hours / 24,
                event_count=4,
                total_input_tokens=latest_tokens * 2,
                total_output_tokens=1_000,
                latest_turn_tokens=latest_tokens,
                peak_turn_tokens=latest_tokens,
                avg_turn_tokens=latest_tokens,
                growth_rate=1_000,
                bloat_ratio=0.98,
                efficiency_pct=2.0,
                bloat_measurable=True,
                replayed_cost_usd=0.25,
                analyzed_cost_usd=0.5,
                latest_turn_replayed_tokens=int(latest_tokens * 0.98),
                is_stale=False,
                is_critical_stale=False,
                is_context_pressure=True,
                is_context_critical=True,
                is_high_bloat=True,
                is_extreme_bloat=True,
                severity=severity,
                recommendations=["Start a fresh session before continuing."],
            )

        health_rows = [
            _health("s1", "/repo/app", 200_000, hours=2),
            _health("s2", "/repo/app", 150_000, hours=1),
            _health("s3", "/repo/docs", 100_000, tool="claude-code", hours=1),
        ]
        with patch.object(ui, "analyze_all_sessions", return_value=health_rows):
            cards = ui._context_health_cards(rows, [])

        self.assertEqual(len(cards), 2)
        app_card = next(card for card in cards if card["project"] == "/repo/app")
        self.assertEqual(app_card["session_id"], "s1")
        self.assertEqual(app_card["session_count"], 2)
        self.assertEqual(app_card["critical_sessions"], 2)
        self.assertIn("2 sessions need attention", app_card["group_note"])
        self.assertEqual(len(app_card["related_sessions"]), 2)

    def test_session_detail_degrades_when_state_snapshot_read_fails(self) -> None:
        now = datetime.now(timezone.utc)
        row = LocalSession(
            session_id="recent",
            tool="claude-code",
            project_path="/repo",
            started_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=1),
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.4,
        )
        with (
            patch.object(ui, "rows_for_window", return_value=[row]),
            patch.object(ui, "scan_all_events", return_value=[]),
            patch.object(ui, "evidence_snapshots_for_sessions", side_effect=OSError("locked")),
            patch.object(ui, "get_outcome", side_effect=OSError("locked")),
        ):
            detail = ui.build_session_detail("recent", days=7)

        self.assertEqual(detail["session_id"], "recent")
        self.assertEqual(detail["project"], "/repo")
        self.assertIsNone(detail["outcome"])

    def test_receipt_combines_prediction_observation_and_outcome(self) -> None:
        now = datetime.now(timezone.utc)
        session = LocalSession(
            session_id="result-1",
            tool="claude-code",
            project_path="/repo",
            started_at=now - timedelta(minutes=1),
            updated_at=now,
            tokens_in=1_000,
            tokens_out=200,
            cost_usd=0.03,
            agent_calls=2,
            tool_calls=1,
        )
        intervention = {
            "id": "intervention-1",
            "created_at": now.isoformat(),
            "tool": "claude",
            "cwd": "/repo",
            "risk": "high",
            "score": 8,
            "selected_risk": "low",
            "selected_score": 2,
            "risk_points_reduced": 6,
            "decision": "brief_accepted",
            "session_id": "result-1",
            "predicted_impact": {
                "available": True,
                "confidence": "medium",
                "basis": "10 comparable sessions",
                "original": {
                    "tokens": [5_000, 7_000],
                    "model_calls": [8, 12],
                    "tool_calls": [5, 9],
                    "api_value_usd": [0.1, 0.2],
                },
                "savings": {
                    "tokens": [3_000, 5_000],
                    "model_calls": [4, 8],
                    "tool_calls": [2, 6],
                    "api_value_usd": [0.05, 0.12],
                },
            },
        }
        receipts = ui._build_intervention_receipts(
            [intervention], [session], {"result-1": {"outcome": "useful"}}
        )

        receipt = receipts[0]
        self.assertEqual(receipt["risk_points_reduced"], 6)
        self.assertEqual(receipt["actual"]["tokens"], 1_200)
        self.assertEqual(receipt["outcome"], "useful")
        self.assertIsNotNone(receipt["inferred"])

    def test_receipt_uses_post_intervention_events_for_existing_thread(self) -> None:
        now = datetime.now(timezone.utc)
        session = LocalSession(
            session_id="existing-thread",
            tool="claude-code",
            project_path="/repo",
            started_at=now - timedelta(days=3),
            updated_at=now + timedelta(seconds=10),
            tokens_in=900_000,
            tokens_out=20_000,
            agent_calls=500,
        )
        intervention = {
            "id": "intervention-existing",
            "created_at": now.isoformat(),
            "tool": "claude",
            "cwd": "/repo",
            "risk": "high",
            "score": 8,
            "decision": "brief_accepted",
            "session_id": "existing-thread",
        }
        events = [
            LocalEvent(
                event_id="event-1",
                session_id="existing-thread",
                tool="claude-code",
                event_type="assistant",
                timestamp=now + timedelta(seconds=5),
                tokens_in=1_200,
                tokens_out=300,
            )
        ]

        receipt = ui._build_intervention_receipts(
            [intervention], [session], {}, events
        )[0]

        self.assertTrue(receipt["actual"]["reliable"])
        self.assertEqual(receipt["actual"]["tokens"], 1_500)

    def test_blocked_receipt_does_not_claim_a_resulting_session(self) -> None:
        now = datetime.now(timezone.utc)
        blocked_session = LocalSession(
            session_id="session-after-block",
            tool="claude-code",
            project_path="/repo",
            started_at=now,
            updated_at=now,
        )
        receipts = ui._build_intervention_receipts([{
            "id": "blocked-1",
            "created_at": now.isoformat(),
            "tool": "claude",
            "cwd": "/repo",
            "decision": "blocked",
            "risk": "high",
            "score": 8,
            "session_id": blocked_session.session_id,
        }], [blocked_session], {}, [])

        self.assertEqual(receipts[0]["session_status"], "No session expected")
        self.assertIsNone(receipts[0]["session_id"])
        self.assertIsNone(receipts[0]["actual"])

    def test_cumulative_codex_totals_do_not_create_false_context_insight(self) -> None:
        now = datetime.now(timezone.utc)
        cumulative = LocalSession(
            session_id="codex-cumulative",
            tool="codex-cli",
            project_path="/repo",
            started_at=now - timedelta(hours=1),
            updated_at=now,
            model="gpt-5.5",
            tokens_in=500_000_000,
            agent_calls=500,
            notes=["tokens_used is Codex's cumulative thread total"],
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=[cumulative]),
                patch.object(ui, "discover_tools", return_value={}),
            ):
                summary = ui.build_summary(1)

        titles = {item["title"] for item in summary["insights"]}
        self.assertNotIn("Large-context session", titles)
        self.assertNotIn("Possible iterative loop", titles)


class WeeklyDigestTests(unittest.TestCase):
    def _session(self, session_id: str, *, cost_usd: float = 1.0, hours_ago: int = 1) -> LocalSession:
        now = datetime.now(timezone.utc)
        return LocalSession(
            session_id=session_id,
            tool="claude-code",
            project_path="/repo",
            started_at=now - timedelta(hours=hours_ago, minutes=5),
            updated_at=now - timedelta(hours=hours_ago),
            cost_usd=cost_usd,
        )

    def test_digest_outcome_breakdown_uses_window_scoped_sessions(self) -> None:
        rows = [self._session("useful-1"), self._session("rework-1")]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                record_outcome("useful-1", "useful")
                record_outcome("rework-1", "rework")
                digest = ui.build_weekly_digest(7)

        self.assertEqual(digest["outcomes"]["useful"], 1)
        self.assertEqual(digest["outcomes"]["rework"], 1)
        self.assertEqual(digest["outcomes"]["abandoned"], 0)

    def test_digest_picks_highest_cost_useful_session(self) -> None:
        rows = [
            self._session("cheap", cost_usd=1.0),
            self._session("expensive", cost_usd=9.0),
            self._session("not-useful", cost_usd=99.0),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                record_outcome("cheap", "useful")
                record_outcome("expensive", "useful")
                record_outcome("not-useful", "rework")
                digest = ui.build_weekly_digest(7)

        self.assertEqual(digest["highest_cost_useful_session"]["api_value_label"], "$9.00")

    def test_digest_surfaces_loop_and_velocity_candidates(self) -> None:
        rows = [self._session("s1")]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
                patch.object(ui, "_loop_signal", return_value={"diagnosis": "Possible loop: identical content repeated 4x."}),
                patch.object(ui, "_velocity_signal", return_value={"ratio": 3.2}),
            ):
                digest = ui.build_weekly_digest(7)

        self.assertEqual(len(digest["loop_candidates"]), 1)
        self.assertIn("identical content", digest["loop_candidates"][0]["diagnosis"])
        self.assertEqual(len(digest["velocity_candidates"]), 1)
        self.assertEqual(digest["velocity_candidates"][0]["ratio_label"], "3.2x baseline pace")

    def test_digest_counts_gates_fired_and_blocked_within_window(self) -> None:
        rows: list[LocalSession] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                record_command_decision(tool="claude", command="rm -rf /", pattern_id="rm_rf", reason="destructive", decision="block")
                record_command_decision(tool="claude", command="git push -f", pattern_id="git_push_force", reason="force push", decision="allow_once")
                digest = ui.build_weekly_digest(7)

        self.assertEqual(digest["command_gate"]["gates_fired"], 2)
        self.assertEqual(digest["command_gate"]["commands_blocked"], 1)

    def test_digest_prompt_gate_counts_flagged_and_modified(self) -> None:
        rows: list[LocalSession] = []

        def _intervention(decision: str) -> None:
            record_intervention(
                tool="claude", cwd="/repo", risk="high", score=8, findings=["Broad scope"],
                original_prompt="delete everything", suggested_prompt="inspect first",
                decision=decision, selected_prompt="inspect first",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                _intervention("brief_accepted")
                _intervention("brief_edited")
                _intervention("auto_brief_headless")
                _intervention("context_added")
                _intervention("allowed_original")
                _intervention("cancelled")
                _intervention("blocked")
                digest = ui.build_weekly_digest(7)

        self.assertEqual(digest["prompt_gate"]["flagged"], 7)
        self.assertEqual(digest["prompt_gate"]["modified"], 4)

    def test_digest_top_sessions_lists_costliest_regardless_of_outcome(self) -> None:
        rows = [
            self._session("cheap", cost_usd=1.0),
            self._session("expensive-unmarked", cost_usd=50.0),
            self._session("useful", cost_usd=9.0),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                record_outcome("useful", "useful")
                digest = ui.build_weekly_digest(7)

        top = digest["top_sessions"]
        self.assertEqual([s["api_value_label"] for s in top], ["$50.00", "$9.00", "$1.00"])
        self.assertEqual(top[0]["outcome"], None)
        self.assertEqual(top[1]["outcome"], "useful")
        self.assertEqual(top[0]["session_id"], "expensive-unmarked")
        self.assertTrue(all(s["session_id"] for s in top), "top_sessions must carry session_id for UI drill-down links")

    def test_digest_recommendation_prioritizes_blocked_commands(self) -> None:
        rows = [self._session("s1")]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
                patch.object(ui, "_loop_signal", return_value={"diagnosis": "Possible loop: identical content repeated 4x."}),
            ):
                record_command_decision(tool="claude", command="rm -rf /", pattern_id="rm_rf", reason="destructive", decision="block")
                digest = ui.build_weekly_digest(7)

        self.assertIn("blocked", digest["recommendation"])
        self.assertIn("hook-status", digest["recommendation"])

    def test_digest_recommendation_falls_back_to_healthy_with_no_signals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=[]),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                digest = ui.build_weekly_digest(7)

        self.assertIn("healthy", digest["recommendation"])

    def test_build_report_includes_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "scan_all", return_value=[]),
                patch.object(ui, "scan_all_events", return_value=[]),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                report = ui.build_report(7)

        self.assertIn("digest", report)
        self.assertIn("recommendation", report["digest"])


class SessionSearchTests(unittest.TestCase):
    """S-27 UI surface: build_session_search() must delegate matching to
    cli.filter_sessions() rather than re-implementing it (issue #34)."""

    def _session(self, session_id: str, *, project_path: str = "/repo", cost_usd: float = 1.0, hours_ago: int = 1) -> LocalSession:
        now = datetime.now(timezone.utc)
        return LocalSession(
            session_id=session_id,
            tool="claude-code",
            project_path=project_path,
            started_at=now - timedelta(hours=hours_ago, minutes=5),
            updated_at=now - timedelta(hours=hours_ago),
            cost_usd=cost_usd,
        )

    def test_session_search_filters_by_text_and_outcome(self) -> None:
        rows = [
            self._session("s1", project_path="/repo/alpha"),
            self._session("s2", project_path="/repo/alpha"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = os.path.join(temp_dir, "state.json")
            with (
                patch.dict(os.environ, {"AIWATCHER_STATE_FILE": state_file}),
                patch.object(ui, "_cached_session_rows", return_value=rows),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                record_outcome("s1", "useful")
                result = ui.build_session_search(30, search="alpha", outcome="useful")

        self.assertEqual(result["total_matched"], 1)
        self.assertEqual(result["sessions"][0]["session_id"], "s1")
        self.assertEqual(result["query"], {"search": "alpha", "outcome": "useful", "evidence": "", "state": ""})

    def test_session_search_no_match_returns_empty_list(self) -> None:
        rows = [self._session("s1", project_path="/repo/alpha")]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}),
            patch.object(ui, "_cached_session_rows", return_value=rows),
            patch.object(ui, "evidence_for_sessions", return_value={}),
            patch.object(ui, "survival_by_session", return_value={}),
            patch("aiwatcher_cli.cli.evidence_for_sessions", return_value={}),
        ):
            result = ui.build_session_search(30, search="nonexistent-project-xyz")

        self.assertEqual(result["total_matched"], 0)
        self.assertEqual(result["sessions"], [])

    def test_session_search_result_carries_session_id_for_drilldown(self) -> None:
        rows = [self._session("s1")]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}),
            patch.object(ui, "_cached_session_rows", return_value=rows),
            patch.object(ui, "evidence_for_sessions", return_value={}),
            patch.object(ui, "survival_by_session", return_value={}),
        ):
            result = ui.build_session_search(30)

        self.assertEqual(result["sessions"][0]["session_id"], "s1")
        self.assertIn("api_value", result["sessions"][0])
        self.assertEqual(result["sessions"][0]["state"]["status"], "recent")
        self.assertTrue(result["sessions"][0]["actions"])

    def test_session_search_filters_by_actionable_session_state(self) -> None:
        rows = [
            self._session("active", project_path="/repo/active", hours_ago=0),
            self._session("stale", project_path="/repo/stale", hours_ago=24 * 8),
        ]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}),
            patch.object(ui, "_cached_session_rows", return_value=rows),
            patch.object(ui, "evidence_for_sessions", return_value={}),
            patch.object(ui, "survival_by_session", return_value={}),
        ):
            active = ui.build_session_search(30, state_filter="active_recent")
            history = ui.build_session_search(30, state_filter="history")

        self.assertEqual([row["session_id"] for row in active["sessions"]], ["active"])
        self.assertEqual([row["session_id"] for row in history["sessions"]], ["stale"])

    def test_session_search_skips_evidence_lookup_without_evidence_filter(self) -> None:
        # evidence_for_sessions() shells out to git per session with no cache --
        # it's the dominant cost of a search request, so it must only run when
        # the caller actually filters by evidence. Regression test for a bug
        # where every search unconditionally paid this cost (multi-second lag
        # on every keystroke, even a plain outcome filter or no filter at all).
        rows = [self._session("s1"), self._session("s2")]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}),
            patch.object(ui, "_cached_session_rows", return_value=rows),
            patch.object(ui, "evidence_for_sessions") as mock_evidence,
        ):
            ui.build_session_search(30)
            ui.build_session_search(30, outcome="useful")

        mock_evidence.assert_not_called()

    def test_session_search_evidence_filter_labels_results_without_recomputing(self) -> None:
        rows = [self._session("s1")]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}),
            patch.object(ui, "_cached_session_rows", return_value=rows),
            patch.object(ui, "evidence_for_sessions") as mock_evidence,
            patch("aiwatcher_cli.cli.evidence_for_sessions", return_value={"s1": SimpleNamespace(inferred_outcome="needs_review")}),
        ):
            result = ui.build_session_search(30, evidence="needs_review")

        # filter_sessions() already computed this internally to filter -- build_session_search()
        # must label results from the `evidence` param directly, not call evidence_for_sessions again.
        mock_evidence.assert_not_called()
        self.assertEqual(result["sessions"][0]["inferred_outcome"], "needs_review")


if __name__ == "__main__":
    unittest.main()


class InsightFeedRankingTests(unittest.TestCase):
    """The feed replaced three panels that each restated the same facts. Its
    contract is: ranked by money, and nothing in it that lacks a comparison."""

    def _card(self, card_id: str, impact):
        return {"id": card_id, "title": card_id, "body": "", "impact_usd": impact,
                "session_id": None, "severity": "info"}

    def test_dollar_cards_rank_above_and_within_themselves(self) -> None:
        cards = [self._card("small", 1.0), self._card("none", None), self._card("big", 100.0)]
        with_impact = [c for c in cards if c["impact_usd"] is not None]
        without = [c for c in cards if c["impact_usd"] is None]
        with_impact.sort(key=lambda c: float(c["impact_usd"] or 0), reverse=True)
        ordered = [*with_impact, *without]
        self.assertEqual([c["id"] for c in ordered], ["big", "small", "none"])

    def test_feed_ranks_by_impact_and_labels_only_dollar_cards(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [
            ui.LocalSession(
                session_id=f"s{i}", tool="claude-code", project_path="/repo",
                started_at=now, updated_at=now, model="claude-sonnet-5",
                tokens_in=5_000_000, tokens_out=1_000, cache_read_tokens=4_900_000,
                cost_usd=20.0,
            )
            for i in range(3)
        ]
        feed = ui._insight_feed(rows, rows, [], days=7, inferred_useful=1, needs_review=0, churned=0)
        impacts = [c["impact_usd"] for c in feed if c["impact_usd"] is not None]

        self.assertEqual(impacts, sorted(impacts, reverse=True))
        self.assertTrue(all(c["impact_label"] for c in feed if c["impact_usd"] is not None))
        self.assertTrue(all(c["impact_label"] == "" for c in feed if c["impact_usd"] is None))

    def test_coverage_gaps_stay_off_the_feed(self) -> None:
        # Tools detected but not scanned are a setup concern that never
        # changes. Mixing them in is what made the old list read as noise.
        now = datetime.now(timezone.utc)
        rows = [ui.LocalSession(
            session_id="s1", tool="claude-code", project_path="/repo",
            started_at=now, updated_at=now, model="claude-sonnet-5",
            tokens_in=5_000_000, tokens_out=1_000, cache_read_tokens=4_900_000, cost_usd=20.0,
        )]
        feed = ui._insight_feed(rows, rows, [], days=7, inferred_useful=0, needs_review=0, churned=0)
        joined = " ".join(f"{c['title']} {c['body']}" for c in feed).lower()
        for tool in ("cursor", "cline", "windsurf"):
            self.assertNotIn(tool, joined)

    def test_every_card_carries_a_stable_id(self) -> None:
        now = datetime.now(timezone.utc)
        rows = [ui.LocalSession(
            session_id="s1", tool="claude-code", project_path="/repo",
            started_at=now, updated_at=now, model="claude-sonnet-5",
            tokens_in=5_000_000, tokens_out=1_000, cache_read_tokens=4_900_000, cost_usd=20.0,
        )]
        feed = ui._insight_feed(rows, rows, [], days=7, inferred_useful=2, needs_review=1, churned=1)
        ids = [c["id"] for c in feed]
        self.assertTrue(all(ids))
        self.assertEqual(len(ids), len(set(ids)))
