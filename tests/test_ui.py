from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from aiwatcher_cli import ui
from aiwatcher_cli.local_state import record_command_decision, record_intervention, record_outcome
from aiwatcher_cli.scanner import LocalEvent, LocalSession, SurfaceCoverage


class DashboardServeTests(unittest.TestCase):
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


class DashboardWindowTests(unittest.TestCase):
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
        self.assertIn('id="contextHealth"', ui.HTML)
        self.assertIn('id="coverageRows"', ui.HTML)
        self.assertIn('id="setupRows"', ui.HTML)
        self.assertIn('id="promptInput"', ui.HTML)
        self.assertIn('class="outcome-button useful', ui.HTML)
        self.assertIn('class="outcome-button rework', ui.HTML)
        self.assertIn('class="outcome-button abandoned', ui.HTML)
        self.assertIn("showToast(`Outcome saved:", ui.HTML)
        self.assertIn("Outcome evidence", ui.HTML)
        self.assertIn('class="handoff-cta"', ui.HTML)
        self.assertIn("Continue in a fresh session", ui.HTML)
        self.assertIn("Create handoff capsule", ui.HTML)
        self.assertIn('class="btn-primary" onclick="openHandoff', ui.HTML)
        self.assertIn("watch --notify", ui.HTML)
        self.assertIn("/api/handoff", ui.HTML)
        self.assertIn("Include prompt excerpt", ui.HTML)
        self.assertNotIn("window.alert", ui.HTML)

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
            bloat_ratio=0.98,
            efficiency_pct=2.0,
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
        ):
            summary = ui.build_summary(7)

        self.assertEqual(summary["coverage"][0]["surface_id"], "claude-code-cli")
        self.assertTrue(any(step["command"] == "aiwatcher hook-status" for step in summary["setup"]))
        self.assertEqual(summary["context_health"][0]["severity"], "critical")
        self.assertEqual(summary["context_health"][0]["action"]["label"], "Start fresh")
        self.assertTrue(any(item["title"] == "Context health needs attention" for item in summary["insights"]))

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
        self.assertTrue(any(item["title"] == "Outcome evidence found" for item in summary["insights"]))

    def test_cost_per_surviving_change_unavailable_below_sample_threshold(self) -> None:
        rows = [
            LocalSession(session_id=f"s{i}", tool="claude-code", project_path="/repo", cost_usd=1.0)
            for i in range(3)
        ]
        snapshots = {
            f"s{i}": {"survival": {"7": {"status": "survived"}}} for i in range(3)
        }
        with patch.object(ui, "evidence_snapshots_for_sessions", return_value=snapshots):
            result = ui._cost_per_surviving_change(rows)
        self.assertFalse(result["available"])
        self.assertEqual(result["sample_count"], 3)

    def test_cost_per_surviving_change_reports_survived_vs_churned_averages(self) -> None:
        rows = (
            [LocalSession(session_id=f"survived-{i}", tool="claude-code", project_path="/repo", cost_usd=2.0) for i in range(3)]
            + [LocalSession(session_id=f"churned-{i}", tool="claude-code", project_path="/repo", cost_usd=4.0) for i in range(2)]
        )
        snapshots = {
            **{f"survived-{i}": {"survival": {"7": {"status": "survived"}}} for i in range(3)},
            **{f"churned-{i}": {"survival": {"7": {"status": "churned"}}} for i in range(2)},
        }
        with patch.object(ui, "evidence_snapshots_for_sessions", return_value=snapshots):
            result = ui._cost_per_surviving_change(rows)
        self.assertTrue(result["available"])
        self.assertEqual(result["surviving_count"], 3)
        self.assertEqual(result["churned_count"], 2)
        self.assertEqual(result["cost_per_surviving_change"], "$2.00")
        self.assertEqual(result["cost_per_churned_change"], "$4.00")

    def test_cost_per_surviving_change_uses_earliest_checked_bucket(self) -> None:
        rows = [
            LocalSession(session_id=f"s{i}", tool="claude-code", project_path="/repo", cost_usd=1.0)
            for i in range(5)
        ]
        # s0: 7-day bucket unchecked, 14-day says survived -- should still count via the 14-day result.
        snapshots = {
            "s0": {"survival": {"14": {"status": "survived"}}},
            **{f"s{i}": {"survival": {"7": {"status": "survived"}}} for i in range(1, 5)},
        }
        with patch.object(ui, "evidence_snapshots_for_sessions", return_value=snapshots):
            result = ui._cost_per_surviving_change(rows)
        self.assertEqual(result["surviving_count"], 5)

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

        self.assertTrue(any(item["title"] == "Work that didn't stick" for item in summary["insights"]))
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
                patch.object(ui, "scan_all", return_value=rows),
                patch.object(ui, "evidence_for_sessions", return_value={}),
                patch.object(ui, "survival_by_session", return_value={}),
            ):
                record_outcome("s1", "useful")
                result = ui.build_session_search(30, search="alpha", outcome="useful")

        self.assertEqual(result["total_matched"], 1)
        self.assertEqual(result["sessions"][0]["session_id"], "s1")
        self.assertEqual(result["query"], {"search": "alpha", "outcome": "useful", "evidence": ""})

    def test_session_search_no_match_returns_empty_list(self) -> None:
        rows = [self._session("s1", project_path="/repo/alpha")]
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.dict(os.environ, {"AIWATCHER_STATE_FILE": os.path.join(temp_dir, "state.json")}),
            patch.object(ui, "scan_all", return_value=rows),
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
            patch.object(ui, "scan_all", return_value=rows),
            patch.object(ui, "evidence_for_sessions", return_value={}),
            patch.object(ui, "survival_by_session", return_value={}),
        ):
            result = ui.build_session_search(30)

        self.assertEqual(result["sessions"][0]["session_id"], "s1")
        self.assertIn("api_value", result["sessions"][0])

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
            patch.object(ui, "scan_all", return_value=rows),
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
            patch.object(ui, "scan_all", return_value=rows),
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
