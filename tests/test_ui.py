from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiwatcher_cli import ui
from aiwatcher_cli.local_state import record_intervention, record_outcome
from aiwatcher_cli.scanner import LocalEvent, LocalSession


class DashboardWindowTests(unittest.TestCase):
    def test_dashboard_uses_focused_drawer_and_inline_feedback(self) -> None:
        self.assertIn('id="detailDrawer"', ui.HTML)
        self.assertIn('data-view="prompt"', ui.HTML)
        self.assertIn('data-view="receipts"', ui.HTML)
        self.assertIn('id="latestIntervention"', ui.HTML)
        self.assertIn('id="promptInput"', ui.HTML)
        self.assertIn('class="outcome-button useful', ui.HTML)
        self.assertIn('class="outcome-button rework', ui.HTML)
        self.assertIn('class="outcome-button abandoned', ui.HTML)
        self.assertIn("showToast(`Outcome saved:", ui.HTML)
        self.assertIn("Outcome evidence", ui.HTML)
        self.assertIn("Create handoff capsule", ui.HTML)
        self.assertIn("/api/handoff", ui.HTML)
        self.assertIn("Include prompt excerpt", ui.HTML)
        self.assertNotIn("window.alert", ui.HTML)

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
        ):
            summary = ui.build_summary(7)

        self.assertEqual(summary["totals"]["inferred_useful_outcomes"], 1)
        self.assertEqual(summary["recent_sessions"][0]["inferred_outcome"], "useful")
        self.assertTrue(any(item["title"] == "Outcome evidence found" for item in summary["insights"]))

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


if __name__ == "__main__":
    unittest.main()
